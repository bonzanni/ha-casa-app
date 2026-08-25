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
import hashlib
import logging
import os
import time
from io import BytesIO
from typing import Any, Awaitable, Callable

from telegram import InputFile, Update
from telegram.constants import ChatAction
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

from channels.tg_richtext import render, render_paged
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
from channels import Channel, DeliveryOutcome
# v0.79.0 (§2 Primitive A): the per-topic OUTPUT SEQUENCER + relay-mediated
# discrete-posting intent registry. Implemented in the sibling module and
# RE-EXPORTED here so ``channels.telegram.OutputSequencer`` resolves per the
# design's "File: channels/telegram.py" reference; the class is deliberately
# PTB-free (injected send/edit primitives) so it stays unit-testable in
# isolation. The live per-engagement instances are owned by the claude_code
# driver (which holds the topic send/edit primitives + the relay); the discrete
# ingresses reach them through that driver's intent-registration API.
from channels.output_sequencer import (  # noqa: F401 — re-export
    IntentRegistry,
    OutputSequencer,
    projection_hash as discrete_projection_hash,
)
from media_policies import MEDIA_POLICIES
from text_util import utf16_len, utf16_prefix_end
from channels.telegram_supervisor import ReconnectSupervisor
from log_cid import cid_var, new_cid
from provenance import (
    sanitize_external_context,
    scheduled_delivery_markers,
    strict_positive_id,
)
from rate_limit import RateLimiter

import topic_ledger

logger = logging.getLogger(__name__)

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
# v0.75.0 (W5/Sol B3,B4): inline-callback data — v1 broker format + legacy
# perm:<verdict>:<rid> back-compat.
# ---------------------------------------------------------------------------

# Telegram hard-caps callback_data at 64 bytes.
_CALLBACK_DATA_MAX_BYTES = 64
_CALLBACK_NAMESPACES = ("permission", "engagement_ask", "resident_ask")


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
      the broker's three namespaces, ``option_index`` an int. ``kind`` is
      ``None`` (a single-select / permission tap).
    - A5 · F-MULTI: ``v1|ask_multi|<request_id>|<idx>`` (a checkbox TOGGLE,
      ``kind="toggle"``) and ``v1|ask_multi|<request_id>|s`` (SUBMIT,
      ``kind="submit"``, ``option_index`` is ``None``). The ``ask_multi``
      namespace maps to the ``engagement_ask`` broker namespace — only the
      callback grammar is new.
    - legacy (back-compat): ``perm:<allow|deny>:<request_id>`` — pre-v0.75.0
      keyboards still in flight across an upgrade must keep routing.

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
        if ns == "ask_multi":
            # A5 · F-MULTI: routes onto the engagement_ask broker namespace.
            if idx_s == "s":
                return "engagement_ask", request_id, None, "submit"
            try:
                idx = int(idx_s)
            except ValueError:
                return None, None, None, None
            return "engagement_ask", request_id, idx, "toggle"
        if ns not in _CALLBACK_NAMESPACES:
            return None, None, None, None
        try:
            idx = int(idx_s)
        except ValueError:
            return None, None, None, None
        return ns, request_id, idx, None
    parts = data.split(":", 2)
    if (len(parts) == 3 and parts[0] == "perm"
            and parts[1] in ("allow", "deny") and parts[2]):
        return "permission", parts[2], (0 if parts[1] == "allow" else 1), None
    return None, None, None, None


# v0.81.0 (W-R3, Sol r1-5): button labels are DERIVED channel-side from the
# full option so they stay readable — Telegram truncates long labels to a few
# words, which made most choices unpickable. The full option text lives VERBATIM
# in the message body (``channel_handlers.render_ask_body``); the button carries
# only a short summary. The button's IDENTITY stays the option INDEX in
# ``callback_data`` — the label is display-only, so nothing is lost by shortening.
#
# Round 4 (spec D2, Sol r1-3/r2-3/r2-7/r3-5): the v0.83.0 per-option elision
# ladder is GONE — it garbled labels ("1 · A —…MCP…MCPB"). Button labels are now MODEL-generated: the agent
# supplies an optional ``short`` per option, resolved with WHOLE-SET semantics by
# ``resolve_button_labels`` below. Either EVERY option has a usable short and
# the whole set renders ``n · <short>`` verbatim, or the WHOLE set floors to
# ``Option 1``, ``Option 2``, … — never mixed, never mutated, never a
# rejection. The Haiku label-generation fallback is explicitly DEFERRED (D2
# item 4): until it ships (on the hardened CLI runtime, only if telemetry shows
# the floor is materially inadequate), an ask with no agent-supplied shorts
# always floors. ``floored_ask_telemetry`` is the CONTENT-FREE log line for
# that floor (never the option/question text).
_ASK_BUTTON_CAPTION_CAP = 64

# A5 · F-MULTI checkbox glyphs (also the multi decoration prefix the resolver
# validates against — see ``_classify_button_labels``).
_MULTI_BOX_ON = "☑"
_MULTI_BOX_OFF = "☐"
_MULTI_SUBMIT_TEXT = "✅ Submit"


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


def _classify_button_labels(
    options: list, multi: bool,
) -> "tuple[str | None, list[str]]":
    """Whole-set classification shared by ``resolve_button_labels`` and
    ``floored_ask_telemetry`` (Sol r2-3/r2-7).

    Every option must carry a ``short`` that is (1) a ``str``, (2) non-blank
    after ``strip()``, (3) pairwise-distinct (casefold-insensitive) across the
    WHOLE set, and (4) whose DECORATED caption — ``f"☑ {i+1} · {short}"`` for a
    multi ask, ``f"{i+1} · {short}"`` otherwise — fits the 64-char product
    contract (Sol r3-5: the same short can pass single-select and fail multi
    at the identical length). Any failure floors the WHOLE set.

    Returns ``(floor_reason, captions)``: ``floor_reason`` is ``None`` and
    ``captions`` are the stored ``n · <short>`` captions VERBATIM (undecorated
    — the checkbox glyph is a separate render-time concern, D2 item 3) when
    every option passes; otherwise ``floor_reason`` is one of
    ``no_shorts|blank|dup|too_long`` and ``captions`` is the numbered floor.
    Pure; never mutates ``options``; never raises.
    """
    n = len(options)
    raw = [_button_short(o) for o in options]
    if any(s is None for s in raw):
        return "no_shorts", _floor_captions(n)
    # wb1-4: ``strip()`` is used ONLY as the BLANK predicate (D2: verbatim-or-
    # floor, never mutated). Distinctness, caption construction, and the ≤64
    # decorated-length check all read the RAW short — stripping first would
    # silently change uniqueness (" Setup " vs "Setup") and length decisions
    # (trailing padding that overflows the 64-char caption), both mutations the
    # verbatim contract forbids. A short renders exactly as authored:
    # ``"  Setup  "`` → ``"1 ·   Setup  "``.
    if any(s.strip() == "" for s in raw):
        return "blank", _floor_captions(n)
    if len({s.casefold() for s in raw}) != n:
        return "dup", _floor_captions(n)
    captions = [f"{i + 1} · {raw[i]}" for i in range(n)]
    for cap in captions:
        decorated = f"{_MULTI_BOX_ON} {cap}" if multi else cap
        if len(decorated) > _ASK_BUTTON_CAPTION_CAP:
            return "too_long", _floor_captions(n)
    return None, captions


def resolve_button_labels(options: list, multi: bool) -> list[str]:
    """THE pure whole-set button-label resolver (spec D2 item 1).

    ``options`` items are either a bare ``str`` (full label, no short) or
    ``{"label": str, "short": ...}``. Returns the final captions
    ``f"{i+1} · {short}"`` for ALL options iff EVERY option has a usable short
    (see ``_classify_button_labels``); otherwise the whole-set floor
    ``["Option 1", "Option 2", …]``. Never mixed (some real, some floored),
    never mutated, never raises. Run BEFORE ``BROKER.register`` so the
    resolved captions can be placed in static broker meta (wiring: Task A5).
    """
    _reason, captions = _classify_button_labels(options, multi)
    return captions


def floored_ask_telemetry(options: list, *, multi: bool = False) -> str:
    """CONTENT-FREE log line for a floored ask (spec D2 item 4, Sol r2-7).

    Records only safe dimensions — option count, floor reason
    (``no_shorts|blank|dup|too_long|none`` when it didn't float), a
    short-presence bitmap (``1`` iff the option carries a ``str`` short,
    regardless of blank/dup/length validity — a diagnostic aid for *which*
    option(s) are missing one), and a bounded (12 hex char) hash of the
    option set — NEVER the option/question text (agent-authored,
    operator-decision content that could be large, sensitive, or
    log-injecting once the invented length caps are gone, D1).
    """
    reason, _captions = _classify_button_labels(options, multi)
    bitmap = "".join(
        "1" if _button_short(o) is not None else "0" for o in options)
    labels = [_button_label_text(o) for o in options]
    joined = "\x1f".join(labels)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]
    return (
        f"floored_ask_telemetry count={len(options)} "
        f"reason={reason if reason is not None else 'none'} "
        f"shorts={bitmap} hash={digest}"
    )


def floored_ask_telemetry_line(options: list, *, multi: bool = False) -> "str | None":
    """Return the CONTENT-FREE floor telemetry line IFF the whole-set resolver
    FLOORS this button ask (wb3-4, D2 item 4); ``None`` when every option
    resolved to a verbatim short (no floor happened — nothing to log).

    The wb3-4 wiring gap this closes: :func:`floored_ask_telemetry` existed but
    was never called in production, so the accepted telemetry-gated-Haiku
    decision had no data source. The ask handler logs this line ONCE, at the
    owner's registration path, exactly when a real floor occurs. Pure; never
    raises; empty ``options`` (a free-text anchor) classifies as ``no floor`` →
    ``None``, so anchors never emit."""
    reason, _captions = _classify_button_labels(options, multi)
    if reason is None:
        return None
    return floored_ask_telemetry(options, multi=multi)


def short_option_labels(
    labels: list, shorts: "list | None" = None,
) -> list[str]:
    """Derive button labels for a WHOLE option set (name/signature PINNED —
    resident ``ask_user`` calls this via
    ``post_dm_keyboard(short_labels=True)``, wired from parallel-owned
    ``tools.py``).

    Agent shorts win VERBATIM when every ``labels[i]`` has a usable, paired
    ``shorts[i]`` (same whole-set rule as ``resolve_button_labels``); otherwise
    the WHOLE set floors to the numbered placeholder. No elision anywhere
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


def _captions_from_shorts(
    options: list, shorts: "list | None", multi: bool,
) -> list[str]:
    """Resolve the whole-set captions from ``options`` + parallel ``shorts``
    (the ``resolve_button_labels`` input shape). Used ONLY as the fallback
    when no persisted ``button_labels`` meta exists yet (eager fallback /
    direct-call unit tests) — the live path always consumes the persisted
    captions seeded at registration (D2 item 3, Task A5)."""
    combined = [
        {
            "label": str(opt),
            "short": shorts[i] if shorts is not None and i < len(shorts) else None,
        }
        for i, opt in enumerate(options)
    ]
    return resolve_button_labels(combined, multi)


def _multi_button_caption(i: int, captions: list[str], selected: set) -> str:
    """A5 · F-MULTI: one toggle-row caption ``☐/☑ <stored caption>``. The
    checkbox glyph reflects membership in ``selected``; ``captions[i]`` is
    already the whole-set-resolved ``n · <short>`` or floored ``Option n``."""
    box = _MULTI_BOX_ON if i in selected else _MULTI_BOX_OFF
    return f"{box} {captions[i]}"


def _build_multi_keyboard(
    request_id: str, options: list, captions: "list[str]", selected,
):
    """A5 · F-MULTI: the toggle-many keyboard — one ``☐/☑ n · <label>`` row per
    option (callback ``v1|ask_multi|<rid>|<idx>``) plus a final ``✅ Submit`` row
    (``v1|ask_multi|<rid>|s``). Shared by the initial post and every redraw so
    the two can never disagree.

    v0.84.0 (D2 items 2-3, Task A5 wiring): ``captions`` are the PERSISTED
    whole-set button labels (``resolve_button_labels`` output, seeded into
    broker meta as ``button_labels`` at registration). The builder NEVER
    re-resolves from ``shorts`` — the redraw prepends only the mutable ☐/☑
    glyph to the STORED caption, so every toggle re-renders byte-identically
    (the checkbox glyph stays separate mutable state, never part of the
    persisted caption). This closes the resolution-drift bug where the redraw
    re-derived captions from ``shorts`` independently of the initial post."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    sel = set(selected or ())
    rows = [
        [InlineKeyboardButton(
            text=_multi_button_caption(i, captions, sel),
            callback_data=f"v1|ask_multi|{request_id}|{i}",
        )]
        for i in range(len(options))
    ]
    rows.append([InlineKeyboardButton(
        text=_MULTI_SUBMIT_TEXT,
        callback_data=f"v1|ask_multi|{request_id}|s",
    )])
    return InlineKeyboardMarkup(rows)


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
"""#692: the bound on the ONE cut-off-turn notice attempt. Matches
``_settle_lost_inbound``'s existing bound for the same reason — a bounded
best-effort telling, never retried, because retry-until-posted on an
engagement path is the INV-ENG-003 livelock class."""

_SETTLED_STATUS_TIMEOUT_S = 5.0
"""#692: the bound on the registry's settled-status read.

The registry lock is held across a tombstone write, and this read happens on a
Telegram delivery task — which must not be able to wedge behind a wedged
registry. On timeout the notice is posted ANYWAY: this issue is about silence,
so the fail-open direction is toward telling the operator."""

_TURN_ENDED_UNCONFIRMED_NOTICE = (
    "This engagement has ended, and Casa could not confirm that its account of "
    "the outcome reached this topic. If a summary appears above, that is it."
)
"""#678: what a cut-off follow-up turn's owner says when the engagement is
already TERMINAL and its terminal path could not confirm a telling.

A distinct sentence from :data:`_TURN_CUT_OFF_NOTICE`, on a reviewer's finding
in the first diff round, because that one would be FALSE here. A Telegram
``TimedOut`` can lose the acknowledgement of a completion summary the wire
accepted: the operator then SEES the summary while the funnel recorded the
telling as unconfirmed, and "this turn did not finish … anything it already
posted above may be partial" contradicts what is on their screen. The turn
also did not fail — it ended BECAUSE it completed the engagement.

THE RULE THESE STRINGS OBEY, after the same finding shape three times. A notice
posted from here may assert ONLY what its own site observed:

  (a) the engagement's SETTLED terminal status, which is durable because the
      strict transition commits memory and disk together or neither; and
  (b) that the telling was NOT confirmed.

Everything else was cut rather than reworded a fourth time. Three successive
reviews found the same shape — prose asserting state the site cannot see: first
"this turn did not finish … may be partial" (false when the stream carried
text), then "the outcome was delivered to the engager and is in the engagement
record" (the notify is best-effort and its failure is caught, and the record
holds the terminal state rather than the summary text), then "this topic is
closed … deleted with everything in it at its retention deadline" (the close
can raise and the retention-ledger append is best-effort too). Sharpening the
wording once more would have been the fourth patch on one mechanism. The
mechanism is gone instead: there is no longer any sentence here about topic
lifecycle, retention, the engager, or the record's contents.

WHICH CONTROL HOLDS THIS, stated exactly, because a blacklist of forbidden
phrases stood here and was CUT: the suite pins this sentence by WHOLE-SENTENCE
EQUALITY, so it cannot change silently — a production-only reword fails a test.
Nothing in the suite stops an author who edits this string and its expectation
TOGETHER, and nothing can: whether a new sentence is TRUE is a judgment, and the
blacklist that tried to encode it was bypassed three times by respelling the same
false claims. So if you are editing this string, the rule above is the control,
and it is on you. See `architecture/engagement-finalization.md`.
"""


_TURN_CUT_OFF_NOTICE = (
    "This turn did not finish: its stream stopped without ResultMessage, the "
    "frame that marks a turn that ran to its end, so its outcome was never "
    "confirmed. Anything it already posted above may be partial, and work it "
    "started may already have taken effect \u2014 check before asking for it "
    "again."
)
"""#692: the whole of what a cut-off follow-up turn tells the operator.

It states the terminal-artifact FACT and not merely that something failed: the
report exists to name why the outcome is unknown, and a notice that dropped
that would be a different, weaker guarantee. Pinned by whole-sentence equality
AND by an independent substring assertion, which protect different things — the
first stops a silent rewording, the second stops a rewording that keeps the
sentence's shape and loses its content.

It also does NOT say nothing was reported, and does NOT invite a retry, and
both of those are corrections of an earlier draft that said both. A cut-off
turn can have streamed assistant text before the stream ended — the driver
finalizes whatever it accumulated before recording the observation, deliberately
so — so "nothing was reported back for it" was simply false whenever that
happened, and "send your message again if you want it retried" invited the
operator to repeat work that had already taken effect. For non-idempotent work
that is the notice actively causing harm. What is always true is that the
turn's outcome was never confirmed and that whatever it did may be partial, so
that is what it says."""

# Callback type for on_token streaming
OnTokenCallback = Callable[[str], Awaitable[None]]


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
        # supervisor retry. Boot replay snapshots pre-service but its confirmed
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
        # Test-injection bot and engagement supergroup.
        self._bot = bot
        self.engagement_supergroup_id = engagement_supergroup_id
        self.engagement_permission_ok = False
        # M10 (v0.52.0): the bot's own @username, cached at setup so
        # handle_update can strip a trailing "@botname" off engagement
        # commands (group command menus send "/cancel@casabot"). None until
        # setup_engagement_features resolves it; while None the suffix is
        # stripped unconditionally (safe inside Casa's own supergroup).
        self._bot_username: str | None = None
        # H5 (v0.52.0): bounded LRU of recently-seen webhook update_ids so a
        # Telegram redelivery (retry after a slow ACK) is processed at most
        # once. OrderedDict used as an insertion-ordered ring buffer.
        self._seen_update_ids: "collections.OrderedDict[int, None]" = (
            collections.OrderedDict()
        )
        # M9 (v0.52.0): strong refs to in-flight engagement-turn delivery
        # tasks. handle_update spawns the SDK turn in the background (so the
        # per-topic lock is not held across a multi-minute turn and /cancel
        # can interrupt it); each task keeps a ref here + a done-callback that
        # discards it. Cancelled in stop().
        self._turn_tasks: set[asyncio.Task] = set()
        # Injectable collaborators (wired at startup by Task 22; tests assign AsyncMocks).
        self._engagement_registry = None
        self._observer = None
        self._driver_send_user_turn = None
        # v0.79.0 (§3): seal open narration on inbound (claude_code engagements);
        # wired by casa_core, None-safe for tests.
        self._driver_advance_high_water = None
        # v0.83.0 (§A3, Sol r7-1/r8-1): the answered-RESERVATION seam. SYNCHRONOUS
        # ``reserve_answer(rec) -> token|None`` set in the SAME section as the
        # high-water advance (no await between); ``rollback_answer_reservation(
        # rec, token) -> bool`` (CAS) fires on every non-delivery path (command
        # classification, handler cancellation, spool enqueue rejection). Wired by
        # casa_core, None-safe for tests.
        self._driver_reserve_answer = None
        # G4 D2 (v0.96.0): inbound-ingress reservation seam (see casa_core).
        self._driver_reserve_inbound = None
        self._driver_release_inbound = None
        # #649: in_casa admission-ticket seams (see casa_core). ``admit`` is
        # SYNCHRONOUS and taken before the handler's first risky await;
        # ``discharge`` is idempotent by exact token; ``held`` is the failure
        # owner's peek before its one bounded notice attempt. None-safe.
        self._driver_admit_inbound = None
        self._driver_discharge_inbound = None
        self._driver_inbound_held = None
        # #649: producer-closing stop gate + the failure finalizers stop()
        # must drain (visibility law — a live record's ticket is never
        # discharged before one bounded visible-outcome attempt).
        self._stopping = False
        self._inbound_cleanup_tasks: set[asyncio.Task] = set()
        self._driver_rollback_answer_reservation = None
        # v0.79.0 (§3, F2): route a platform-origin topic notice (command
        # replies, resume errors) through the engagement's OUTPUT SEQUENCER so
        # it seals open narration + advances the high-water under the single
        # writer — a notice can never land BELOW live narration. Wired by
        # casa_core; None-safe (falls back to a direct send).
        self._driver_post_notice = None
        # #692: SYNCHRONOUS read of the in_casa driver's follow-up-turn
        # observation for one exact admission ticket, wired by casa_core;
        # None-safe, and "" for a claude_code record. The driver OBSERVES that
        # a ticketed turn ended with no ResultMessage; this channel's delivery
        # task ADJUDICATES, because only it can tell that cutoff from a
        # healthy in_casa self-emit completion.
        self._driver_turn_incomplete = None
        # #369: clearance-downgrade context revocation seams, wired by
        # casa_core per driver; None-safe for tests. ``invalidate_session``
        # tears the live session down (in_casa: close the client; claude_code:
        # stop the service + drop the control-dir session pointer);
        # ``context_rebuilder(rec) -> str`` establishes a FRESH session at the
        # record's (clamped) markers and returns the preamble to prepend to
        # the next delivered turn.
        self._driver_invalidate_session = None
        self._engagement_context_rebuilder = None
        # Preamble stash, keyed by engagement id: in-memory only — losing it
        # to a crash loses a courtesy note, never the rebuild itself (the
        # durable flag is what refuses the old session).
        self._rebuild_preambles: dict[str, str] = {}
        self._engagement_driver = None
        self._finalize_cancel = None
        self._finalize_complete_user = None
        self._session_registry = None   # wired in main() (Task 9); /new reset
        self._semantic_memory = None    # wired in main() (Task 9); /new reset
        self._main_feed_redirect_seen: set[int] = set()
        # Per-topic handler lock (Bug 10, v0.14.7). aiohttp dispatches
        # handle_update concurrently per Bot-API update; without this,
        # /cancel racing a regular turn could route the turn to a driver
        # that's already been torn down by _finalize_engagement. Keyed by
        # message_thread_id (1:1 with engagement via _topic_index). Mirrors
        # in_casa_driver._locks. Entries are not pruned — bounded growth,
        # cleared on add-on restart.
        self._engagement_handler_locks: dict[int, asyncio.Lock] = {}
        # #317: per-chat serialization for the DM path (_handle). Handlers run
        # with block=False (H5), so /new and an immediately-following message
        # dispatch concurrently — the follow-up could resume the mid-reset
        # session or have its fresh session erased by the reset's trailing
        # remove. Serializing /new (which awaits the whole reset) against
        # same-chat message ENQUEUE restores the ordering M29 assumed from
        # PTB's default serialized dispatch; other chats keep flowing. Keyed
        # by chat_id; entries are not pruned — bounded growth (DM chats),
        # cleared on add-on restart, mirroring _engagement_handler_locks.
        self._chat_serial_locks: dict[str, asyncio.Lock] = {}
        # #347: per-engagement topic-STATE edit serialization (see
        # update_topic_state) — orders the relay's awaiting/active round-trip
        # against terminal-transition edits so a closed topic can never be
        # repainted active.
        self._topic_state_locks: dict[str, asyncio.Lock] = {}
        # R5 (v0.89.0): NON-PERSISTENT current-inbound target for the `react`
        # framework tool. Maps engagement_id -> (chat_id, topic_id, message_id)
        # of the LATEST operator message seen in that engagement's topic.
        # Deliberately in-memory (NOT a registry field): it must NOT survive a
        # restart — after a restart there is no known "latest" message to react
        # to, and the react tool's non-live gate makes an empty map fail soft.
        # Recorded SYNCHRONOUSLY in handle_update from the trusted
        # msg.message_id; MONOTONIC (never regresses to an older id).
        self._current_inbound: dict[str, tuple[int, int, int]] = {}
        # Default routing: forward to the existing PTB handler.
        self._route_to_ellen = self._route_to_ellen_default

    # ------------------------------------------------------------------
    # R5 (v0.89.0): current-inbound target + emoji reaction (react tool)
    # ------------------------------------------------------------------

    def _record_inbound_target(
        self, engagement_id: str, chat_id: int, topic_id: int,
        message_id: int | None,
    ) -> None:
        """Record the LATEST operator message for an engagement (react target).

        MONOTONIC: replaces the stored target ONLY when ``message_id`` is
        strictly greater than the stored one. ``handle_update`` runs
        concurrently per Bot-API update; the per-topic lock serialises routing
        but NOT Telegram delivery order, so an unconditional write could let a
        delayed older update clobber a newer target. Telegram message ids are
        monotonically increasing per chat, so the greatest id is the newest.
        """
        if not engagement_id or message_id is None:
            return
        prev = self._current_inbound.get(engagement_id)
        if prev is not None and message_id <= prev[2]:
            return
        self._current_inbound[engagement_id] = (chat_id, topic_id, message_id)

    def get_current_inbound(
        self, engagement_id: str,
    ) -> tuple[int, int, int] | None:
        """Return ``(chat_id, topic_id, message_id)`` for the engagement's
        latest operator message, or ``None`` (restart / no inbound / cleared)."""
        return self._current_inbound.get(engagement_id)

    def clear_inbound(self, engagement_id: str) -> None:
        """Best-effort drop of a finished engagement's target (map hygiene).
        A missed clear is harmless: react's non-live gate rejects it anyway."""
        self._current_inbound.pop(engagement_id, None)

    async def set_reaction(
        self, chat_id: int, message_id: int, emoji: str,
    ) -> None:
        """Set a single emoji reaction on a message (PTB set_message_reaction).

        Raises when the channel isn't started (RuntimeError) and lets
        TelegramError propagate — the ``react`` tool classifies both."""
        from telegram import ReactionTypeEmoji
        bot = self.bot
        if bot is None:
            raise RuntimeError("Telegram channel not started; cannot set reaction")
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )

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

        # #649: bounded, loop-until-stable drain of the cancelled turn tasks
        # AND the ticket-cleanup finalizers their done_callbacks create —
        # a single snapshot cannot see a finalizer born after it (Sol seam
        # r8/r9). Bounded: shutdown never wedges on a notice; whatever the
        # bound abandons is the accepted non-durable-ledger process-death
        # limit.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while True:
            pending = [t for t in (
                list(self._turn_tasks) + list(self._inbound_cleanup_tasks))
                if not t.done()]
            if not pending or loop.time() >= deadline:
                break
            await asyncio.wait(
                pending, timeout=max(0.1, deadline - loop.time()))
        self._turn_tasks.clear()
        self._inbound_cleanup_tasks.clear()

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
        app = builder.build()
        # E-13: dispatch to handle_update (engagement-aware router) so
        # that /cancel, /complete, /silent in engagement topics are
        # intercepted. handle_update internally calls _handle for
        # non-engagement chats (Ellen DM via _route_to_ellen).
        #
        # H5 (v0.52.0): block=False dispatches each update via
        # Application.create_task rather than awaiting it inline, so one
        # long engagement turn cannot stall PTB's sequential update fetcher
        # (default max_concurrent_updates=1) — which in polling mode would
        # otherwise freeze Ellen DMs too. Same-topic ordering stays
        # serialized by _engagement_handler_locks; PTB routes task
        # exceptions to the registered error handler.
        app.add_handler(
            MessageHandler(filters.TEXT, self.handle_update, block=False)
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

        # E-F (v0.30.0): engagement-feature setup MUST run after
        # `self._app = app`. Pre-fix, casa_core.py invoked this once at
        # boot — but if that first `_rebuild` raised on `set_webhook`
        # (transient network blip), `self._app` was never set, the boot
        # call to `setup_engagement_features()` saw `self.bot is None`,
        # raised AttributeError on `None.get_me()`, and left
        # `engagement_permission_ok=False` permanently. Every subsequent
        # supervisor-driven `_rebuild` succeeded, but no path re-ran the
        # engagement setup. Tying it to `_rebuild` makes it self-healing
        # on every successful rebuild. Idempotent — line 647 resets the
        # flag at the start of each call.
        try:
            await self.setup_engagement_features()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "setup_engagement_features failed during _rebuild: %s", exc,
            )

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
        lock = self._chat_serial_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            await self._handle_serialized(update, chat_id)

    async def _handle_serialized(self, update: Update, chat_id: str) -> None:
        """Body of ``_handle``, run under ``_chat_serial_locks[chat_id]`` (#317)."""
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
            # M29: ack BEFORE the (potentially multi-second) save so the user
            # gets instant feedback. reset_channel stays awaited (NOT
            # backgrounded) — the registry entry must survive until the retain
            # succeeds so the reaper can retry. Handlers dispatch with
            # block=False (H5), so PTB no longer serializes updates; the
            # per-chat lock held by _handle (#317) is what now guarantees no
            # same-chat follow-up can resume the old session mid-reset.
            if self._app is not None:
                try:
                    await self._app.bot.send_message(
                        chat_id=chat_id,
                        text="Starting fresh — I still remember what matters.",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("/new ack send to chat_id=%s failed: %s", chat_id, exc)
            if self._session_registry is not None and self._semantic_memory is not None:
                from session_registry import build_scoped_session_key
                from session_saver import reset_channel
                channel_key = build_scoped_session_key(
                    "telegram", self.default_agent, chat_id,
                )
                await reset_channel(
                    channel_key, self._session_registry, self._semantic_memory,
                    channel="telegram",
                )
            return  # do NOT feed /new to the agent

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
                return

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

        user = update.effective_user
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
            # Bug 8 (v0.14.6): user_id is needed downstream to enforce
            # originator-only /cancel and /complete on engagement topics.
            # Stored as int when present (Telegram user ids are ints).
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

        msg = BusMessage(
            type=MessageType.CHANNEL_IN,
            source="telegram",
            target=self.default_agent,
            content=update.message.text,
            channel="telegram",
            context=context,
            trusted_user_origin=trusted_origin,
        )
        await self._bus.send(msg)

    # ------------------------------------------------------------------
    # Engagement routing (Task 11)
    # ------------------------------------------------------------------

    async def _route_to_ellen_default(self, update) -> None:
        """Default behavior for non-engagement chats: feed into existing _handle."""
        await self._handle(update, None)

    async def _post_engagement_notice(self, rec, text: str) -> None:
        """v0.79.0 (§3, F2): post a platform-origin notice into an engagement
        topic THROUGH the output sequencer (seals open narration + advances the
        high-water under the single writer) so it can never land BELOW live
        narration. Falls back to a direct send when the sequencer seam is not
        wired (tests) or the engagement's driver has no sequencer (in_casa)."""
        if self._driver_post_notice is not None:
            await self._driver_post_notice(rec, text)
        else:
            # v0.109.0 (G3): notices carry agent/platform markdown — render.
            await self.send_to_topic_rich(rec.topic_id, text)

    async def handle_update(
        self, update, _context: ContextTypes.DEFAULT_TYPE | None = None
    ) -> None:
        """PTB MessageHandler entry-point. Routes by chat_id to engagement or Ellen.

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
        text = (msg.text or "").strip()
        thread_id = getattr(msg, "message_thread_id", None)
        user_id = msg.from_user.id if msg.from_user else None

        # 1) 1:1 chat with Ellen — existing behaviour
        # Note: self.chat_id may be a string or int — coerce for comparison
        if str(chat_id) == str(self.chat_id):
            return await self._route_to_ellen(update)

        # 2) Engagement supergroup
        if self.engagement_supergroup_id and chat_id == self.engagement_supergroup_id:
            if not thread_id:
                await self._maybe_redirect_main_feed(user_id)
                return
            if self._engagement_registry is None:
                return  # not wired yet; ignore silently
            # Bug 10 (v0.14.7): per-topic lock serialises updates landing
            # in the same engagement so a /cancel can't finish tearing
            # down the driver while a concurrent regular turn is still
            # mid-routing. Different topics still run in parallel.
            lock = self._engagement_handler_locks.setdefault(
                thread_id, asyncio.Lock(),
            )
            async with lock:
                rec = self._engagement_registry.by_topic_id(thread_id)
                if rec is None or rec.status in ("completed", "cancelled", "error"):
                    await self.send_to_topic(thread_id, "No active engagement in this topic.")
                    return

                # #324: engagement-topic turns consult the same inbound rate
                # limit as the DM route (previously they spooled unthrottled).
                # Keyed per TOPIC — one topic is one conversation; a
                # supergroup-wide key would let one busy topic starve the
                # others. Commands are EXEMPT (mirroring /new-before-limiter
                # in the DM route): a /cancel must never be refused by
                # throttling, or a runaway engagement could not be stopped.
                # Checked before ANY side effect (clearance clamp, inbound
                # reservation, high-water advance) so a dropped message
                # leaves no state behind; the ordering cost is only cosmetic
                # (later narration may land below the dropped message).
                if (self._rate_limiter is not None
                        and self._rate_limiter.enabled
                        and not text.startswith("/")):
                    decision = self._rate_limiter.check(
                        f"{chat_id}:{thread_id}")
                    if not decision.allowed:
                        if decision.should_notify:
                            logger.info(
                                "Telegram rate limit hit for engagement "
                                "topic %s (chat_id=%s); replying with "
                                "one-shot notice", thread_id, chat_id,
                            )
                            try:
                                await self._post_engagement_notice(
                                    rec, _RATE_LIMIT_REPLY)
                            except Exception:  # noqa: BLE001 — advisory
                                logger.warning(
                                    "rate-limit notice to topic %s failed",
                                    thread_id, exc_info=True,
                                )
                        return

                # R5 (v0.89.0): record this operator message as the react
                # target — SYNCHRONOUSLY, from the TRUSTED msg.message_id and
                # the authoritative topic/chat identity, BEFORE the first await
                # below. Monotonic (see _record_inbound_target): a delayed older
                # update cannot clobber a newer target. Only ACTIVE records
                # reach here (the status check above returned otherwise).
                self._record_inbound_target(
                    rec.id, chat_id, thread_id,
                    getattr(msg, "message_id", None),
                )

                # #649: ONE mention-aware recognized-command classification,
                # computed here and reused by the dispatch below — a
                # recognized command is consumed by this handler itself
                # (never delivered to the model) and gets no admission
                # ticket; every other text does, HERE, before the clearance
                # clamp / session-invalidation awaits a completion can race.
                recognized = self._classify_engagement_command(text)
                inbound_token = None
                if recognized is None and self._driver_admit_inbound is not None:
                    try:
                        inbound_token = self._driver_admit_inbound(rec, text)
                    except Exception:  # noqa: BLE001
                        logger.debug("admit_inbound failed", exc_info=True)

                # G4 D2 (v0.96.0, Sol code-r2-1): SYNCHRONOUS ingress
                # reservation at TRUE handler entry — before ANY suspension.
                # #649 moved it up past the clearance/invalidation awaits so
                # that contract is finally literal (unchanged semantics
                # otherwise: taken for every text, commands included, and
                # released in the finally for every path that does not hand
                # off delivery, or by _deliver_turn_bg once the spool
                # enqueue resolves).
                inbound_reserved = False
                if self._driver_reserve_inbound is not None:
                    try:
                        # #664: classified at birth — a recognized command's
                        # reservation still vetoes a racing completion but is
                        # excluded from every lost-inbound disclosure (the
                        # command is consumed here, never delivered).
                        inbound_reserved = bool(
                            self._driver_reserve_inbound(
                                rec, command=recognized is not None))
                    except Exception:  # noqa: BLE001
                        logger.debug("reserve_inbound failed", exc_info=True)
                answer_token = None
                handed_off = False
                try:
                    # #336 (Terra, review r4): an engagement reads at the
                    # clearance of the turn that CREATED it — but any member of
                    # the engagement supergroup can steer it by messaging its
                    # topic, and answering a steering turn at the creator's
                    # clearance would hand a non-operator the operator's private
                    # memory through the engagement. Clamp the engagement's
                    # read-clearance DOWN to this sender's before their turn is
                    # delivered, so it never reads above the least-privileged
                    # person who has steered it. Monotonic: it only ever lowers,
                    # so it is safe under concurrent steerers and idempotent for
                    # the ordinary operator-only case (where it no-ops).
                    moved = await self._engagement_registry.lower_origin_clearance(
                        rec.id, self._origin_clearance_for_user_id(user_id))
                    if moved:
                        # #369: the clamp gates future reads, but the live session
                        # still HOLDS what it fetched at the old tier — transcript
                        # and launch-injected archive — and this steerer could
                        # simply ask it to restate them. Tear the session down NOW
                        # (under this same lock, before the turn is delivered) and
                        # durably drop the resume pointer; _resume_and_ready's
                        # rebuild branch establishes the fresh floor-context this
                        # turn will be delivered into. Teardown failure is logged
                        # and does not skip the pointer drop — the persisted
                        # context_rebuild_pending flag (set by the clamp itself)
                        # is what refuses the old session either way.
                        if self._driver_invalidate_session is not None:
                            try:
                                await self._driver_invalidate_session(rec)
                            except Exception:  # noqa: BLE001 — flag fails closed
                                logger.warning(
                                    "engagement %s session invalidation failed "
                                    "after clearance downgrade (rebuild flag "
                                    "still blocks resume)", rec.id[:8],
                                    exc_info=True,
                                )
                        await self._engagement_registry.clear_session_id(rec.id)

                    # v0.79.0 (§3, F2/F5): an inbound operator message is a causal
                    # event, visible on Telegram the instant it arrives. SEAL open
                    # narration + advance the topic high-water at TRUE handler entry
                    # — BEFORE command handling and BEFORE update_user_turn()'s
                    # await. This must precede any command reply (/silent, a rejected
                    # /cancel|/complete) AND any suspension, else a reply or mid-turn
                    # narration could append BELOW the operator's message.
                    if self._driver_advance_high_water is not None:
                        try:
                            await self._driver_advance_high_water(
                                rec, getattr(msg, "message_id", None))
                        except Exception as exc:  # noqa: BLE001 — advisory sealing
                            logger.debug(
                                "advance_high_water failed for %s: %s",
                                rec.id[:8], exc,
                            )

                    # v0.83.0 (§A3, Sol r7-1): set the answered RESERVATION in the
                    # SAME synchronous section as the high-water advance — NO await
                    # between the advance above and this reserve — so a concurrent
                    # result-finalize that observed the answer's high-water also
                    # observes the reservation. ``reserve_answer`` is SYNCHRONOUS.
                    # The reservation counts the oldest unanswered anchor as ANSWERED
                    # (gates/summary/re-anchor) until the message either PROMOTES it
                    # (durable enqueue) or ROLLS it back (every non-delivery path
                    # below: command classification, resume failure, handler
                    # cancellation, spool enqueue rejection). ``handed_off`` transfers
                    # ownership to ``_deliver_turn_bg`` on the delivery path.
                    if self._driver_reserve_answer is not None:
                        try:
                            answer_token = self._driver_reserve_answer(rec)
                        except Exception:  # noqa: BLE001 — reservation is advisory
                            answer_token = None
                    if recognized is not None:
                        # M10 (v0.52.0): group command menus send "/cancel@botname"
                        # — the mention-strip lives in the ONE classification
                        # computed above (#649); a command addressed to a
                        # DIFFERENT bot classified as None and falls through
                        # to the user-turn path, matching PTB's
                        # CommandHandler semantics.
                        command = recognized
                        # Bug 8 (v0.14.6): /cancel and /complete are originator-only.
                        # Pre-fix any user in the supergroup could terminate any
                        # engagement; user_id wasn't even checked. /silent stays
                        # open since it's local to the engagement (anyone reading
                        # along can quiet the observer in their topic). §A3: a
                        # command NEVER becomes a delivered user turn — CAS-roll
                        # back the reservation (Sol r8-1). Ordered AFTER the notice
                        # so the Task-9 seam's re-anchor lands below it (Sol r12-1).
                        if command in ("/cancel", "/complete"):
                            owner_id = rec.origin.get("user_id")
                            if (
                                owner_id is not None
                                and user_id is not None
                                and int(owner_id) != int(user_id)
                            ):
                                # F2: route through the sequencer so this reply
                                # cannot land below open narration.
                                await self._post_engagement_notice(
                                    rec,
                                    f"Only the engagement originator can {command}. "
                                    "Ask them, or start your own engagement.",
                                )
                                await self._rollback_answer(rec, answer_token)
                                return
                            # F2 (whole-branch r2): TERMINAL path — run the
                            # finalizer FIRST, then gate the re-anchor SUPPRESSION
                            # on finalize SUCCESS. On success, finalize's
                            # settle_all_open_questions already owns + settles
                            # every open anchor (it never reads the reservation,
                            # so a still-held reservation during finalize is
                            # inert), and the suppressed rollback just clears the
                            # reservation — no redundant re-anchor copy. On a
                            # FAILED strict terminal transition (finalize returns
                            # False → engagement stays live, nothing settled), the
                            # rollback runs WITHOUT suppression so the fourth-
                            # consumer re-anchor moves the stranded live anchor
                            # below the command instead of orphaning it above.
                            # The originator-rejected + /silent rollbacks above are
                            # NON-terminal and keep re-anchoring unconditionally.
                            if command == "/cancel":
                                finalized = await self._finalize_cancel(
                                    rec, reason="user")
                            else:
                                finalized = await self._finalize_complete_user(rec)
                            # #649: a falsy finalize whose record is STILL
                            # LIVE means the strict terminal persist rolled
                            # back (or the stale-guard lost) — the operator's
                            # command would otherwise be silently ignored
                            # and a later agent completion could proceed.
                            # (Already-terminal records re-read as terminal
                            # and stay silent, as before.) Posted BEFORE the
                            # rollback so a re-anchored question still lands
                            # below it (Sol r12-1 wire ordering).
                            if not finalized:
                                latest = (
                                    self._engagement_registry.get(rec.id)
                                    if self._engagement_registry is not None
                                    else None)
                                if latest is not None and latest.status in (
                                        "active", "idle"):
                                    try:
                                        await self._post_engagement_notice(
                                            rec,
                                            f"{command} could not be "
                                            "finalized — please retry.")
                                    except Exception:  # noqa: BLE001
                                        logger.debug(
                                            "command retry notice failed",
                                            exc_info=True)
                            await self._rollback_answer(
                                rec, answer_token,
                                suppress_reanchor=bool(finalized))
                            return
                        if command == "/silent":
                            if self._observer is not None:
                                self._observer.silence(rec.id)
                            # F2: route through the sequencer (single writer).
                            await self._post_engagement_notice(
                                rec, "Observer quieted for this engagement.",
                            )
                            await self._rollback_answer(rec, answer_token)
                            return

                    # Resume-if-suspended + lifecycle gate + idle→active turn
                    # stamp — the SINGLE shared core (``_resume_and_ready``),
                    # run UNDER this per-topic lock. A False return means the
                    # record went terminal, resume was exhausted (2-strike), or
                    # it is an unresumable orphan (each already surfaced +
                    # cleaned up inside the helper); do NOT deliver — the finally
                    # rolls the answered/inbound reservations back. The status
                    # re-check inside still runs under the lock, preserving the
                    # Bug-10 guarantee (a cancel that already finalised the
                    # driver blanks the turn here before any task is spawned).
                    # v0.79.0 (§3, F2/F5): the high-water advance + narration
                    # seal happened at TRUE handler entry above (before command
                    # handling and any suspension).
                    if not await self._resume_and_ready(rec):
                        return

                    # M9 (v0.52.0): deliver the user turn in a tracked background
                    # task so the per-topic lock (and, in polling mode, PTB's
                    # update fetcher) is NOT held across the whole multi-minute
                    # SDK turn — a subsequent /cancel can then acquire the lock
                    # and interrupt the in-flight turn.
                    if self._driver_send_user_turn is not None:
                        if self._stopping:
                            # #649 producer-closing stop gate: no delivery
                            # task after stop() began — one bounded telling,
                            # then the finally's discharge no-ops.
                            await self._settle_lost_inbound(rec, inbound_token)
                            return
                        # #649: belt-and-suspenders late admission — any
                        # text reaching delivery without a ticket (seam not
                        # wired, or a future path) is admitted before the
                        # task exists.
                        if (inbound_token is None
                                and self._driver_admit_inbound is not None):
                            try:
                                inbound_token = self._driver_admit_inbound(
                                    rec, text)
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "late admit_inbound failed", exc_info=True)
                        # §A3: the message becomes a delivered user turn — TRANSFER
                        # reservation ownership to the background task, which
                        # promotes (accepted) or CAS-rolls-back (rejected) per the
                        # enqueue disposition. ``handed_off`` stops the finally
                        # below from clobbering the in-flight reservation.
                        # #649 (Terra seam r3): set ONLY after the task exists
                        # and both callbacks are attached — a create_task
                        # raise otherwise skipped every rollback with no
                        # owner in existence.
                        task = asyncio.create_task(
                            self._deliver_turn_bg(
                                rec, text,
                                tg_message_id=getattr(msg, "message_id", None),
                                answer_token=answer_token,
                                inbound_reserved=inbound_reserved,
                                inbound_token=inbound_token))
                        self._turn_tasks.add(task)
                        task.add_done_callback(self._turn_tasks.discard)
                        # Task-end backstop for cancelled-before-first-step.
                        task.add_done_callback(
                            lambda t, r=rec, tok=inbound_token:
                                self._schedule_inbound_cleanup(r, tok))
                        handed_off = True
                    return
                except BaseException:
                    # #649 visibility law: an exception/cancellation before
                    # hand-off leaves the ticket with no task owner — one
                    # bounded notice attempt while it is still ledgered,
                    # then discharge (the finally's discharge is the
                    # idempotent second half).
                    if inbound_token is not None and not handed_off:
                        await self._settle_lost_inbound(rec, inbound_token)
                    raise
                finally:
                    # §A3 (Sol r8-1): every path that did NOT hand the reservation
                    # to the delivery task — command classification, resume
                    # failure/orphan, handler CANCELLATION — CAS-rolls it back.
                    # The CAS no-ops if a command branch already rolled it back,
                    # if promotion consumed it, or if a later message re-reserved.
                    if not handed_off:
                        await self._rollback_answer(rec, answer_token)
                        # G4 D2: a path that never handed off the delivery
                        # task must release the ingress reservation too —
                        # a leak here would refuse completion forever.
                        if inbound_reserved and (
                                self._driver_release_inbound is not None):
                            try:
                                # #664: release with the kind it was born
                                # with, so the command sub-counter pairs.
                                self._driver_release_inbound(
                                    rec, command=recognized is not None)
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "release_inbound failed", exc_info=True)
                        # #649: expected-non-delivery paths (a losing
                        # _resume_and_ready, the stop gate) discharge the
                        # ticket directly; exception paths already made
                        # their telling above. Idempotent by exact token.
                        if (inbound_token is not None
                                and self._driver_discharge_inbound is not None):
                            try:
                                self._driver_discharge_inbound(
                                    rec, inbound_token)
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "inbound discharge failed", exc_info=True)

        # 3) Other chats. When telegram_chat_id is configured it is an
        #    allowlist (DOCS.md: "Telegram chat ID to restrict messages
        #    to. Leave empty to accept all chats.") — drop updates from
        #    any other chat. Empty chat_id = accept all (documented opt-in).
        #    Route 1 (the configured DM) and route 2 (the engagement
        #    supergroup + its topics) already returned above, so reaching
        #    here means this chat is neither.
        if str(self.chat_id or "").strip():
            logger.info(
                "Dropping Telegram update from unauthorized chat_id=%s "
                "(user_id=%s); telegram_chat_id restriction active",
                chat_id, user_id,
            )
            return
        return await self._route_to_ellen(update)

    async def _rollback_answer(
        self, rec, token, *, suppress_reanchor: bool = False,
    ) -> None:
        """§A3: best-effort CAS rollback of an answered reservation (a no-op when
        unwired, the token is None, or the CAS loses ownership). Swallows its own
        errors so no non-delivery path is aborted by a rollback failure.

        F2 (whole-branch gate): TERMINAL command paths (``/cancel``/``/complete``)
        pass ``suppress_reanchor=True`` because engagement finalize follows and
        its ``settle_all_open_questions`` owns the open anchors — re-anchoring
        here would post a redundant copy that terminal settlement instantly
        settles."""
        if token is None or self._driver_rollback_answer_reservation is None:
            return
        try:
            await self._driver_rollback_answer_reservation(
                rec, token, suppress_reanchor=suppress_reanchor)
        except Exception:  # noqa: BLE001 — rollback is advisory
            logger.debug("rollback_answer_reservation failed for %s",
                         rec.id[:8], exc_info=True)

    def _classify_engagement_command(self, text: str) -> str | None:
        """#649: THE one mention-aware recognized-command classification,
        computed once per update and reused by admission accounting AND the
        command dispatch below — never re-derived (a second, slightly
        different predicate is how a borrowed predicate answers a different
        question). Returns '/cancel' | '/complete' | '/silent' | None; a
        command explicitly addressed to a DIFFERENT bot is None (it falls
        through to delivery, exactly as the historical dispatch treated it)."""
        if not text.startswith("/"):
            return None
        token = text.split()[0].lower()
        command, _, mention = token.partition("@")
        if mention and self._bot_username and mention != self._bot_username:
            return None  # addressed to another bot — not ours
        return command if command in ("/cancel", "/complete", "/silent") else None

    async def _settle_lost_inbound(self, rec, token) -> None:
        """#649 visibility law: before a still-ledgered admission ticket is
        discharged on a failure path, make ONE bounded, guarded notice
        attempt — never gated on any record-status read (no status, settled
        or not, proves the terminal snapshot ran; a funnel terminal already
        closed the topic, so the attempt fails harmlessly there and the
        intentional silences survive de facto). One attempt, then discharge
        regardless: retry-until-posted would be an UNCLEARABLE completion
        veto (the INV-ENG-003 livelock class)."""
        if token is None or self._driver_discharge_inbound is None:
            return
        try:
            held = (self._driver_inbound_held(rec, token)
                    if self._driver_inbound_held is not None else False)
            if held:
                try:
                    await asyncio.wait_for(self._post_engagement_notice(
                        rec, "Your message could not be delivered — "
                             "please resend it."), 5)
                except BaseException:  # noqa: BLE001 — bounded best-effort
                    logger.debug("lost-inbound notice failed for %s",
                                 rec.id[:8], exc_info=True)
        finally:
            try:
                self._driver_discharge_inbound(rec, token)
            except Exception:  # noqa: BLE001
                logger.debug("inbound discharge failed", exc_info=True)

    def _schedule_inbound_cleanup(self, rec, token) -> None:
        """#649: task-end backstop, attached as a done_callback at both
        spawn sites. On every normal path the ticket is already discharged
        (held=False → pure no-op); its real work is the
        cancelled-before-first-step task, whose coroutine never ran any
        try/finally. Scheduled UNCONDITIONALLY (stop() included) and
        tracked, so the shutdown drain awaits it."""
        if token is None:
            return
        try:
            t = asyncio.create_task(self._settle_lost_inbound(rec, token))
        except RuntimeError:  # loop closing — process death; non-durable
            return
        self._inbound_cleanup_tasks.add(t)
        t.add_done_callback(self._inbound_cleanup_tasks.discard)

    async def _report_incomplete_turn(self, rec, token) -> None:
        """#692/#678: tell the operator when THEIR turn ended without the
        turn's own terminal artifact and the engagement is still live.

        Called on ``_deliver_turn_bg``'s SUCCESS path only. Every exception
        path already has an owner — the "Turn failed" notice, the terminal
        absorb branch, or ``_settle_lost_inbound`` — and adding a telling
        there too would be a second telling for one turn.

        Three properties carry this:

        1. **One bounded attempt, never retried.** Both awaits are bounded
           independently (D21); a retry-until-posted here would be an
           unclearable completion veto.
        2. **The suppression reads the SETTLED record, not a lock-free
           snapshot.** A healthy in_casa self-emit completion reaches here
           looking exactly like a cutoff: ``emit_completion`` commits the
           terminal transition and ``_finalize_engagement_tail`` then closes
           the engagement's own SDK client, so the turn ends with no
           ``ResultMessage``. The flip and the funnel's topic work both
           precede that close, so a settled read sees them and this stays
           quiet; a LOCK-FREE read could instead see the transient value of a
           strict transition that then rolls back, and would swallow a notice
           that was owed.
        3. **It is an ATTEMPT, not an ordering guarantee.** Nothing orders an
           in_casa notice against the finalize funnel's topic operations, so a
           terminal writer that wins the flip after the read above can paint
           and close first and this notice lands below the paint, or fails
           against a closed topic. Installing that ordering needs the
           in_casa turn-admission fence that does not exist (INV-ENG-009 is
           claude_code-only) and is deliberately out of scope here. The
           invariant is written as an attempt for that reason.
        4. **R2: terminal STATUS is not the suppression predicate — a
           CONFIRMED terminal telling is.** A seam reviewer reproduced the
           composition this change would otherwise create with its own other
           arm: the completion summary fails, the funnel's one bounded
           disclosure fails too, the tail's ``driver.cancel`` then ends this
           turn's iterator, and a status-only reader sees ``completed`` and
           stays quiet — a terminal record over a topic that heard NOTHING,
           and zero notices. So the read asks the whole question at once
           (``settled_terminal_state``), and every way of NOT knowing that the
           topic was told — no record of one, a read that raises, a read that
           times out — falls through to the one attempt. The harm being fixed
           is silence, so that is the direction to fail in.
        """
        # No ``token is None`` half: the seam is total over a ticket it does
        # not hold and returns "" for one, so the guard would be unpinnable
        # (see casa_core.read_followup_incomplete). The seam-wired check IS
        # load-bearing — an unwired channel has no seam to call.
        if self._driver_turn_incomplete is None:
            return
        try:
            reason = self._driver_turn_incomplete(rec, token)
        except Exception:  # noqa: BLE001 — an observation read is never fatal
            logger.debug("follow-up observation read failed for %s",
                         rec.id[:8], exc_info=True)
            return
        if not reason:
            return
        status, told = "", False
        if self._engagement_registry is not None:
            try:
                status, told = await asyncio.wait_for(
                    self._engagement_registry.settled_terminal_state(rec.id),
                    _SETTLED_STATUS_TIMEOUT_S)
            except BaseException:  # noqa: BLE001 — fail TOWARD telling
                status, told = "", False
                logger.warning(
                    "settled terminal-state read did not return for %s — "
                    "telling the operator anyway", rec.id[:8], exc_info=True)
        if status in ("completed", "cancelled", "error") and told:
            logger.info(
                "turn %s ended without its result frame on an already-terminal "
                "engagement (%s) whose terminal path DID tell this topic — it "
                "owns the telling", rec.id[:8], status)
            return
        terminal_untold = status in ("completed", "cancelled", "error")
        if terminal_untold:
            logger.warning(
                "turn %s ended without its result frame on a terminal "
                "engagement (%s) whose terminal path told this topic nothing "
                "— telling the operator", rec.id[:8], status)
        logger.warning(
            "engagement %s: a follow-up turn ended without its result frame "
            "over a live record (reason=%s) — telling the operator",
            rec.id[:8], reason)
        # WHICH telling depends on the state, and the two are not
        # interchangeable: over a LIVE record the turn really was cut off, and
        # over a TERMINAL one it ended because it completed the engagement,
        # whose account may in fact be on the operator's screen already (a lost
        # acknowledgement is indistinguishable from a failed send from here).
        try:
            await asyncio.wait_for(
                self._post_engagement_notice(
                    rec,
                    _TURN_ENDED_UNCONFIRMED_NOTICE if terminal_untold
                    else _TURN_CUT_OFF_NOTICE),
                _TURN_INCOMPLETE_NOTICE_TIMEOUT_S)
        except BaseException:  # noqa: BLE001 — one bounded best-effort attempt
            logger.warning("incomplete-turn notice failed for %s",
                           rec.id[:8], exc_info=True)

    async def _deliver_turn_bg(
        self, rec, text: str, *, tg_message_id: int | None = None,
        answer_token: str | None = None,
        inbound_reserved: bool = False,
        inbound_token: object | None = None,
    ) -> None:
        """M9 (v0.52.0): run one engagement user-turn to completion.

        Spawned by handle_update as a tracked background task so the
        per-topic lock is released as soon as the turn is dispatched. If a
        concurrent /cancel finalised the driver mid-turn, send_user_turn
        raises (DriverNotAliveError / transport-closed) and we stay quiet —
        that is the expected end of an interrupted turn, not a failure.

        #692: staying quiet is right only when the turn RAISED. A turn cut off
        mid-tool-loop RETURNS NORMALLY — the SDK's response iterator simply
        ends without a ResultMessage — so the "Turn failed" notice below,
        which lives inside ``except Exception``, could never fire for it and
        the operator's message was consumed and answered with nothing. The
        success path therefore asks ``_report_incomplete_turn``, which is that
        shape's owner.

        v0.79.0 (§3): ``tg_message_id`` threads the durable inbound envelope to
        the operator's Telegram message (reply-quoting / receipts).

        v0.83.0 (§A3, Sol r9-1/r10-2): this task OWNS the answered reservation
        after hand-off. The enqueue DISPOSITION drives its fate — an ACCEPTED
        enqueue (``queued``/``evicted_other``) already PROMOTED (consumed) the
        reservation inside ``enqueue``; a REJECTED enqueue (``dropped_full`` /
        ``error``) or a raised/cancelled delivery CAS-rolls it back so the
        question stops being treated as answered.
        """
        def _release_inbound():
            # G4 D2: the reservation ends when the enqueue RESOLVED either
            # way — an accepted enqueue transfers "unread" accounting to the
            # spool (state=queued); a rejected/raised one is gone.
            nonlocal inbound_reserved
            if inbound_reserved and self._driver_release_inbound is not None:
                inbound_reserved = False
                try:
                    self._driver_release_inbound(rec)
                except Exception:  # noqa: BLE001
                    logger.debug("release_inbound failed", exc_info=True)

        # #369: a just-rebuilt context gets its preamble (context-reset note +
        # floor-refetched archive) prepended to the first delivered turn.
        preamble = self._rebuild_preambles.pop(rec.id, None)
        if preamble:
            text = f"{preamble}\n\n{text}"
        try:
            if inbound_token is not None:
                disposition = await self._driver_send_user_turn(
                    rec, text, tg_message_id=tg_message_id,
                    inbound_token=inbound_token)
            else:
                disposition = await self._driver_send_user_turn(
                    rec, text, tg_message_id=tg_message_id)
            _release_inbound()
            # #649: on the success path the driver already discharged the
            # ticket at its evidence frame — this is the idempotent safety.
            if (inbound_token is not None
                    and self._driver_discharge_inbound is not None):
                self._driver_discharge_inbound(rec, inbound_token)
            # #692: the turn RETURNED, which is not the same as the turn
            # having finished. Ask the driver whether this ticket's turn left
            # the turn's own terminal artifact, and tell the operator if it
            # did not. After the discharge above, so the ledger is in its
            # final state either way.
            await self._report_incomplete_turn(rec, inbound_token)
        except asyncio.CancelledError:
            _release_inbound()
            # Cancelled before a durable enqueue could promote — roll back.
            # #649 visibility law: one bounded notice attempt while the
            # ticket is still held, then discharge (guarded — a shutdown
            # cancellation's notice may fail; that is the bounded attempt).
            await self._settle_lost_inbound(rec, inbound_token)
            await self._rollback_answer(rec, answer_token)
            raise
        except Exception as exc:  # noqa: BLE001
            _release_inbound()
            # The enqueue raised (never durably spooled) — the reservation must
            # roll back so the answer-lifecycle isn't left falsely promoted.
            latest = (
                self._engagement_registry.get(rec.id)
                if self._engagement_registry is not None else None
            )
            if latest is not None and latest.status in (
                "completed", "cancelled", "error",
            ):
                await self._rollback_answer(rec, answer_token)
                logger.info(
                    "turn delivery ended by finalize for %s: %s",
                    rec.id[:8], exc,
                )
                # #649: never infer disclosure from a status read — a
                # transiently-terminal strict persist can roll back live.
                # The settle attempt targets a closed topic on a GENUINE
                # terminal (whose flip snapshot disclosed the held ticket)
                # and fails harmlessly; on a fake one it posts.
                await self._settle_lost_inbound(rec, inbound_token)
                return
            logger.warning("turn delivery failed for %s: %s", rec.id[:8], exc)
            try:
                # F2 sweep (Sol r3): route through the sequencer (single writer)
                # so this failure notice can't land below open narration.
                await self._post_engagement_notice(rec, f"Turn failed: {exc}")
            except Exception:  # noqa: BLE001 — best-effort user notice
                pass
            finally:
                # M4 (§A3 wave 2): a CancelledError while AWAITING the notice
                # bypasses ``except Exception`` — without a finally the rollback
                # below never runs and the answered reservation LEAKS (the anchor
                # stays invisibly 'answered'). Guarantee the rollback here so it
                # runs on both the normal and the cancelled paths; a CancelledError
                # still propagates after. Sol r12-1 ordering preserved: rollback
                # runs AFTER the notice attempt, so a rollback consumer's re-posted
                # question lands BELOW the notice.
                await self._rollback_answer(rec, answer_token)
                # #649: the "Turn failed" notice above WAS this delivery's
                # one visible-outcome attempt — discharge follows it.
                if (inbound_token is not None
                        and self._driver_discharge_inbound is not None):
                    try:
                        self._driver_discharge_inbound(rec, inbound_token)
                    except Exception:  # noqa: BLE001
                        logger.debug("inbound discharge failed", exc_info=True)
            return
        # §A3: a durable-enqueue REJECTION (capacity drop / spool-write error)
        # rolls the reservation back; an accepted enqueue already promoted it.
        if disposition in ("dropped_full", "error"):
            await self._rollback_answer(rec, answer_token)

    async def _resume_and_ready(self, rec) -> bool:
        """Resume-if-suspended + lifecycle gate for one engagement turn, run
        UNDER the per-topic handler lock (``_engagement_handler_locks[topic]``).
        Returns True when ``rec`` is live and deliverable; False when the caller
        must NOT deliver — a terminal record, resume exhausted (2-strike), or an
        unresumable orphan (each already surfaced + cleaned up here).

        Bug 10 / v0.65.0 / §A3: this is the SINGLE resume+lifecycle core shared
        by the operator-message path (``handle_update``) and the system-turn
        path (``deliver_system_turn`` — v0.102.0 #217 post-consent auto-resume).
        Keeping ONE copy under ONE lock is what stops a manual nudge and a
        system resume from both observing a suspended in_casa driver and
        double-opening its ``ClaudeSDKClient`` / lock. Operator-message-specific
        bookkeeping (answered/inbound reservations, reply-threading, the finally
        rollback) stays in ``handle_update`` AROUND this call; only the
        resume/lifecycle core lives here."""
        # Re-resolve under the lock: a driver-side result-finalize can flip the
        # record terminal between handler entry and here. Only active/idle
        # records are deliverable; a terminal one aborts before any resume.
        reg = self._engagement_registry
        if reg is not None:
            latest = reg.get(rec.id)
            if latest is None or latest.status not in ("active", "idle"):
                return False
            rec = latest

        # #369: a clearance downgrade marked this engagement's context for
        # rebuild — REFUSE to resume the recorded session (it holds transcript
        # + archive fetched above the record's clamped floor) and establish a
        # fresh one at the current markers instead. Runs before the resume
        # block below so the stale sid is never handed to the driver; ordered
        # for every caller (steering turn, system turn, post-crash next turn).
        # Fail closed: if the rebuild cannot be established, the turn is not
        # delivered and the flag stays set for the next attempt.
        if getattr(rec, "context_rebuild_pending", False):
            if self._engagement_context_rebuilder is None:
                logger.warning(
                    "engagement %s needs a context rebuild but no rebuilder "
                    "is wired — refusing delivery", rec.id[:8])
                return False
            try:
                preamble = await self._engagement_context_rebuilder(rec)
            except Exception as exc:  # noqa: BLE001 — fail closed, retry next turn
                logger.warning(
                    "engagement %s context rebuild failed: %s — refusing "
                    "delivery (will retry on the next turn)", rec.id[:8], exc,
                )
                await self._post_engagement_notice(
                    rec, "This engagement is re-establishing its context — "
                         "please resend in a moment.")
                return False
            if preamble:
                self._rebuild_preambles[rec.id] = preamble
            if reg is not None:
                await reg.clear_context_rebuild_pending(rec.id)
            else:
                rec.context_rebuild_pending = False

        # Resume suspended client if needed (in_casa driver only — the
        # claude_code driver has s6 keeping the subprocess alive across Casa
        # restarts, and its engagements never have sdk_session_id).
        if rec.driver != "claude_code" and self._engagement_driver is not None:
            drv = self._engagement_driver
            if not drv.is_alive(rec) and rec.sdk_session_id:
                fail_count = rec.origin.get("_resume_fail_count", 0)
                try:
                    await drv.resume(rec, rec.sdk_session_id)
                    # #326: persist the reset so a pre-restart strike cannot
                    # combine with a later one across reboots. Skip the write
                    # when the counter was already clean (the common path).
                    if fail_count and reg is not None:
                        await reg.set_resume_fail_count(rec.id, 0)
                    else:
                        rec.origin["_resume_fail_count"] = 0
                except Exception as exc:  # noqa: BLE001
                    fail_count += 1
                    # #326: durable two-strike counter — without persistence a
                    # restart reset it and an unrecoverable engagement dodged
                    # the error transition forever.
                    if reg is not None:
                        await reg.set_resume_fail_count(rec.id, fail_count)
                    else:
                        rec.origin["_resume_fail_count"] = fail_count
                    logger.warning(
                        "resume failed (%d/2) for engagement %s: %s",
                        fail_count, rec.id[:8], exc,
                    )
                    won_error = reg is None
                    if fail_count >= 2 and reg is not None:
                        # #326: mark_error reports whether THIS call won the
                        # terminal transition — a /cancel that finalized during
                        # the resume await keeps its status, and the loser must
                        # not run duplicate topic cleanup.
                        won_error = await reg.mark_error(
                            rec.id, kind="resume_failed", message=str(exc),
                        )
                    await self.send_to_topic(
                        rec.topic_id,
                        f"Could not resume this engagement: {exc}. "
                        f"Start a fresh one if needed.",
                    )
                    if fail_count >= 2 and won_error:
                        # [AR-1] (v0.65.0): this terminal path bypasses finalize
                        # — title-mark, close + ledger the topic here
                        # (best-effort, never raises). After the notice: posting
                        # into a just-closed topic works only while the bot keeps
                        # can_manage_topics — mirror the funnel's send-then-close
                        # order.
                        await self._cleanup_error_topic(rec)
                    return False
            elif not drv.is_alive(rec):
                # No session to resume — orphan.
                if reg is not None:
                    await reg.mark_error(
                        rec.id, kind="orphan_no_session",
                        message="no sdk_session_id to resume with",
                    )
                await self.send_to_topic(
                    rec.topic_id, "This engagement can't be resumed.",
                )
                # [AR-1] (v0.65.0): terminal path bypasses finalize — see above.
                await self._cleanup_error_topic(rec)
                return False

        # v0.79.0 (§3): stamp the idle→active user turn so idle-reminder +
        # retention accounting see fresh activity before delivery.
        if reg is not None:
            import time as _time
            await reg.update_user_turn(rec.id, _time.time())
        return True

    async def _dispatch_engagement_continuation(
        self, *, engagement_id: str, text: str,
    ) -> bool:
        """#400: resume a SPECIALIST ENGAGEMENT after an operator authorized (or
        denied) a protected plugin tool over the DM authorization keyboard.

        The authz finish hook (``authz_grants._make_finish_hook``) calls this
        instead of ``_dispatch_button_continuation`` when the challenge was
        engagement-origin. It looks up the record by id and hands the
        ``[authorization approved|denied]`` turn to the SAME
        ``deliver_system_turn`` seam the post-consent auto-resume uses (#217) —
        which acquires the per-topic lock, runs the shared ``_resume_and_ready``
        gate (fail-closed on a terminal / unresumable / idle-reaped engagement),
        and spawns the turn as a tracked background task so this callback never
        blocks for the turn's duration.

        Returns ``True`` when the turn was handed off, ``False`` when the
        engagement is unknown (the finish hook then surfaces a delivery-failed
        note on the DM keyboard). A record that resolves but is non-deliverable
        is handled inside ``deliver_system_turn`` (it logs + returns), which is
        still a successful hand-off from this seam's perspective — the grant was
        minted and the operator saw the approval; a re-nudge resumes it."""
        reg = self._engagement_registry
        rec = reg.get(engagement_id) if reg is not None else None
        if rec is None:
            logger.info(
                "authz continuation: engagement %s unknown — cannot resume",
                str(engagement_id)[:8],
            )
            return False
        await self.deliver_system_turn(rec, text)
        return True

    async def deliver_system_turn(self, rec, text: str) -> None:
        """Resume-if-suspended (under the per-topic lock), then deliver a
        synthetic system-authored turn to engagement ``rec`` — the seam a
        background reconcile callback uses to PROCEED a paused configurator
        engagement after an operator tap (v0.102.0 #217: post-consent install
        auto-resume).

        Acquires the SAME ``_engagement_handler_locks[rec.topic_id]`` lock the
        operator-message path holds, so a concurrent manual nudge and this
        system turn can never both resume a suspended in_casa driver (no
        duplicate ``ClaudeSDKClient``/parallel recipe). Inside the lock it runs
        the shared ``_resume_and_ready`` gate — abandoning delivery (False) for
        a terminal/unresumable engagement, which the helper already surfaced +
        cleaned up — then hands the turn to the SAME ``_deliver_turn_bg`` task
        the manual path uses, with the operator-message kwargs at their no-op
        defaults (a system turn has no operator message, reply-thread, or answer
        to carry). The turn is spawned as a tracked background task (in_casa
        turns stream the whole recipe to completion), so the lock is released as
        soon as the resume + dispatch is done — this never blocks the caller (a
        tap-callback finish hook) for the turn's duration."""
        # #649: admit the continuation SYNCHRONOUSLY at entry, before the
        # topic-lock wait and every _resume_and_ready await — a completion
        # racing any of those suspensions must already see it as unread.
        inbound_token = (self._driver_admit_inbound(rec, text)
                         if self._driver_admit_inbound is not None else None)
        handed_off = False
        try:
            lock = self._engagement_handler_locks.setdefault(
                rec.topic_id, asyncio.Lock())
            async with lock:
                if not await self._resume_and_ready(rec):
                    logger.info(
                        "post-consent auto-resume skipped for engagement %s — "
                        "not deliverable (terminal/unresumable); operator can "
                        "re-nudge", rec.id[:8])
                    return
                if self._stopping:
                    # #649 producer-closing stop gate: no new delivery task
                    # after stop() began; the failure owner's settle below
                    # makes the drop visible (bounded, best-effort).
                    await self._settle_lost_inbound(rec, inbound_token)
                    inbound_token = None
                    return
                task = asyncio.create_task(self._deliver_turn_bg(
                    rec, text, inbound_token=inbound_token))
                self._turn_tasks.add(task)
                task.add_done_callback(self._turn_tasks.discard)
                # Task-end backstop for a cancelled-before-first-step task.
                task.add_done_callback(
                    lambda t, r=rec, tok=inbound_token:
                        self._schedule_inbound_cleanup(r, tok))
                handed_off = True
        except BaseException:
            # Raise/cancellation before hand-off (e.g. inside
            # _resume_and_ready's strict writes): the ticket has no task
            # owner — apply the visibility law here, then re-raise.
            if inbound_token is not None and not handed_off:
                await self._settle_lost_inbound(rec, inbound_token)
                inbound_token = None
            raise
        finally:
            if (not handed_off and inbound_token is not None
                    and self._driver_discharge_inbound is not None):
                # Expected-False path (_resume_and_ready surfaced/cleaned
                # up) — discharge directly, no second telling.
                try:
                    self._driver_discharge_inbound(rec, inbound_token)
                except Exception:  # noqa: BLE001
                    logger.debug("inbound discharge failed", exc_info=True)

    async def _maybe_redirect_main_feed(self, user_id: int | None) -> None:
        if user_id is None:
            return
        if user_id in self._main_feed_redirect_seen:
            return
        self._main_feed_redirect_seen.add(user_id)
        try:
            await self.bot.send_message(
                chat_id=self.engagement_supergroup_id,
                text=(
                    "This supergroup is for engagement topics. Start one by "
                    "talking to me in our DM."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("main-feed redirect send failed: %s", exc)

    # ------------------------------------------------------------------
    # E-12 (v0.37.0) Task 20 / v0.75.0 (W5/Sol B3,B4): inline-keyboard
    # callback dispatch — fail-closed contract, single `cq.answer()`.
    # ------------------------------------------------------------------

    async def _on_inline_callback(
        self, update: Any, context: Any = None,
    ) -> None:
        """Dispatch inline-keyboard callbacks (permission / ask verdicts, …).

        v0.75.0 [D:§W5 callback, Sol B4/B5/r2-B3/r3-B5/r7-B2/B6]: EXACTLY ONE
        ``await cq.answer(toast)`` per path (via :func:`_safe_answer`, which
        swallows a transport failure so it never crashes the handler). Order:
        parse ``v1|ns|rid|idx`` (or the legacy ``perm:<allow|deny>:<rid>``
        shape) FIRST; ``cq.from_user is None`` fails closed; resolve the
        engagement from the topic; reject a TERMINAL record BEFORE claiming
        (r7-B6 — closes the window where ``try_transition_terminal`` has
        flipped status but is still awaiting tombstone I/O); look up the
        broker request's static meta; reject wrong-topic / out-of-range
        option / wrong-actor WITHOUT claiming; only then claim + (for
        ``engagement_ask``) persist the state advance BEFORE committing, so
        the awaiting handler never resumes un-authorized.

        This handler NEVER edits the keyboard — that is owned exclusively by
        the broker finish-hook set at post time (r3-B3), which fires once on
        outcome even if the posting hook task was cancelled.
        """
        cq = update.callback_query
        data = cq.data or ""
        ns, request_id, idx, kind = _parse_callback_data(data)
        if ns is None:
            await _safe_answer(cq, "expired")
            return

        # v0.76.0 (W5b, r1-B2): resident_ask is DM-scoped (dm:/authz:), NOT
        # topic-scoped — it is handled fully by its own single-owner branch
        # BEFORE any engagement/topic lookup and returns.
        if ns == "resident_ask":
            await self._on_resident_callback(cq, request_id, idx)
            return

        if cq.from_user is None:
            # r3-B5: fail closed — an anonymous/missing actor can never
            # authorize a verdict.
            await _safe_answer(cq, "expired")
            return

        thread_id = getattr(cq.message, "message_thread_id", None)
        if thread_id is None or self._engagement_registry is None:
            await _safe_answer(cq, "expired")
            return
        rec = self._engagement_registry.by_topic_id(thread_id)
        if rec is None:
            await _safe_answer(cq, "expired")
            return
        # r7-B6: reject a TERMINAL record BEFORE claiming — closes the
        # window where try_transition_terminal has flipped rec.status but is
        # still awaiting tombstone I/O before _finalize_engagement reaches
        # cancel_scope.
        if getattr(rec, "status", None) not in ("active", "idle"):
            await _safe_answer(cq, "expired")
            return

        from verdict_broker import BROKER

        meta = BROKER.get_meta(namespace=ns, scope=rec.id, request_id=request_id)
        if meta is None:
            await _safe_answer(cq, "expired")
            return
        if meta.get("topic_id") != thread_id:
            await _safe_answer(cq, "expired")
            return
        options = meta.get("options") or []
        # A5 · F-MULTI: a SUBMIT tap carries no option index (``idx is None``);
        # the range check applies only to single-select + toggle taps.
        if kind != "submit" and idx not in range(len(options)):
            await _safe_answer(cq, "invalid")
            return

        # Fail closed on actor (r3-B5/r7-B2): no operator_id on record, or a
        # tap from someone other than the bound operator, is refused WITHOUT
        # claiming — a late/wrong tap must never consume the live request.
        expected = meta.get("operator_id")
        if expected is None or cq.from_user.id != expected:
            await _safe_answer(cq, "not for you")
            return

        # A5 · F-MULTI TOGGLE: broker-owned atomic flip on a LIVE, UNCLAIMED
        # request, then a revalidated markup-only keyboard redraw through the
        # sequencer's ``edit_discrete`` (§A9). This handler NEVER resolves the
        # request — only the finish hook (submit / timeout / supersede) settles.
        if kind == "toggle":
            selected = BROKER.toggle_selection(
                namespace=ns, scope=rec.id, request_id=request_id, idx=idx)
            if selected is None:
                await _safe_answer(cq, "expired")
                return
            await self._redraw_multi_keyboard(
                rec.id, request_id, meta, options, selected)
            await _safe_answer(
                cq, "added" if idx in selected else "removed")
            return

        # A5 · F-MULTI SUBMIT: require ≥1 selection, then claim carrying the full
        # selection (``option_index`` = the FIRST selected index for compat).
        option_indices: "list[int] | None" = None
        claim_index = idx
        if kind == "submit":
            selected = sorted(set(meta.get("selected") or ()))
            if not selected:
                await _safe_answer(cq, "pick at least one")
                return
            option_indices = selected
            claim_index = selected[0]

        claim = BROKER.claim(
            namespace=ns, scope=rec.id, request_id=request_id,
            option_index=claim_index, actor_id=cq.from_user.id,
            option_indices=option_indices,
        )
        if isinstance(claim, str):
            # Non-winning: a late tap on an already-retired keyboard never
            # authorizes anything (no state change).
            await _safe_answer(
                cq, {"duplicate": "already answered", "stale": "expired",
                     "forbidden": "not for you"}[claim],
            )
            return

        committed = False
        advanced = False
        try:
            if ns == "engagement_ask":
                # r2-B7/r3-B4: persist `authorized` under the registry lock
                # BEFORE the awaiting handler resumes. r7-B1 (strict):
                # advance_interaction_state doesn't exist until Task 7 — a
                # registry lacking it takes the no-op skip-to-commit path
                # (Task 7 activates the guard automatically once it lands).
                advance = getattr(
                    self._engagement_registry, "advance_interaction_state", None,
                )
                if advance is not None:
                    try:
                        await advance(rec.id, "operator_answered")
                        advanced = True
                    except asyncio.CancelledError:
                        # B4 (Sol diff r2): advance SHIELDS its mutate+persist,
                        # so by the time it re-raises CancelledError the durable
                        # write has RESOLVED. If it authorized durably (state
                        # now "authorized"), mark `advanced` so the finally
                        # COMMITS rather than re-arming a request that would
                        # later expire no_answer DESPITE disk authorization. A
                        # rolled-back persist failure leaves the state != authorized
                        # → fall through to abort. Then honor the cancellation.
                        if getattr(
                            rec, "interaction_state", "",
                        ) == "authorized":
                            advanced = True
                        raise
                    except Exception:  # noqa: BLE001
                        # Could NOT authorize — do not resolve the ask
                        # unauthorized (frozen W2 contract). Release + re-arm
                        # the timer; the operator can re-tap.
                        logger.warning(
                            "engagement_ask advance_interaction_state failed "
                            "(engagement=%s rid=%s)",
                            rec.id[:8], request_id, exc_info=True,
                        )
                        BROKER.abort_claim(claim)
                        await _safe_answer(cq, "couldn't record — please tap again")
                        return
            committed = self._commit_ask_with_anchor(claim, ns, rec, meta)
        finally:
            # r7-B1: ANY exit without a commit (including CancelledError,
            # which `except Exception` above would not catch) must resolve the
            # claim so a claimed live request whose timer was cancelled is never
            # stranded. B4 (Sol diff r2): an authorized-but-uncommitted ask —
            # the caller was cancelled in the persist window AFTER the durable
            # write landed — must COMMIT (commit is identity-checked and safe),
            # else abort_claim (re-arm the timer for a re-tap). abort_claim is
            # idempotent + sync.
            if not committed:
                if advanced:
                    committed = self._commit_ask_with_anchor(claim, ns, rec, meta)
                else:
                    BROKER.abort_claim(claim)
        await _safe_answer(cq, "✔" if committed else "expired")

    async def _redraw_multi_keyboard(
        self, engagement_id: str, request_id: str, meta: dict,
        options: list, selected: list,
    ) -> bool:
        """A5 · F-MULTI: redraw the toggle keyboard after a flip, via the
        sequencer's markup-only ``edit_discrete`` (§A9).

        The redraw REVALIDATES live-and-unclaimed under the serialized CM
        immediately before the wire edit (``BROKER.is_live_unclaimed``) — a
        terminal outcome that resolved in between skips the edit, so a stale
        redraw can never resurrect a settled keyboard. Degrades to a no-op
        (returns False) when the driver/sequencer seam is absent (eager
        fallback / unit fakes). The message id + agent ``shorts`` live in the
        broker-owned meta seeded at post time."""
        mid = meta.get("message_id")
        if not isinstance(mid, int):
            return False
        try:
            import agent as _agent_mod
            drv = getattr(_agent_mod, "active_claude_code_driver", None)
        except Exception:  # noqa: BLE001
            drv = None
        if drv is None:
            return False
        editor = getattr(drv, "edit_ask_keyboard", None)
        if editor is None:
            return False
        from verdict_broker import BROKER

        # D2 item 3 (Task A5): redraw from the PERSISTED whole-set captions
        # seeded at registration — prepend only the mutable ☐/☑ glyph, never
        # re-resolve from ``shorts`` (byte-identical re-render, no drift).
        # Fall back to resolving from ``shorts`` only for a legacy meta that
        # predates the ``button_labels`` seed.
        captions = meta.get("button_labels")
        if not isinstance(captions, list) or len(captions) != len(options):
            captions = _captions_from_shorts(options, meta.get("shorts"), True)
        markup = _build_multi_keyboard(
            request_id, options, captions, selected)

        def _still_live() -> bool:
            return BROKER.is_live_unclaimed(
                namespace="engagement_ask", scope=engagement_id,
                request_id=request_id)

        try:
            return await editor(
                engagement_id, mid, markup, revalidate=_still_live)
        except Exception:  # noqa: BLE001 — a redraw failure never breaks the tap
            logger.debug("multi keyboard redraw failed", exc_info=True)
            return False

    def _commit_ask_with_anchor(
        self, claim: Any, ns: str, rec: Any, meta: dict,
    ) -> bool:
        """ONE commit helper for BOTH engagement-ask commit paths (the normal
        path and the ``finally`` recovery commit) — v0.79.0 §4 causal handoff.

        ``BROKER.commit`` resolves the ask future FIRST; only then, and only for
        ``engagement_ask``, do we SYNCHRONOUSLY set the sequencer's one-shot
        reply anchor to the ask message id. PRE-RESUMPTION GUARANTEE: asyncio
        run-to-completion means no awaiting coroutine RESUMES between the
        synchronous ``commit()`` and this synchronous set, so the CLI's ask
        response (which cannot be produced until its waiter resumes) can never
        be emitted before the anchor is in place — the SAME turn's first output
        threads to the question. Advisory only: no persistence, no rollback; a
        crash leaves unthreaded output with nothing pending."""
        from verdict_broker import BROKER

        committed = BROKER.commit(claim)
        if committed and ns == "engagement_ask":
            mid = meta.get("message_id") if isinstance(meta, dict) else None
            if isinstance(mid, int):
                try:
                    import agent as _agent_mod
                    drv = getattr(_agent_mod, "active_claude_code_driver", None)
                    setter = getattr(drv, "set_engagement_reply_anchor", None)
                    if setter is not None:
                        setter(rec.id, mid)
                except Exception:  # noqa: BLE001 — advisory, never break commit
                    logger.debug("reply-anchor set failed", exc_info=True)
        return committed

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
    # v0.37.2 (C-1): permission-relay keyboard composer
    # ------------------------------------------------------------------

    async def post_perm_keyboard(
        self,
        *,
        engagement_id: str,
        request_id: str,
        tool_name: str,
        tool_input: dict,
    ) -> int | None:
        """Post the ✅/❌ permission-relay keyboard to an engagement topic.

        Called by ``engagement_permission_relay`` hook (spec §4.4). Resolves
        the topic_id from ``engagement_registry``. ``send_to_topic`` returns
        the message_id (or None on failure); we pass it through.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        rec = self._engagement_registry.get(engagement_id)
        if rec is None or rec.topic_id is None:
            logger.warning(
                "post_perm_keyboard: unknown engagement or no topic_id "
                "(engagement=%s)", engagement_id[:8],
            )
            return None
        # #347: the relay hook's status==active gate goes stale across its
        # awaits — terminalization can land between that check and this post
        # (the terminal sweep cancels the broker scope BEFORE this request
        # registers, so nothing else withdraws it). This fresh-fetch is the
        # authoritative re-check: a terminal engagement never gets a live
        # Allow/Deny keyboard; returning None resolves waiters as
        # delivery_failed (→ deny) instead of a full-timeout wait.
        if getattr(rec, "status", None) in ("completed", "cancelled", "error"):
            logger.warning(
                "post_perm_keyboard: engagement %s is terminal (%s) — "
                "refusing keyboard", engagement_id[:8], rec.status,
            )
            return None

        # M11 (v0.52.0): escape the dynamic text. tool_name goes inside a
        # *bold* span (general MarkdownV2 escaping); the preview goes inside a
        # ``` code fence (pre/code escaping — only backtick + backslash). The
        # static prefix has no reserved chars. Unescaped, MCP tool names
        # (mcp__x__y — underscore runs) and Bash previews (backticks/
        # backslashes) trigger a Telegram 400 that the relay hook turns into
        # an auto-deny.
        body_lines = [f"Claude wants to use: *{_escape_mdv2(tool_name)}*"]
        preview = self._build_perm_preview(tool_name, tool_input)
        if preview:
            body_lines.append("")
            body_lines.append(f"```\n{_escape_mdv2_pre(preview)}\n```")
        body = "\n".join(body_lines)

        # v0.75.0 (W5/Sol B3,B4): v1 broker callback_data — option_index
        # 0=allow, 1=deny (verdict_broker.py meta["options"]=["allow","deny"]).
        kbd = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                text="✅ Allow",
                callback_data=f"v1|permission|{request_id}|0",
            ),
            InlineKeyboardButton(
                text="❌ Deny",
                callback_data=f"v1|permission|{request_id}|1",
            ),
        ]])

        try:
            return await self.send_to_topic(
                rec.topic_id, body,
                reply_markup=kbd,
                parse_mode="MarkdownV2",
            )
        except TelegramError as exc:
            # M11 defense-in-depth: any residual MarkdownV2 parse failure
            # degrades to an unformatted keyboard rather than a hook-level
            # auto-deny. The operator still sees the request and both buttons.
            logger.warning(
                "post_perm_keyboard MarkdownV2 send failed (engagement=%s); "
                "retrying as plain text: %s", engagement_id[:8], exc,
            )
            plain = f"Claude wants to use: {tool_name}"
            if preview:
                plain += f"\n\n{preview}"
            return await self.send_to_topic(
                rec.topic_id, plain, reply_markup=kbd,
            )

    @staticmethod
    def _build_perm_preview(tool_name: str, tool_input: dict) -> str:
        """Render a short, single-line preview of the tool's identifying field."""
        if tool_name == "Bash":
            return str(tool_input.get("command", ""))[:200]
        if tool_name in ("Edit", "Write", "Read", "Glob", "Grep"):
            return str(
                tool_input.get("file_path") or tool_input.get("pattern") or ""
            )[:200]
        if tool_name.startswith("mcp__"):
            return ""  # MCP tool calls — no useful primary field
        return ""

    async def edit_perm_keyboard_outcome(
        self, *, topic_id: int | None, message_id: int, outcome: dict,
    ) -> None:
        """Broker finish-hook target (r3-B3) — the ONLY writer that mutates a
        posted permission keyboard after the fact.

        Strips the inline buttons so a resolved / expired / cancelled
        keyboard can never be tapped again. ``_on_inline_callback`` itself
        never edits the keyboard — this method (invoked by the broker's
        finish-hook machinery, ``hooks._perm_keyboard_finish``) is the sole
        writer, and it fires exactly once per request regardless of which
        task set it up.
        """
        if not self.engagement_supergroup_id:
            return
        # v0.79.0 §4 (the real S1): send an EXPLICIT empty keyboard rather than
        # ``reply_markup=None`` — PTB drops None params, so a None markup would
        # leave the permission buttons tappable after the verdict settled.
        from telegram import InlineKeyboardMarkup
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=self.engagement_supergroup_id,
                message_id=message_id,
                reply_markup=InlineKeyboardMarkup([]),
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise
            logger.warning(
                "edit_perm_keyboard_outcome failed (topic=%s message_id=%s "
                "outcome=%s): %s",
                topic_id, message_id, outcome.get("outcome"), exc,
            )

    # ------------------------------------------------------------------
    # v0.75.0 (W5): engagement_ask keyboard composer + plain topic-message
    # edit/delete helpers.
    # ------------------------------------------------------------------

    async def post_options_keyboard(
        self,
        *,
        engagement_id: str,
        request_id: str,
        question: str,
        options: list,
        shorts: "list | None" = None,
        multi: bool = False,
        reply_to: "int | None" = None,
    ) -> int | None:
        """Post a rich-text multiple-choice question with one tappable
        button per option (W5 `ask`).

        ``reply_to`` (#332): the consumed turn reply target — a deferred ask
        that is the turn's first output threads to the operator's inbound
        message (v0.79.0 §3), like the narration open and reply posters.

        Mirrors ``post_perm_keyboard``'s engagement/topic resolution but skips
        MarkdownV2 entirely — the question/options are operator- or agent-authored
        free text. R2b (v0.89.0): the body renders through ``post_ask_body_rich``
        (a SINGLE-ATTEMPT rich send, fail-closed on an entity ``BadRequest``) so
        markdown (``**bold**``, `` `code` ``) renders as ``MessageEntity`` spans
        instead of leaking literal markers, WITHOUT the double-send that the
        round-4 single-post cancellation gate forecloses. Plain bodies still send
        raw (``render()`` returns ``entities=None`` ⇒ verbatim plain), so no
        escaping pass is needed.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        rec = self._engagement_registry.get(engagement_id)
        if rec is None or rec.topic_id is None:
            logger.warning(
                "post_options_keyboard: unknown engagement or no topic_id "
                "(engagement=%s)", engagement_id[:8],
            )
            return None

        # v0.75.0 (W5): v1 broker callback_data, one button (its own row)
        # per option — option_index is this option's position in `options`.
        # v0.81.0 (W-R3) / v0.84.0 (round 4, D2): the button LABEL is resolved
        # over the WHOLE option set by ``resolve_button_labels`` — EVERY option
        # needs a usable agent-supplied ``short`` or the WHOLE set floors to
        # numbered placeholders (never a per-option elision/short mix, D2 item
        # 1). ``question`` is already the canonical body (``render_ask_body``):
        # the FULL options are numbered VERBATIM in it, and ``callback_data``
        # still carries the option INDEX as the identity.
        # A5 · F-MULTI: a multi ask renders the toggle-many keyboard (checkboxes
        # + ✅ Submit) via the shared builder; single-select keeps one tappable
        # button per option (``v1|engagement_ask|<rid>|<idx>``).
        # D2 item 3 (Task A5): the initial render consumes the PERSISTED
        # whole-set ``button_labels`` seeded into broker meta at registration —
        # the SAME captions the multi redraw reads — so post and redraw can
        # never disagree. Falls back to resolving from ``shorts`` only when no
        # broker meta exists (eager fallback / direct-call unit tests).
        from verdict_broker import BROKER
        _meta = BROKER.get_meta(
            namespace="engagement_ask", scope=engagement_id, request_id=request_id)
        captions = _meta.get("button_labels") if isinstance(_meta, dict) else None
        if not isinstance(captions, list) or len(captions) != len(options):
            captions = _captions_from_shorts(options, shorts, multi)

        _send_kwargs: dict = {}
        if reply_to is not None:
            _send_kwargs["reply_to_message_id"] = reply_to

        if multi:
            kbd = _build_multi_keyboard(request_id, options, captions, selected=())
            return await self.post_ask_body_rich(
                rec.topic_id, question, reply_markup=kbd, **_send_kwargs)

        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                text=captions[i],
                callback_data=f"v1|engagement_ask|{request_id}|{i}",
            )]
            for i in range(len(options))
        ])

        return await self.post_ask_body_rich(
            rec.topic_id, question, reply_markup=kbd, **_send_kwargs)

    async def edit_topic_message(
        self, topic_id: int | None, message_id: int, text: str,
        *, clear_keyboard: bool = False,
    ) -> bool:
        """Plain (no parse_mode) text edit of a posted topic message.

        Broker finish-hook target for the ``engagement_ask`` namespace
        (mirrors ``edit_perm_keyboard_outcome``'s role for ``permission``,
        see ``channel_handlers._ask_keyboard_finish``). "Message is not
        modified" (JC4 — an identical re-edit) is tolerated as success, not
        an error, since it means the desired end state already holds.

        ``clear_keyboard`` (v0.79.0 §4, the real S1 fix): when True, send an
        EXPLICIT empty ``InlineKeyboardMarkup([])`` alongside the text so the
        settled question drops its tappable buttons. A bare ``edit_message_text``
        with no ``reply_markup`` leaves the old keyboard in place (PTB drops the
        None param, so ``editMessageText`` never touches the markup) — that was
        the settle-path bug (S1): answered/expired questions stayed tappable.
        """
        if not self.engagement_supergroup_id:
            return False
        reply_markup = None
        if clear_keyboard:
            from telegram import InlineKeyboardMarkup
            reply_markup = InlineKeyboardMarkup([])
        try:
            if reply_markup is not None:
                await self.bot.edit_message_text(
                    chat_id=self.engagement_supergroup_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
            else:
                await self.bot.edit_message_text(
                    chat_id=self.engagement_supergroup_id,
                    message_id=message_id,
                    text=text,
                )
            return True
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return True
            logger.warning(
                "edit_topic_message failed (topic=%s message_id=%s): %s",
                topic_id, message_id, exc,
            )
            return False
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise
            logger.warning(
                "edit_topic_message failed (topic=%s message_id=%s): %s",
                topic_id, message_id, exc,
            )
            return False

    async def edit_topic_message_rich(
        self, topic_id: int | None, message_id: int, text: str,
        *, clear_keyboard: bool = False,
    ) -> bool:
        """R2a (v0.89.0): rich-text edit of a posted NARRATION topic message.

        Rich sibling of ``edit_topic_message``: ``render()`` the CURRENT full
        string into ``MessageEntity`` spans and edit them in, so markdown
        (``**bold**``, `` `code` ``, fenced-pre) renders instead of leaking
        literal markers. Plain (no markup / over limits ⇒ ``entities is None``)
        delegates to the plain ``edit_topic_message`` and edits the RAW text
        verbatim. On an entity ``BadRequest`` the ORIGINAL text is edited in
        plain once (fail-literal); a "not modified" ``BadRequest`` is tolerated
        as success (JC4, matching ``edit_topic_message``). ``clear_keyboard``
        drops the tappable buttons via an explicit empty ``InlineKeyboardMarkup``
        (the S1 fix), exactly as ``edit_topic_message`` does.

        RENDERS ON EVERY EDIT (not only at a terminal edit): a narration message
        can be SEALED by a tool_use / discrete post / rollover before the turn's
        ``result``, and a render-at-terminal rule would leave every earlier
        sealed message permanently plain. ``render()`` recomputes from the whole
        current string each call.

        Accepted, documented residuals (NOT fixed — YAGNI): a throttled
        INTERMEDIATE edit can catch a half-open span (``**bold`` before the
        closing ``**`` has streamed in) and briefly show literal markers; it
        self-heals on the next edit and is fully correct at every sealing edit.
        A rollover split can bisect a span across two messages and each half
        degrades to literal independently — degraded, never corrupt.
        """
        display, entities = render(text)
        if entities is None:
            return await self.edit_topic_message(
                topic_id, message_id, text, clear_keyboard=clear_keyboard)
        if not self.engagement_supergroup_id:
            return False
        markup_kwargs: dict = {}
        if clear_keyboard:
            from telegram import InlineKeyboardMarkup
            markup_kwargs["reply_markup"] = InlineKeyboardMarkup([])
        try:
            await self.bot.edit_message_text(
                chat_id=self.engagement_supergroup_id,
                message_id=message_id,
                text=display,
                entities=entities,
                **markup_kwargs,
            )
            return True
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return True
            logger.warning(
                "edit_topic_message_rich fell back to plain "
                "(topic=%s message_id=%s): %s", topic_id, message_id, exc)
            try:
                await self.bot.edit_message_text(
                    chat_id=self.engagement_supergroup_id,
                    message_id=message_id,
                    text=text,  # ORIGINAL, verbatim
                    **markup_kwargs,
                )
                return True
            except BadRequest as exc2:
                if "not modified" in str(exc2).lower():
                    return True
                logger.warning(
                    "edit_topic_message_rich plain retry failed "
                    "(topic=%s message_id=%s): %s", topic_id, message_id, exc2)
                return False
            except Exception as exc2:  # noqa: BLE001 — best-effort, never raise
                logger.warning(
                    "edit_topic_message_rich plain retry failed "
                    "(topic=%s message_id=%s): %s", topic_id, message_id, exc2)
                return False
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise
            logger.warning(
                "edit_topic_message_rich failed "
                "(topic=%s message_id=%s): %s", topic_id, message_id, exc)
            return False

    async def send_topic_message_markup(
        self, topic_id: int | None, text: str, markup: Any = None,
        *, reply_to: int | None = None,
    ) -> int | None:
        """A9 (v0.83.0): topic send carrying an inline keyboard, returning the
        posted ``message_id`` (markup-capable companion to ``send_to_topic``
        for the OUTPUT SEQUENCER's ``post_discrete``).

        ``markup`` is an ``InlineKeyboardMarkup`` (passed through), the
        ``output_sequencer.MARKUP_EMPTY`` sentinel (⇒ explicit empty keyboard),
        or ``None`` (⇒ no keyboard, plain send). ``reply_to`` reply-threads the
        send. Best-effort: returns ``None`` on failure rather than raising.

        v0.109.0 (G4): the body renders rich, restoring symmetry with the edit
        sibling (``edit_topic_message_markup`` has rendered since R2c v0.89.0 —
        a discrete post showed literal ``**`` until its settle-edit re-rendered
        it). Same contract as R2c: entities on success; no markup / over caps ⇒
        ORIGINAL raw text plain; an entity ``BadRequest`` (which posted
        NOTHING) retries ONCE plain — this is not the cancellation-gated ask
        poster, so the retry cannot double-post an ask."""
        if not self.engagement_supergroup_id:
            return None
        kwargs: dict = {}
        resolved = self._resolve_wire_keyboard(markup)
        if resolved is not None:
            kwargs["reply_markup"] = resolved
        if reply_to is not None:
            from telegram import ReplyParameters
            kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to, allow_sending_without_reply=True,
            )
        display, entities = render(text)
        try:
            if entities is not None:
                try:
                    msg = await self.bot.send_message(
                        chat_id=self.engagement_supergroup_id,
                        text=display,
                        message_thread_id=topic_id,
                        entities=entities,
                        **kwargs,
                    )
                    return msg.message_id
                except BadRequest as exc:
                    logger.warning(
                        "send_topic_message_markup rich send fell back to "
                        "plain (topic=%s): %s", topic_id, exc,
                    )
            msg = await self.bot.send_message(
                chat_id=self.engagement_supergroup_id,
                text=text,
                message_thread_id=topic_id,
                **kwargs,
            )
            return msg.message_id
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise
            logger.warning(
                "send_topic_message_markup failed (topic=%s): %s", topic_id, exc,
            )
            return None

    async def edit_topic_message_markup(
        self, topic_id: int | None, message_id: int, text, markup,
    ) -> bool:
        """A9 (v0.83.0): markup-capable edit of a posted topic message (the
        sequencer's ``edit_discrete`` wire).

        * ``text`` non-None, ``markup is _ABSENT`` → text-only edit; the existing
          keyboard is left UNTOUCHED (PTB drops the omitted ``reply_markup``).
        * ``text`` non-None, ``markup`` given → edit text AND keyboard.
        * ``text is None``, ``markup`` given → markup-only edit via
          ``bot.edit_message_reply_markup``.
        * ``text is None`` and ``markup is _ABSENT`` → nothing to do (no-op True).

        ``markup`` values resolve like ``send_topic_message_markup``'s (a real
        keyboard passes through; ``MARKUP_EMPTY``/``None`` ⇒ explicit empty
        keyboard to CLEAR). "Message is not modified" is tolerated as success
        (the desired end-state already holds), matching ``edit_topic_message``.

        R2c (v0.89.0): the two TEXT-present branches render markdown into
        ``MessageEntity`` spans (the multi-select settle path
        ``settle_ask_keyboard`` → ``edit_discrete`` → here re-sends the ask body,
        which carries the raw ``**`` markers). ``render()`` decides: entities
        present ⇒ edit them in (keeping the resolved ``reply_markup``); on an
        entity ``BadRequest`` retry ONCE plain with the ORIGINAL raw text — an
        EDIT is NOT the cancellation-gated POST, so this second edit introduces
        no double-send race. ``entities is None`` (plain text / the marker
        literals from the re-anchor/orphan edits) ⇒ edit the RAW text verbatim
        (behaviour-preserving). The ``text is None`` markup-only branch never
        renders. The F1 no-op cache in ``edit_discrete`` keys on the raw ``text``
        ABOVE this wire, so rendering below it stays consistent."""
        from channels.output_sequencer import _ABSENT
        if not self.engagement_supergroup_id:
            return False
        markup_touched = markup is not _ABSENT
        if text is None and not markup_touched:
            return True  # nothing to change
        markup_kwargs: dict = {}
        if markup_touched:
            markup_kwargs["reply_markup"] = self._resolve_wire_keyboard(
                markup, clear=True)
        try:
            if text is None:
                # Markup-only edit — render() is never invoked (no text).
                await self.bot.edit_message_reply_markup(
                    chat_id=self.engagement_supergroup_id,
                    message_id=message_id, **markup_kwargs,
                )
                return True
            display, entities = render(text)
            if entities is None:
                await self.bot.edit_message_text(
                    chat_id=self.engagement_supergroup_id,
                    message_id=message_id, text=text, **markup_kwargs,
                )
                return True
            try:
                await self.bot.edit_message_text(
                    chat_id=self.engagement_supergroup_id,
                    message_id=message_id, text=display, entities=entities,
                    **markup_kwargs,
                )
                return True
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return True
                logger.warning(
                    "edit_topic_message_markup rich edit fell back to plain "
                    "(topic=%s message_id=%s): %s", topic_id, message_id, exc)
                await self.bot.edit_message_text(
                    chat_id=self.engagement_supergroup_id,
                    message_id=message_id, text=text,  # ORIGINAL, verbatim
                    **markup_kwargs,
                )
                return True
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return True
            logger.warning(
                "edit_topic_message_markup failed (topic=%s message_id=%s): %s",
                topic_id, message_id, exc,
            )
            return False
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise
            logger.warning(
                "edit_topic_message_markup failed (topic=%s message_id=%s): %s",
                topic_id, message_id, exc,
            )
            return False

    @staticmethod
    def _resolve_wire_keyboard(markup, *, clear: bool = False):
        """Map a sequencer markup value to a concrete PTB ``reply_markup``.

        A real ``InlineKeyboardMarkup`` passes through unchanged; the
        ``output_sequencer.MARKUP_EMPTY`` sentinel resolves to an explicit empty
        keyboard. ``None`` resolves to an explicit empty keyboard when *clear* is
        set (an EDIT that must drop the old keyboard) or to ``None`` otherwise (a
        fresh SEND has no keyboard to clear)."""
        from telegram import InlineKeyboardMarkup
        from channels.output_sequencer import MARKUP_EMPTY
        if markup is None:
            return InlineKeyboardMarkup([]) if clear else None
        if isinstance(markup, str) and markup == MARKUP_EMPTY:
            return InlineKeyboardMarkup([])
        return markup

    async def delete_topic_message(
        self, topic_id: int | None, message_id: int,
    ) -> bool:
        """Delete a single message from a topic (W5 companion to
        ``edit_topic_message``).

        Best-effort: any failure returns ``False`` rather than raising —
        unlike whole-topic ``delete_topic``, losing one message is not
        audit-critical.
        """
        if not self.engagement_supergroup_id:
            return False
        try:
            return bool(await self.bot.delete_message(
                chat_id=self.engagement_supergroup_id,
                message_id=message_id,
            ))
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise
            logger.warning(
                "delete_topic_message failed (topic=%s message_id=%s): %s",
                topic_id, message_id, exc,
            )
            return False

    # ------------------------------------------------------------------
    # v0.76.0 (W5b/resident_ask): DM keyboard composer + plain DM-message
    # edit helper + button-continuation dispatch. Chat-addressed analogues
    # of the v0.75.0 engagement-topic helpers (no topic thread) — the
    # resident-tap spine [A:§2, r1-B2/r3-B1].
    # ------------------------------------------------------------------

    async def post_dm_keyboard(
        self, *, chat_id: int, request_id: str, text: str, options: list[str],
        short_labels: bool = False,
    ) -> int | None:
        """Post a plain-text question to a DM chat with one tappable button
        per option (resident_ask).

        DM analogue of ``post_options_keyboard``: chat-addressed (no topic
        thread), plain send (operator-/agent-authored free text, no MarkdownV2
        escaping pass). ``callback_data`` is ``v1|resident_ask|<request_id>|<i>``
        where ``i`` is the option's position. Returns the Telegram
        ``message_id``, or ``None`` on send failure — the broker's
        ``ensure_posted`` treats ``None`` as a delivery failure (r10-B3).

        ``short_labels`` (v0.81.0, W-R3b; v0.84.0 round 4 D2): when True, derive
        button labels via the whole-set ``short_option_labels`` — this call site
        has no per-option agent ``short`` to offer it (``options`` is a plain
        label list), so it always floors to the numbered placeholder until the
        deferred Haiku label-generation fallback ships (D2 item 4). Telegram
        truncates long labels, which made ``ask_user`` options unpickable; the
        FULL options must already live VERBATIM in ``text`` (the caller renders
        them with ``render_ask_body``). Default False keeps the authz-challenge
        DM path (``authz_grants`` — short ``Approve``/``Deny`` options) untouched.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        heuristic = short_option_labels(options) if short_labels else None
        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                text=(heuristic[i] if heuristic is not None else options[i]),
                callback_data=f"v1|resident_ask|{request_id}|{i}",
            )]
            for i in range(len(options))
        ])
        # v0.109.0 (G1): render the BODY rich (DM parity with
        # ``post_ask_body_rich``) — agent-authored ask bodies are markdown-heavy
        # and this was the dominant literal-``**`` DM surface. Buttons and
        # ``callback_data`` are untouched (index-addressed). Single-shot
        # ``render()``: no markup / over caps ⇒ the ORIGINAL raw text sends
        # plain. An entity ``BadRequest`` posted NOTHING (Telegram rejected the
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
                    chat_id=chat_id, text=text, reply_markup=kbd,
                )
        except Exception as exc:  # noqa: BLE001 — delivery failure, not fatal
            logger.warning(
                "post_dm_keyboard send failed (chat=%s): %s", chat_id, exc,
            )
            return None
        return msg.message_id

    async def edit_dm_message(
        self, chat_id: int, message_id: int, text: str,
    ) -> bool:
        """Plain (no parse_mode, no reply_markup) text edit of a posted DM
        message — resident_ask finish-hook target.

        DM analogue of ``edit_topic_message`` (chat-addressed, no supergroup
        guard). "Message is not modified" (an identical re-edit) is tolerated
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
        v0.79.0 fixed for the TOPIC path (``edit_topic_message``'s
        ``clear_keyboard``); the DM path was left behind, and it settles EVERY
        DM question Casa asks: ``ask_user``, ``wipe_memory``, the
        protected-action challenge, and callback / event / trigger / persona /
        specialist install consent. Unconditional rather than opt-in because
        every caller in the tree is a broker FINISH hook — a terminal outcome,
        never a mid-question repaint — and an opt-in flag is a thing the next
        call site forgets.
        """
        from telegram import InlineKeyboardMarkup

        cleared = InlineKeyboardMarkup([])
        display, entities = render(text)
        try:
            if entities is not None:
                try:
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
            await self.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
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
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise
            logger.warning(
                "edit_dm_message failed (chat=%s message_id=%s): %s",
                chat_id, message_id, exc,
            )
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
        text: str, epoch: str | None = None,
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

    async def _internal_post(self, path: str, payload: dict) -> dict:
        """Dispatch an internal-handler call from inside casa-main.

        Used by handlers that need to round-trip into the internal-handler
        family that the per-engagement MCP server also uses. Implemented as
        an HTTP call over the casa-main Unix socket for symmetry with the
        engagement-side ``_internal_post`` in ``casa_engagement_channel`` —
        a future optimization could swap to a direct callable dispatch, but
        the wire-level path is the simplest correct shape today.
        """
        import aiohttp
        connector = aiohttp.UnixConnector(path="/run/casa/internal.sock")
        async with aiohttp.ClientSession(connector=connector) as sess:
            async with sess.post(
                f"http://localhost{path}", json=payload,
            ) as resp:
                return await resp.json()

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

    # ------------------------------------------------------------------
    # Engagement topic helpers
    # ------------------------------------------------------------------

    ENGAGEMENT_COMMANDS = [
        ("cancel", "Cancel this engagement and close the topic"),
        ("complete", "Mark this engagement complete (no agent summary)"),
        ("silent", "Stop proactive notifications from Ellen for this engagement"),
    ]

    async def setup_engagement_features(self) -> None:
        """Idempotent startup wiring for forum-supergroup engagements.

        - Verifies the bot has ``can_manage_topics`` in the supergroup.
        - Registers slash commands scoped to that supergroup.
        Safe to call when ``engagement_supergroup_id`` is unset — no-op.
        """
        self.engagement_permission_ok = False
        self.engagement_can_pin = False
        if not self.engagement_supergroup_id:
            return
        # Bot-permissions check
        try:
            me = await self.bot.get_me()
            # M10 (v0.52.0): cache the bot's own @username so handle_update
            # can strip "@botname" off menu-selected group commands. The
            # isinstance guard keeps MagicMock get_me() fakes in tests from
            # poisoning the cache (leaves it None → strip unconditionally).
            _u = getattr(me, "username", None)
            if isinstance(_u, str) and _u:
                self._bot_username = _u.lower()
            member = await self.bot.get_chat_member(
                chat_id=self.engagement_supergroup_id,
                user_id=me.id,
            )
            if not getattr(member, "can_manage_topics", False):
                logger.error(
                    "Engagement supergroup %s: bot lacks can_manage_topics; "
                    "engagements disabled",
                    self.engagement_supergroup_id,
                )
                return
            self.engagement_permission_ok = True
            # v0.79.0 (§5): probe can_pin_messages alongside can_manage_topics.
            # Best-effort — the pinned live summary is nicer-to-have; without
            # the grant the summary still lives, just unpinned (a WARN at
            # pin-attempt time, never a hard failure).
            self.engagement_can_pin = bool(
                getattr(member, "can_pin_messages", False))
            if not self.engagement_can_pin:
                logger.info(
                    "Engagement supergroup %s: bot lacks can_pin_messages; "
                    "the live summary will not be pinned",
                    self.engagement_supergroup_id,
                )
            # v0.37.1 D-1: boot-time diagnostic for the topic-icon map.
            # Non-fatal — logged only if any of our IDs rotated out of
            # Telegram's curated set.
            try:
                from channels import topic_icons
                await topic_icons.verify_against_telegram(self.bot)
            except Exception as exc:  # noqa: BLE001 — diagnostic only
                logger.info(
                    "Engagement supergroup %s: topic_icons verifier "
                    "failed (%s); proceeding", self.engagement_supergroup_id, exc,
                )
        except Exception as exc:
            logger.error(
                "Engagement supergroup %s: bot-permissions check failed: %s",
                self.engagement_supergroup_id, exc,
            )
            return

        # setMyCommands — scoped to the supergroup chat
        from telegram import BotCommand, BotCommandScopeChat  # type: ignore
        commands = [BotCommand(c, d) for c, d in self.ENGAGEMENT_COMMANDS]
        scope = BotCommandScopeChat(chat_id=self.engagement_supergroup_id)
        await self.bot.set_my_commands(commands, scope=scope)
        logger.info(
            "Engagement supergroup %s: commands registered (%s)",
            self.engagement_supergroup_id,
            [c for c, _ in self.ENGAGEMENT_COMMANDS],
        )

    async def open_engagement_topic(
        self, *, name: str, role: str = "",
    ) -> int:
        """Create a Telegram forum topic in the engagement supergroup.

        v0.37.1 D-1: ``role`` resolves to a numeric ``custom_emoji_id``
        via ``channels.topic_icons.icon_id_for_role`` and is sent as
        ``icon_custom_emoji_id``. Unknown roles fall back to the
        default (🤖) icon — the helper never returns None.

        Returns the ``message_thread_id``. Raises ``RuntimeError`` if
        the supergroup is not configured.
        """
        if not self.engagement_supergroup_id:
            raise RuntimeError("engagement supergroup not configured")
        from channels.topic_icons import icon_id_for_role
        topic = await self.bot.create_forum_topic(
            chat_id=self.engagement_supergroup_id,
            name=name,
            icon_custom_emoji_id=icon_id_for_role(role),
        )
        return topic.message_thread_id

    async def send_to_topic(
        self, thread_id: int, text: str, **kwargs,
    ) -> int:
        """Post a message into the given forum-supergroup topic.

        Returns the Telegram ``message_id`` of the posted message.
        ``kwargs`` is forwarded to ``bot.send_message`` (used by Phase 2+
        callers passing ``reply_markup`` for inline keyboards, etc.).
        """
        if not self.engagement_supergroup_id:
            raise RuntimeError("engagement supergroup not configured")
        msg = await self.bot.send_message(
            chat_id=self.engagement_supergroup_id,
            text=text,
            message_thread_id=thread_id,
            **kwargs,
        )
        return msg.message_id

    async def send_to_topic_rich(
        self, thread_id: int, text: str, **kwargs,
    ) -> int:
        """R2a (v0.89.0): post agent output into a topic WITH rich text.

        The narration relay path (``OutputSequencer.open_narration`` →
        ``casa_core._send_to_topic``) and the block-mode reply finalize
        (``send_response_to_topic``) both route through here so markdown
        (``**bold**``, `` `code` ``, fenced-pre) renders as ``MessageEntity``
        spans instead of leaking literal markers. ``render()`` recomputes from
        the COMPLETE current string on every call — see the matching per-edit
        render in ``edit_topic_message_rich``.

        Plain (no markup / over limits ⇒ ``entities is None``) → send the RAW
        text verbatim through the plain ``send_to_topic`` (mirrors the
        raw-when-no-entities precedent elsewhere in this module). On an entity
        ``BadRequest`` the ORIGINAL text is resent plain once (fail-literal); a
        duplicate is harmless for narration, which is not under the cancellation
        gate. ``kwargs`` (e.g. ``reply_parameters``) forward to both paths.

        Accepted, documented residuals (render-every-edit, NOT fixed — YAGNI):
        a throttled INTERMEDIATE edit can catch a half-open span (``**bold``
        before the closing ``**`` streams in) and briefly show literal markers;
        it self-heals at the next edit. A rollover split can bisect a span
        across messages and each half degrades to literal independently —
        degraded, never corrupt.
        """
        display, entities = render(text)
        if entities is None:
            return await self.send_to_topic(thread_id, text, **kwargs)
        if not self.engagement_supergroup_id:
            raise RuntimeError("engagement supergroup not configured")
        try:
            msg = await self.bot.send_message(
                chat_id=self.engagement_supergroup_id, text=display,
                message_thread_id=thread_id, entities=entities, **kwargs,
            )
        except BadRequest as exc:
            logger.warning("topic narration rich-text fell back to plain: %s", exc)
            msg = await self.bot.send_message(
                chat_id=self.engagement_supergroup_id, text=text,
                message_thread_id=thread_id, **kwargs,
            )
        return msg.message_id

    async def post_ask_body_rich(
        self, thread_id: int, text: str, **kwargs,
    ) -> int:
        """R2b (v0.89.0): SINGLE-ATTEMPT rich send for the ask/anchor POST path.

        The round-4 single-post CANCELLATION invariant is BINDING: the ask
        poster (``post_options_keyboard``) and the free-text-anchor poster
        (``channel_handlers._post_anchor``) must make EXACTLY ONE physical Bot
        API send attempt, so a cancel that latches immediately before the send
        (re-read at the wire) can never be raced by a *second* send. This is
        why this method must NOT be ``send_to_topic_rich`` / ``send_response_to_topic``
        — their rich→``BadRequest``→plain fallback is that forbidden second send.

        Behaviour (mirrors the raw-when-no-entities precedent of the other rich
        primitives, but with NO fallback):
        * killswitch off / ``entities is None`` (no markup, or markers stripped
          e.g. >100 spans) → send the ORIGINAL RAW ``text`` plain in ONE attempt
          (never the marker-stripped ``display``);
        * entities present → send WITH entities in ONE attempt. An entity
          ``BadRequest`` FAILS CLOSED — it PROPAGATES to the caller (which treats
          a failed post as ``delivery_failed``); there is NO plain retry and NO
          second send.

        ``kwargs`` (e.g. ``reply_markup`` for the ask keyboard) forward on both
        paths. Returns the posted ``message_id``.
        """
        display, entities = render(text)
        if entities is None:
            return await self.send_to_topic(thread_id, text, **kwargs)
        if not self.engagement_supergroup_id:
            raise RuntimeError("engagement supergroup not configured")
        # ONE physical send. On BadRequest this raises — fail-closed, never a
        # second (plain) send (the double-send the cancellation gate forecloses).
        msg = await self.bot.send_message(
            chat_id=self.engagement_supergroup_id, text=display,
            message_thread_id=thread_id, entities=entities, **kwargs,
        )
        return msg.message_id

    async def pin_topic_message(
        self, thread_id: int, message_id: int,
    ) -> bool:
        """v0.79.0 (§5): pin the live-summary message within its forum topic.

        Best-effort — returns ``False`` (never raises) when pinning is
        unavailable (no ``can_pin_messages`` grant, or a Telegram error). A
        pinned message in a forum-topic thread pins within that topic. Never
        unpins any other message.
        """
        if not self.engagement_supergroup_id:
            return False
        if not getattr(self, "engagement_can_pin", False):
            return False
        try:
            await self.bot.pin_chat_message(
                chat_id=self.engagement_supergroup_id,
                message_id=message_id,
                disable_notification=True,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — pin is best-effort
            logger.info(
                "pin_topic_message failed (thread=%s message_id=%s): %s",
                thread_id, message_id, exc,
            )
            return False

    async def update_topic_state(
        self, *, engagement_id: str, new_state: str,
    ) -> None:
        """E-12 (v0.37.0) Task 23: re-render the topic title with ``new_state``.

        Reuses the existing role_or_type + ``task`` stored on the
        ``EngagementRecord``. Idempotent — when ``new_state`` is identical to
        the persisted ``current_state_emoji`` it's a no-op. Unknown
        ``new_state`` values are also no-ops (defensive).

        #529 sentinel scheme: the persisted emoji is made UNCERTAIN (``""``,
        strict write-ahead) BEFORE the wire await and settled to the real
        value only after it. A cancellation landing anywhere between the two
        persists therefore leaves a value the no-op guard above can never
        match — the next serialized paint (any state, including the one that
        was persisted before the cancel) goes through and re-converges the
        wire. The pre-#529 order (persist only after a successful edit) left
        wire=new/persisted=old on a cancel in the window, and the guard then
        swallowed every paint requesting the persisted state: a PERMANENT
        divergence. An identical-title ``"not modified"`` rejection counts as
        wire success (the desired title already holds — the landed-but-
        unpersisted case) so the sentinel converges instead of looping.
        """
        from channels.state_emoji import (
            STATE_EMOJI, compose_topic_title, concise_task,
        )
        if self._engagement_registry is None:
            return
        # #347 (Sol+Terra r4): ONE per-engagement lock totally orders every
        # state edit — all writers (the permission relay's awaiting/active
        # round-trip AND the terminal-transition paths) go through this
        # method, so under the lock the record re-read below is authoritative
        # for the whole check→wire-edit→persist span. A terminal transition
        # marks the registry BEFORE calling here, so any ``active`` repaint
        # ordered after it observes the terminal status and refuses; one
        # ordered before it is simply overwritten by the terminal edit that
        # queued behind the lock. This closes the check-to-edit interleaving
        # a caller-side status gate cannot (the wire call was already in
        # flight).
        lock = self._topic_state_locks.setdefault(engagement_id, asyncio.Lock())
        async with lock:
            rec = self._engagement_registry.get(engagement_id)
            if rec is None or rec.topic_id is None:
                return
            # Terra r5: EVERY non-terminal paint (active 🟢 / awaiting 🟡) is
            # refused over a terminal record — a delayed authenticated
            # update_state request must not repaint a closed topic; terminal
            # paints (completed/failed/cancelled) remain allowed.
            if (
                getattr(rec, "status", None)
                in ("completed", "cancelled", "error")
                and new_state not in ("completed", "failed", "cancelled")
            ):
                return  # never repaint a terminal engagement live
            new_emoji = STATE_EMOJI.get(new_state)
            if new_emoji is None or new_emoji == rec.current_state_emoji:
                return

            # W-R6 (v0.81.0): read the persisted short topic title
            # (engager-supplied or Casa-derived at ingest); legacy rows with
            # no persisted title fall back to the concise_task label so old
            # tombstones never crash.
            short_title = (
                getattr(rec, "topic_title", "") or concise_task(rec.task or "")
            )
            title = compose_topic_title(
                state=new_state,
                short_task=short_title,
            )
            if not self.engagement_supergroup_id:
                return
            # #529 step 1: durable uncertainty BEFORE the wire. Strict — a
            # swallowed write failure would proceed to the wire with a record
            # a restart reloads as the OLD emoji (the original stuck state).
            # On failure the registry rolled the in-memory field back; never
            # touch the wire with an unpersistable record.
            try:
                await self._engagement_registry.set_channel_state(
                    engagement_id, current_state_emoji="", strict=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "U3 update_topic_state sentinel persist failed "
                    "(engagement=%s state=%s): %s — skipping wire edit",
                    engagement_id[:8], new_state, exc,
                )
                return
            try:
                await self.bot.edit_forum_topic(
                    chat_id=self.engagement_supergroup_id,
                    message_thread_id=rec.topic_id,
                    name=title,
                )
            except asyncio.CancelledError:
                raise  # wire state unknown; the sentinel stays (self-heals)
            except Exception as exc:  # noqa: BLE001
                if "not modified" not in str(exc).lower():
                    logger.warning(
                        "U3 update_topic_state edit_forum_topic failed "
                        "(engagement=%s state=%s): %s",
                        engagement_id[:8], new_state, exc,
                    )
                    return  # sentinel stays; the next paint retries
                # "not modified": the wire already carries this title —
                # treat as success and settle the real emoji below.
            # #529 step 2: settle the real value. Strict so a failed write
            # rolls the in-memory field back to the sentinel (memory=new with
            # disk="" would re-arm the no-op guard against the repair paint).
            try:
                await self._engagement_registry.set_channel_state(
                    engagement_id, current_state_emoji=new_emoji, strict=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "U3 set_channel_state(%s, %s) failed: %s — sentinel "
                    "kept; next paint re-converges",
                    engagement_id[:8], new_emoji, exc,
                )

    async def close_topic(self, thread_id: int) -> None:
        """Close a forum topic (v0.37.1 D-1 rename + simplification).

        Previously named ``close_topic_with_check`` and flipped the
        bubble icon to ``"✅"`` on close. That was a literal-char value
        rather than the required numeric ``custom_emoji_id``, so it
        silently failed and also stripped the leading char from the
        topic name. v0.37.1: bubble stays as the role icon for the
        engagement's whole lifecycle; state lives in the title (set
        by ``update_topic_state`` upstream). Body simplifies to a
        plain ``close_forum_topic``.
        """
        if not self.engagement_supergroup_id:
            raise RuntimeError("engagement supergroup not configured")
        try:
            await self.bot.close_forum_topic(
                chat_id=self.engagement_supergroup_id,
                message_thread_id=thread_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "close_forum_topic failed for thread=%s: %s",
                thread_id, exc,
            )

    async def delete_topic(self, thread_id: int) -> None:
        """Delete a forum topic and all its messages (v0.65.0 topic
        retention, [AR-3]/[AR-9]).

        Deliberately UNLIKE :meth:`close_topic`, this PROPAGATES every
        Telegram exception to the caller — it mirrors close_topic ONLY in
        the RuntimeError-when-unconfigured guard, NOT in its
        swallow-everything except. The topic-ledger sweep classifies the
        real error (not_found / permission / transient / unknown) to
        decide whether an entry is resolved or retained; a swallow here
        would make every failure look like success and silently drop
        ledger entries even under permission denial [AR-3].

        Refuses ``thread_id in (None, 0, 1)`` with a ValueError — 1 is
        the supergroup's General topic and None/0 mean "no topic"; an
        accidental call must never touch General [AR-9]. Requires the
        ``can_delete_messages`` admin right; without it Telegram raises
        BadRequest, which the sweep records as a permission failure and
        keeps the entry for retry.
        """
        if not self.engagement_supergroup_id:
            raise RuntimeError("engagement supergroup not configured")
        if thread_id in (None, 0, 1):
            raise ValueError(
                f"refusing to delete General/invalid topic "
                f"(thread_id={thread_id})"
            )
        await self.bot.delete_forum_topic(
            chat_id=self.engagement_supergroup_id,
            message_thread_id=thread_id,
        )

    async def _cleanup_error_topic(self, rec) -> None:
        """[AR-1] (v0.65.0) Best-effort topic cleanup for the two direct-
        ``mark_error`` terminal paths in ``handle_update``
        (``resume_failed``, ``orphan_no_session``).

        Those paths bypass the finalize funnel entirely, so pre-v0.65.0
        their topics stayed open — and unrecorded — forever after every
        restart-with-in-flight-engagement. ``rec.topic_id`` is guaranteed
        non-None here: the record was resolved *by* topic id. Title-mark
        (❌ failed, like every funnel-terminal topic), close and ledger
        append each run in their own try/except (warn-and-continue) — a
        Telegram or disk failure must never break the resume-failure
        handling itself, and the append happens even when the close fails
        (the sweep's not_found handling makes deleting an already-gone
        topic safe).
        """
        try:
            await self.update_topic_state(
                engagement_id=rec.id, new_state="failed",
            )
        except Exception as exc:  # noqa: BLE001 — best-effort title mark
            logger.warning(
                "error-path topic cleanup: update_topic_state failed for "
                "engagement %s (topic %s): %s",
                rec.id[:8], rec.topic_id, exc,
            )
        try:
            await self.close_topic(rec.topic_id)
        except Exception as exc:  # noqa: BLE001 — best-effort close
            logger.warning(
                "error-path topic cleanup: close_topic failed for "
                "engagement %s (topic %s): %s",
                rec.id[:8], rec.topic_id, exc,
            )
        try:
            await topic_ledger.append(
                engagement_id=rec.id,
                chat_id=self.engagement_supergroup_id,
                topic_id=rec.topic_id,
                outcome="error",
            )
        except Exception as exc:  # noqa: BLE001 — ledger raises on I/O failure
            logger.warning(
                "error-path topic cleanup: topic_ledger.append failed for "
                "engagement %s (topic %s): %s",
                rec.id[:8], rec.topic_id, exc,
            )
        # R5 (v0.89.0): drop the react target for this terminal engagement.
        self.clear_inbound(rec.id)

    # ------------------------------------------------------------------
    # Outbound: block mode
    # ------------------------------------------------------------------

    def create_topic_stream(self, topic_id: int) -> "TopicStreamHandle":
        """Build a per-turn streaming handle bound to this topic.

        Used by InCasaDriver to emit AssistantMessage chunks
        progressively rather than buffering the whole turn (Bug 1).
        """
        return TopicStreamHandle(self, topic_id)

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
    # send()/send_to_topic()/finalize_stream() stay plain for tools, bus
    # routing, notices, permission prompts, and error text.
    # ------------------------------------------------------------------

    async def _send_one(self, chat_id, original, display, entities, **kw):
        """Send one ≤4096 message with entities; on entity BadRequest resend the
        ORIGINAL text plain (exactly one retry — a TimedOut etc. propagates so we
        never duplicate a message Telegram may already have accepted)."""
        try:
            return await self._app.bot.send_message(
                chat_id=chat_id, text=display, entities=entities, **kw,
            )
        except BadRequest as exc:
            logger.warning("rich-text send fell back to plain: %s", exc)
            return await self._app.bot.send_message(
                chat_id=chat_id, text=original, **kw,
            )

    async def send_response(
        self, message: str, context: dict[str, Any],
    ) -> DeliveryOutcome:
        """Block-mode agent response with rich-text rendering (plain fallback).

        v2 (RC3): an oversized response paginates through ``render_paged()`` —
        every page sends rendered (previously the whole message fell back to
        plain raw chunks via ``send()``). Single page keeps the v0.70.0
        contract exactly (raw-when-no-entities, BadRequest → raw resend);
        multi-page fallbacks use each page's DISPLAY."""
        # Release the lease before the availability guard (reconnect-safe).
        target_chat = _resolve_chat_id(context, self.chat_id)
        self._release_typing(context, target_chat)
        if self._app is None:
            logger.warning("Telegram channel not started; cannot send message")
            return DeliveryOutcome.NOT_DELIVERED
        pages = render_paged(message)
        if len(pages) == 1:
            display, entities = pages[0]
            if entities is None:
                # Delegate's outcome, VERBATIM (#556 design §2.1) — rewrapping
                # it here is how a delegation path loses the distinction.
                return await self.send(message, context)
            await self._send_one(target_chat, message, display, entities)
            context["_delivery_head_sent"] = True
            return DeliveryOutcome.DELIVERED
        outcome = DeliveryOutcome.NOT_DELIVERED
        for display, entities in pages:
            if entities is None:
                await self._app.bot.send_message(
                    chat_id=target_chat, text=display)
            else:
                await self._send_one(target_chat, display, display, entities)
            # Page 1 carries the notice: once it lands the delivery counts as
            # shown, however the remaining pages fare.
            if outcome is DeliveryOutcome.NOT_DELIVERED:
                context["_delivery_head_sent"] = True
                outcome = DeliveryOutcome.DELIVERED
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
        if self._app is None:
            return DeliveryOutcome.NOT_DELIVERED
        message_id = _peek_stream_message_id(on_token)
        if message_id is None:
            return await self.send_response(full_text, context)  # verbatim
        # v2 (RC3): an oversized SUCCESSFUL response no longer delegates to
        # the plain finalize_stream (raw-marker chunks); page 1 rich-edits the
        # streamed message, the rest send rendered. Single page keeps the
        # v0.70.0 contract (no markup → plain finalize, BadRequest → raw edit).
        pages = render_paged(full_text)
        if len(pages) == 1 and pages[0][1] is None:
            return await self.finalize_stream(  # verbatim (§2.1)
                full_text, context, on_token)
        display0, entities0 = pages[0]
        fallback0 = full_text if len(pages) == 1 else display0
        # Page 1 carries a prepended operator notice, so page 1 decides the
        # outcome; the overflow sends below swallow their errors and cannot
        # downgrade it (#556 design §2.3).
        outcome = DeliveryOutcome.NOT_DELIVERED
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
        for display, entities in pages[1:]:
            try:
                if entities is None:
                    await self._app.bot.send_message(
                        chat_id=target_chat, text=display)
                else:
                    await self._send_one(target_chat, display, display, entities)
            except TelegramError as exc:
                logger.warning("Final stream overflow send failed: %s", exc)
        return outcome

    async def send_response_to_topic(
        self, thread_id: int, text: str, **kwargs,
    ) -> int:
        """Post an agent response into an engagement topic with rich text.

        Used by the Claude-Code reply handler and by TopicStreamHandle's
        finalize. v2 (2026-07-19, Sol+Terra D3): THIS is the paginating rich
        helper — an oversized response becomes several rendered messages via
        ``render_paged()`` (each ≤4096 UTF-16 units and ≤100 entities), so a
        long reply never ships raw markdown (the RC3 leak) and never 400s on
        length. ``send_to_topic_rich`` stays the sequencer's ONE-message
        narration primitive and is only used here for the single-page case.

        Single page keeps the v0.89.0 contract exactly (raw-when-no-entities,
        BadRequest → raw resend). Multi-page: every page sends its DISPLAY
        slice (markers already consumed — raw source offsets no longer
        correspond), falling back to the same display plain on an entity
        BadRequest; ``kwargs`` (e.g. ``reply_parameters``) attach to the FIRST
        page only. Returns the LAST page's ``message_id`` (the bottom-most
        message — the correct reply anchor for subsequent traffic)."""
        pages = render_paged(text)
        if len(pages) == 1:
            return await self.send_to_topic_rich(thread_id, text, **kwargs)
        if not self.engagement_supergroup_id:
            raise RuntimeError("engagement supergroup not configured")
        last_mid: int | None = None
        page_kwargs = kwargs
        for display, entities in pages:
            if entities is None:
                last_mid = await self.send_to_topic(
                    thread_id, display, **page_kwargs)
            else:
                try:
                    msg = await self.bot.send_message(
                        chat_id=self.engagement_supergroup_id, text=display,
                        message_thread_id=thread_id, entities=entities,
                        **page_kwargs,
                    )
                    last_mid = msg.message_id
                except BadRequest as exc:
                    logger.warning(
                        "topic response page fell back to plain: %s", exc)
                    last_mid = await self.send_to_topic(
                        thread_id, display, **page_kwargs)
            page_kwargs = {}
        assert last_mid is not None  # pages is never empty
        return last_mid

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


class TopicStreamHandle:
    """Per-engagement-topic streaming primitive used by InCasaDriver.

    Mirrors the (chat_id-keyed) on_token / finalize_stream pattern that
    Ellen uses on direct DMs (lines 739-859) but parameterised by
    topic_id and bound to the engagement supergroup chat. State
    (message_id, last_edit) lives for the duration of one SDK turn —
    each turn opens a fresh Telegram message in the topic.
    """

    def __init__(self, channel: "TelegramChannel", topic_id: int) -> None:
        self._channel = channel
        self._topic_id = topic_id
        self._message_id: int | None = None
        self._last_edit: float = 0.0

    async def emit(self, accumulated_text: str) -> None:
        """Throttled cumulative-text emit. First call sends a new
        message in the topic; subsequent calls edit-in-place,
        throttled at _STREAM_THROTTLE seconds."""
        bot = (
            getattr(self._channel._app, "bot", None)
            if self._channel._app else None
        )
        if bot is None:
            return

        now = time.monotonic()

        if self._message_id is None:
            # #305 (Sol r1): first-send UTF-16 guard, sibling of the DM
            # streaming closure — finalize() splits/paginate what emit skips.
            if utf16_len(accumulated_text) > _TG_MAX_LENGTH:
                return
            try:
                result = await bot.send_message(
                    chat_id=self._channel.engagement_supergroup_id,
                    message_thread_id=self._topic_id,
                    text=accumulated_text,
                )
                self._message_id = result.message_id
                self._last_edit = now
            except TelegramError as exc:
                logger.warning("Stream send failed: %s", exc)
            return

        if now - self._last_edit < _STREAM_THROTTLE:
            return

        # #305: UTF-16 units, Telegram's unit.
        if utf16_len(accumulated_text) > _TG_MAX_LENGTH:
            return

        try:
            await bot.edit_message_text(
                chat_id=self._channel.engagement_supergroup_id,
                message_id=self._message_id,
                text=accumulated_text,
            )
            self._last_edit = now
        except TelegramError as exc:
            if "not modified" not in str(exc).lower():
                logger.warning("Stream edit failed: %s", exc)

    async def finalize(self, full_text: str) -> None:
        """Final edit (or send) once the SDK loop has drained. Handles
        overflow by editing the first chunk and sending subsequent
        chunks as fresh topic messages."""
        bot = (
            getattr(self._channel._app, "bot", None)
            if self._channel._app else None
        )
        if bot is None:
            return

        if self._message_id is None:
            # No prior emit: fresh message(s). v2 (RC3): send_response_to_topic
            # paginates internally, so any size delivers rendered.
            try:
                await self._channel.send_response_to_topic(
                    self._topic_id, full_text,
                )
            except TelegramError as exc:
                logger.warning("Stream finalize rich send failed: %s", exc)
            return

        # v2 (RC3): page 1 rich-edits the streamed message; the rest are rich
        # sends. Entity BadRequest degrades to that page's DISPLAY plain (the
        # single-page case keeps the v0.89.0 raw-fallback contract).
        pages = render_paged(full_text)
        display0, entities0 = pages[0]
        fallback0 = full_text if len(pages) == 1 else display0
        try:
            if entities0 is not None:
                try:
                    await bot.edit_message_text(
                        chat_id=self._channel.engagement_supergroup_id,
                        message_id=self._message_id,
                        text=display0, entities=entities0,
                    )
                except BadRequest:
                    await bot.edit_message_text(
                        chat_id=self._channel.engagement_supergroup_id,
                        message_id=self._message_id,
                        text=fallback0,
                    )
            else:
                await bot.edit_message_text(
                    chat_id=self._channel.engagement_supergroup_id,
                    message_id=self._message_id,
                    text=fallback0,
                )
        except TelegramError as exc:
            if "not modified" not in str(exc).lower():
                logger.warning("Stream finalize edit failed: %s", exc)
        for display, entities in pages[1:]:
            try:
                if entities is not None:
                    try:
                        await bot.send_message(
                            chat_id=self._channel.engagement_supergroup_id,
                            message_thread_id=self._topic_id,
                            text=display, entities=entities,
                        )
                    except BadRequest:
                        await bot.send_message(
                            chat_id=self._channel.engagement_supergroup_id,
                            message_thread_id=self._topic_id,
                            text=display,
                        )
                else:
                    await bot.send_message(
                        chat_id=self._channel.engagement_supergroup_id,
                        message_thread_id=self._topic_id,
                        text=display,
                    )
            except TelegramError as exc:
                logger.warning(
                    "Stream finalize overflow send failed: %s", exc,
                )


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
