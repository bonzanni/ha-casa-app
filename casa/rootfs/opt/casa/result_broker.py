"""#792: the plugin result contract and its escrow broker.

A third-party plugin's MCP tool can return a live capability — a sign-in link,
a one-time code, a bearer token — and until this module existed Casa's first
sight of those bytes was the model's echo of them, on its way to the chat.
Casa never proxies a plugin's MCP server (the CLI talks to it directly), so the
seam Casa owns is the CLI's hook protocol, and the boundary is built there:

* **The contract** (``casa.resultContract`` in ``plugin.json``, extracted by
  ``plugin_store.manifest_result_contract``, mapped by
  ``plugin_grants.result_contract_map``) is the PRODUCER's declaration of which
  of its tools return a capability and which consume one. Casa validates its
  shape and never infers or supplements it (#785).
* **Admission** (:func:`make_plugin_admission_hook`, PreToolUse): a non-setup
  tool of a plugin that has NOT adopted the contract is refused BEFORE it runs.
  The exact ``casa.setupTool`` is exempt — its consent URL is delivered to the
  operator by design. For an adopting plugin the same callback runs the
  authorization decision for a protected tool and, only if that allowed the
  call, arms the references the call consumes — in ONE callback, because the
  SDK dispatches same-event matchers concurrently and arming must follow the
  authorization decision.
* **Escrow** (:class:`ReferenceStore`): a ``capability`` tool DEPOSITS the
  value with Casa over the internal Unix socket DURING the call and returns a
  ``casa-cap-<32hex>`` reference in its place. The value never enters the MCP
  result, the model's context, the transcript or its ``mcpMeta`` sidecar.
  A reference is bound to the call's grant identity (whoever could consume an
  approval for the same call — ``authz_grants.resolve_grant_identity``),
  single-use, TTL-bound (one approval keyboard plus one grant window),
  memory-only and so restart-losing.
* **Redemption**: a ``consumes`` parameter of a tool of the same plugin, in a
  session with the same grant identity, is rewritten by admission to
  ``<reference>:<ticket>``; the consumer plugin redeems it over the internal
  socket. The ticket exists only in the rewritten input the tool receives —
  the model's own message carries the bare reference (measured: the rewritten
  argument is not recorded in the transcript), so a parallel tool cannot
  present it.
* **Replacement** (:func:`make_result_hook`, PostToolUse): the second
  boundary. A result for a non-adopting plugin's non-setup tool is replaced by
  a withheld notice before the model sees it; a ``capability`` result passes
  only when every declared slot carries the reference of a deposit bound to
  that very call. :func:`make_failure_hook` (PostToolUseFailure) can replace
  nothing — the event has no replacement field — and only closes the call.
* **Delivery** (#1015): a slot the tool declares ``delivers`` as an
  ``operator_link`` never reaches the model at all. After the structural
  check, the same PostToolUse hook takes the deposit once and posts ONE
  labelled-link message to the chat of the call's grant identity — the chat
  the operator asked in, never a task topic — with the destination host
  printed by Casa from the URL. Proven delivery REPLACES the result with a
  receipt (``casa_delivery.status = "delivered"``); anything short of it
  withholds the result and drops the deposit. The producer's own result is
  delivery-neutral: the CLI abandons a hook past its matcher timeout and
  lets the original result through, so the receipt is the only carrier of
  the positive claim, and a result without one claims nothing.

Scope (the invariant's own): sessions that carry the authorization seam —
resident, delegated specialist, specialist engagement. Executor sessions are
outside it (#923). A producer that breaks its declared contract is the
plugin-author trust boundary #785 rules on; Casa detects no such violation.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import hmac
import json
import uuid
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from aiohttp import web

from media_policies import MEDIA_POLICIES

logger = logging.getLogger(__name__)

PLUGIN_TOOL_PREFIX = "mcp__plugin_"
PLUGIN_TOOL_MATCHER = "mcp__plugin_.*"    # measured on CLI 2.1.220: a regex
REFERENCE_PREFIX = "casa-cap-"
_REF_RE = re.compile(r"^casa-cap-[0-9a-f]{32}$")
_TICKET_RE = re.compile(r"^[0-9a-f]{32}$")
BROKER_SOCKET_PATH = "/run/casa/internal.sock"
ENV_CLIENT = "CASA_BROKER_CLIENT"
ENV_SOCKET = "CASA_BROKER_SOCKET"
# An in-flight call or an arm is never held open by a call that will not
# return (authz denial and cancellation produce no terminal hook event): both
# are time-capped and superseded by the next qualifying call.
INFLIGHT_CAP_S = 600.0
MAX_VALUE_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
# #1015: operator-link delivery. The hook's own bound on a slow Telegram —
# NOT the ordering guarantee: the CLI's matcher deadline runs independently of
# this process, which is why only the replacement receipt carries the claim.
DELIVERY_TIMEOUT_S = 20.0
# The matcher timeout the three broker matchers set EXPLICITLY (the SDK's
# default is also 60 s; a default is not a commitment).
HOOK_TIMEOUT_S = 60.0
MAX_LINK_BYTES = 2048
MAX_CAPTION_CHARS = 200
MAX_LABEL_CHARS = 40
DEFAULT_LINK_LABEL = "Open"
DELIVERED_TO = "operator_chat"
OPERATOR_LINK = "operator_link"
# S3: a specialist's plugin output posted by Casa, labelled, verbatim. The
# body is judged in CHARACTERS at deposit (a body over the cap is refused,
# never truncated — truncation would be Casa retelling); the page PLAN is
# capped separately at delivery, before the first send, because a character
# cap does not bound a page count (emoji are two UTF-16 units each, markers
# are bounded by the 100-entity budget: three 12,000-character bodies
# measured 4, 7 and 11 pages).
OPERATOR_MESSAGE = "operator_message"
OPERATOR_FILE = "operator_file"
# S5: a proposal — text, buttons, and per button the exact plugin tool call a
# tap commits; posted labelled with an inline keyboard, the calls held by
# Casa under the broker's ``proposal`` namespace (design §2–§3).
OPERATOR_PROPOSAL = "operator_proposal"
PROPOSAL_TTL_S = 3600.0          # the desk's idle bound: a proposal older than a dialogue is stale
PROPOSAL_MAX_LIVE = 32           # live proposals per chat; the 33rd deposit is withheld
PROPOSAL_TEXT_CHARS = 4000
PROPOSAL_MAX_BUTTONS = 6
PROPOSAL_LABEL_CHARS = 32
PROPOSAL_REVISION_CHARS = 64
PROPOSAL_MAX_PAGES = 6           # #1377: plain pages a card may bring before it
# S5 (Astra, diff rounds 2–3): the composed post leaves room for the longest
# line Casa appends when the keyboard settles — `\n☑ <label>` with a label of
# PROPOSAL_LABEL_CHARS characters that may each be an astral code point (two
# UTF-16 units); `\n✖ <reason>`, `\n⌛ expired` and `\n↻ replaced` are shorter —
# else a maximal proposal could never be settled
PROPOSAL_SETTLE_RESERVE = 1 + 2 + 2 * PROPOSAL_LABEL_CHARS
MAX_MESSAGE_CHARS = 12_000
MAX_MESSAGE_PAGES = 6
# The COMPOSED file caption — Casa's label line, a newline, the plugin's
# caption — must fit ``send_media``'s cap (tools._CAPTION_MAX; a test pins
# the two equal). An upload is slower than a text send, so its bound is
# longer — still under the CLI's matcher deadline (HOOK_TIMEOUT_S), so the
# receipt path, not the deadline, decides.
MAX_FILE_CAPTION_CHARS = 1024
FILE_DELIVERY_TIMEOUT_S = 45.0
# One fixed Casa marker for "a specialist posted this"; the label is
# ``<glyph> <display name>`` and the plugin cannot set, prefix or suppress it.
POST_LABEL_GLYPH = "📊"
# §6: the body-free echo the resident's conversation sees — one line per
# proven post, at most this many per vehicle, each within this bound.
ECHO_LINE_MAX = 120
ECHO_MAX_LINES = 5
_WS_RE = re.compile(r"\s")
_DOMAINISH_RE = re.compile(r"\.[A-Za-z]")


def reference_ttl_s() -> float:
    """Longest a conforming flow can legitimately wait between minting and
    redeeming: one approval keyboard plus one grant window — both constants
    are ``authz_grants``', never a fresh literal here."""
    from authz_grants import DEFAULT_GRANT_TTL_S, _CHALLENGE_TTL_S
    return float(_CHALLENGE_TTL_S) + float(DEFAULT_GRANT_TTL_S)


def new_client_id() -> str:
    return secrets.token_hex(16)


def new_reference() -> str:
    return REFERENCE_PREFIX + secrets.token_hex(16)


def is_reference(value: Any) -> bool:
    return isinstance(value, str) and bool(_REF_RE.fullmatch(value))


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _InFlight:
    client_id: str
    artifact_id: str
    tool_name: str
    tool_use_id: str
    identity: Any                 # authz_grants.GrantIdentity
    provides: tuple
    opened_at: float
    deposits: dict = dataclasses.field(default_factory=dict)   # slot -> ref
    delivers: dict = dataclasses.field(default_factory=dict)   # slot -> kind (#1015)
    # S5 §2: a proposal's buttons name OTHER tools of the plugin, judged at
    # deposit against the session's own contract and protected maps and the
    # depositing tool's entry (its server) — recorded here at admission.
    contract_map: Any = None
    protected: Any = None
    entry: Any = None


@dataclasses.dataclass
class _Reference:
    value: str
    slot: str
    identity: Any
    minted_at: float
    expires_at: float
    armed: tuple | None = None    # (client_id, tool_use_id, ticket)
    used: bool = False
    caption: str = ""             # #1015: delivered slots only, validated
    label: str = ""
    media_kind: str = ""          # S3: operator_file only — a MEDIA_POLICIES key
    proposal: Any = None          # S5: the parsed, validated proposal object
    filename: str = ""            # S7a: operator_file only — the delivered name, validated
    key: str = ""                 # #1312: delivered slots only — the plugin's delivery key

    def __repr__(self) -> str:      # never the value
        return (f"_Reference(slot={self.slot!r}, armed={self.armed is not None}, "
                f"used={self.used})")


# -- #1015: what a delivered slot's deposit may carry ------------------------

def _link_ok(value: Any) -> bool:
    """``https``, a hostname, ≤ MAX_LINK_BYTES, printable, no whitespace."""
    if not isinstance(value, str) or not value:
        return False
    if len(value.encode("utf-8", "surrogateescape")) > MAX_LINK_BYTES:
        return False
    if not value.isprintable() or _WS_RE.search(value):
        return False
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme == "https" and bool(host)


def _text_ok(value: Any, limit: int) -> bool:
    """A single printable line of at most *limit* characters that cannot
    itself read as a link: no scheme separator, no ``www.``."""
    if not isinstance(value, str) or len(value) > limit or not value.isprintable():
        return False
    lowered = value.lower()
    return "://" not in lowered and "www." not in lowered


def _caption_ok(value: Any) -> bool:
    return _text_ok(value, MAX_CAPTION_CHARS)


def _label_ok(value: Any) -> bool:
    """A label additionally may not look like a domain (``.`` followed by a
    letter): the host is Casa's to print from the URL, never the plugin's."""
    return _text_ok(value, MAX_LABEL_CHARS) and not _DOMAINISH_RE.search(value)


# -- S3: what a message or file slot's deposit may carry ----------------------

def _message_ok(value: Any) -> bool:
    """An ``operator_message`` body: non-blank text of at most
    MAX_MESSAGE_CHARS characters and MAX_VALUE_BYTES bytes, with no control
    character (Unicode category Cc) but newline and tab. Spaces of every
    width, joiners, marks and separators are text, not controls."""
    if not isinstance(value, str) or not value.strip():
        return False
    if len(value) > MAX_MESSAGE_CHARS:
        return False
    if len(value.encode("utf-8", "surrogateescape")) > MAX_VALUE_BYTES:
        return False
    return all(ch in "\n\t" or unicodedata.category(ch) != "Cc" for ch in value)


def _file_caption_ok(value: Any, label: str) -> bool:
    """An ``operator_file`` caption: one printable line, and the COMPOSED
    caption (``label``, newline, caption) fits MAX_FILE_CAPTION_CHARS —
    overflow is refused, never truncated."""
    if not isinstance(value, str) or not value.isprintable():
        return False
    return len(label) + 1 + len(value) <= MAX_FILE_CAPTION_CHARS


def proposal_ok(value: Any, call: Any) -> tuple[dict | None, str | None]:
    """S5 §2: the deposit-time judgement of a proposal — the JSON-encoded
    object, its text and buttons within bounds, every button's call resolved
    against the DEPOSITING call's own plugin and server through
    ``stored_calls`` (never a string the plugin supplies beyond the bare
    name), the argument grammar, the revision, and the one-physical-message
    rule on the composed post. Returns ``(parsed, None)`` with each button's
    call replaced by its resolved identities and canonical form, or
    ``(None, reason)``."""
    import stored_calls as sc
    from channels.tg_richtext import render_paged
    from text_util import utf16_len
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        return None, "bad_proposal"
    try:
        obj = json.loads(value)
    except ValueError:
        return None, "bad_proposal"
    if not isinstance(obj, dict):
        return None, "bad_proposal"
    text = obj.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > PROPOSAL_TEXT_CHARS:
        return None, "bad_proposal"
    if not _message_ok(text):
        return None, "bad_proposal"
    # #1377: optional plain pages posted before the card, each one labelled message
    pages = obj.get("pages")
    if pages is not None:
        if (not isinstance(pages, list) or not 1 <= len(pages) <= PROPOSAL_MAX_PAGES
                or not all(isinstance(p, str) and p.strip() and len(p) <= PROPOSAL_TEXT_CHARS
                           and _message_ok(p) for p in pages)):
            return None, "bad_proposal"
    buttons = obj.get("buttons")
    if not isinstance(buttons, list) or not 1 <= len(buttons) <= PROPOSAL_MAX_BUTTONS:
        return None, "bad_proposal"
    revision = obj.get("revision", "")
    if revision is None:
        revision = ""
    if not isinstance(revision, str) or len(revision) > PROPOSAL_REVISION_CHARS:
        return None, "bad_proposal"
    entry = getattr(call, "entry", None)
    servers = tuple(getattr(entry, "servers", ()) or ())
    if (call.identity is None or entry is None or len(servers) != 1
            or getattr(call, "contract_map", None) is None):
        return None, "bad_proposal"
    seg, server = entry.plugin_seg, servers[0]
    resolved = []
    arm_buttons = close_buttons = 0
    for button in buttons:
        if not isinstance(button, dict):
            return None, "bad_proposal"
        label = button.get("label")
        if (not isinstance(label, str) or not label.strip()
                or len(label) > PROPOSAL_LABEL_CHARS or not _text_ok(label, PROPOSAL_LABEL_CHARS)):
            return None, "bad_proposal"
        # #1375: a Close button stores no call — Casa removes the card's keyboard itself;
        # at most one per proposal, never beside another kind (it counts toward the six)
        if "close" in button:
            if (button.get("close") is not True or "call" in button or "arm_file" in button
                    or "keep_card" in button):
                return None, "bad_proposal"
            close_buttons += 1
            if close_buttons > 1:
                return None, "bad_proposal"
            resolved.append({"label": label, "close": True})
            continue
        # S6 §2.4: a button is EITHER a stored call OR an `arm_file` button — never both,
        # never neither; at most one arm button per proposal (it counts toward the six)
        if "arm_file" in button:
            if button.get("arm_file") is not True or "call" in button or "keep_card" in button:
                return None, "bad_proposal"
            arm_buttons += 1
            if arm_buttons > 1:
                return None, "bad_proposal"
            resolved.append({"label": label, "arm_file": True})
            continue
        spec = button.get("call")
        if not isinstance(spec, dict):
            return None, "bad_proposal"
        arguments = spec.get("arguments", {})
        if arguments is None:
            arguments = {}
        if sc.arguments_ok(arguments) is not None:
            return None, "bad_proposal"
        stored, why = sc.resolve_stored_call(
            spec.get("tool"), contract_map=call.contract_map,
            protected=getattr(call, "protected", None) or {}, seg=seg, server=server)
        if stored is None:
            return None, "bad_proposal"
        kept = {"label": label, "call": {
            "server": stored.server, "wire_name": stored.wire_name,
            "runtime_name": stored.runtime_name, "proposal": stored.proposal,
            "arguments": arguments, "canonical": sc.canonical_json(arguments)}}
        if "keep_card" in button:
            # #1362: a file button may leave its card live — only #1303's file sibling
            # (a tap that acts, or posts a card, settles the card it sits on)
            if button.get("keep_card") is not True or not sc.delivers_file(
                    call.contract_map.tools.get(stored.runtime_name)):
                return None, "bad_proposal"
            kept["keep_card"] = True
        resolved.append(kept)
    head = post_label(call.identity.enforcement_role)
    for body in [text, *(pages or ())]:
        composed = compose_operator_message(body, head)
        if (len(render_paged(composed)) != 1
                or utf16_len(composed) > 4096 - PROPOSAL_SETTLE_RESERVE):
            return None, "bad_proposal"
    out = {"text": text, "buttons": resolved, "revision": revision}
    if pages is not None:
        out["pages"] = list(pages)
    return out, None


def post_label(role: str) -> str:
    """The label Casa heads a specialist's post with: the glyph and the
    persona display name the ``<delegates>`` block advertises for *role*
    (the role itself when it has none) — from the one map Casa resolves
    delegation targets against, never from the plugin."""
    import tools as tools_mod
    return f"{POST_LABEL_GLYPH} {tools_mod._display_name_for_role(role)}"


def compose_operator_message(body: str, label: str) -> str:
    """The ONE text ``render_paged`` splits: the label line, then the body
    as deposited. Paginating the composed text budgets the header like any
    other line, so page 1 carries it and pages 2+ are bare — no planner,
    no reserved budget (a header added after pagination overflows a full
    page)."""
    return f"{label}\n{body}"


def compose_file_caption(label: str, caption: str) -> str:
    """The file's caption: the label line first, the plugin's caption (if
    any) beneath; a file with no plugin caption is still labelled."""
    return f"{label}\n{caption}" if caption else label


# -- S3 §6: the body-free echo ledger ------------------------------------------

@dataclasses.dataclass(frozen=True)
class PostEvent:
    """One proven post, as the resident's conversation may learn of it:
    Casa-derived metadata only — never the body, the caption or the file
    name, which are plugin-authored bytes the model must see only as the
    receipt."""
    tool_use_id: str
    plugin: str
    slot: str
    label: str
    pages: int | None
    media_kind: str | None
    buttons: int | None = None     # S5: a proposal's button count


class PostLedger:
    """Append-only events keyed by the call's OWNER — the engagement id the
    identity carries, or a sync delegation's id — drained atomically by
    the vehicle that echoes them, so a post is echoed once and never
    twice. A list, never a set: two tools of one plugin may each deliver
    the same slot name, and two proven posts are two lines. In memory and
    FIFO-bounded on owners, like the broker's references: a restart between
    the post and its echo loses the echo, not the post."""

    def __init__(self, max_owners: int = 512, max_events: int | None = None) -> None:
        self._events: "collections.OrderedDict[str, list[PostEvent]]" = collections.OrderedDict()
        self._max_owners = max_owners
        # S4 §6: a ledger may bound events per owner too, oldest dropped — the
        # desk's resident echo asks for it (a chat's lines could grow without a
        # resident turn to drain them); the S3 post ledger does not, and keeps
        # every event of an owner as before.
        self._max_events = max_events
        self._lock = threading.Lock()

    def record(self, owner: str, event: Any) -> None:
        if not owner:
            return
        with self._lock:
            events = self._events.setdefault(owner, [])
            events.append(event)
            if self._max_events is not None:
                del events[:-self._max_events]
            self._events.move_to_end(owner)
            while len(self._events) > self._max_owners:
                self._events.popitem(last=False)

    def drain(self, owner: str) -> list[PostEvent]:
        """Read and clear *owner*'s events in one step."""
        if not owner:
            return []
        with self._lock:
            return self._events.pop(owner, [])


POSTS = PostLedger()


# -- S4 §2: the post map ----------------------------------------------------

@dataclasses.dataclass(frozen=True)
class PostRecord:
    """Who posted a message Casa sent on a plugin's or a desk's behalf: the
    specialist (``role``, the call's enforcement role), the operator it was
    posted for, and the slot/tool/owner of the post. The route (§3) reads
    ``role`` and ``operator_id``; nothing here is plugin-authored."""
    role: str
    operator_id: int
    plugin: str
    slot: str
    tool_use_id: str
    owner: str
    posted_at: float
    # the delivery kind (OPERATOR_LINK/MESSAGE/FILE, or ``desk_reply``): the
    # desk's outcome echo names a landed post by it, never by its body
    kind: str = ""


POST_MAP_MAX = 4096


class PostMap:
    """``(chat_id, message_id) → PostRecord`` for every physical message a
    delivered post produced — each page, each fallback chunk, the media
    message, the link message, a desk reply's pages — recorded by the channel
    AS IT LANDS, so a page the operator holds is routable even when a later
    page failed and the hook withheld the result. Memory-only and
    FIFO-bounded: a restart forgets it and an entry older than
    ``POST_MAP_MAX`` newer messages is evicted — a reply on either is a plain
    message to the resident (the fallback decision 2 names)."""

    def __init__(self, max_entries: int = POST_MAP_MAX) -> None:
        self._entries: "collections.OrderedDict[tuple[int, int], PostRecord]" = collections.OrderedDict()
        self._max = max_entries
        self._lock = threading.Lock()

    @staticmethod
    def _key(chat_id: Any, message_id: Any):
        if isinstance(chat_id, bool) or isinstance(message_id, bool):
            return None
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return None
        if chat_id <= 0 or message_id <= 0:
            return None
        return (chat_id, message_id)

    def record(self, chat_id: Any, message_id: Any, record: PostRecord) -> None:
        key = self._key(chat_id, message_id)
        if key is None:
            return
        with self._lock:
            self._entries[key] = record
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def get(self, chat_id: Any, message_id: Any) -> PostRecord | None:
        key = self._key(chat_id, message_id)
        if key is None:
            return None
        with self._lock:
            return self._entries.get(key)

    def owned(self, owner: str) -> list[PostRecord]:
        """Every retained record filed under *owner* (a delegation or desk
        turn id), in landing order — the complete account of what Casa
        posted for that turn, whatever the slot's kind (S4 §5.6)."""
        if not owner:
            return []
        with self._lock:
            return [r for r in self._entries.values() if r.owner == owner]


POST_MAP = PostMap()

_MEDIA_WORDS = {
    "document": "a document", "photo": "a photo", "audio": "an audio file",
    "voice": "a voice message", "zip": "a zip archive", "text": "a text file",
}


def echo_parts(events: list[PostEvent]) -> list[tuple[str, str]]:
    """The echo lines' parts, ``(label, outcome)``: one per proven post, at most
    ECHO_MAX_LINES, then ``("", "…and N more.")``. A composer that re-labels a
    line (S6's file turn) takes the outcome from here, never by stripping a
    label off the rendered line — the event's label may differ from the
    turn's (diff round 1, Astra)."""
    parts: list[tuple[str, str]] = []
    for event in events[:ECHO_MAX_LINES]:
        if getattr(event, "buttons", None) is not None:
            n = event.buttons
            lead = f"{event.pages} pages and " if event.pages else ""
            tail = f"posted {lead}a proposal to your chat ({n} button{'s' if n != 1 else ''})."
        elif event.media_kind:
            tail = f"posted {_MEDIA_WORDS.get(event.media_kind, 'a file')} to your chat."
        else:
            pages = event.pages or 1
            tail = f"posted to your chat ({pages} page{'s' if pages != 1 else ''})."
        parts.append((event.label, tail))
    if len(events) > ECHO_MAX_LINES:
        parts.append(("", f"…and {len(events) - ECHO_MAX_LINES} more."))
    return parts


def echo_lines(events: list[PostEvent]) -> list[str]:
    """§6: one Casa-authored line per proven post, at most ECHO_MAX_LINES
    then ``…and N more.``, each within ECHO_LINE_MAX characters; the label,
    the kind and the page count or media kind — nothing plugin-authored."""
    lines: list[str] = []
    for label, tail in echo_parts(events):
        if not label:
            lines.append(tail)
            continue
        room = ECHO_LINE_MAX - len(tail) - 1
        label = label if len(label) <= room else label[:room - 1] + "…"
        lines.append(f"{label} {tail}")
    return lines


def _post_owner(identity: Any) -> str:
    """The ledger key: the engagement id the identity carries, else the sync
    delegation's id threaded onto it as advisory metadata, else nothing (a
    direct resident turn has the post in front of the operator already)."""
    return (getattr(identity, "engagement_id", "") or ""
            or getattr(identity, "delegation_id", "") or "")


class ReferenceStore:
    """Thread-safe, memory-only escrow of deposited capabilities (#792).

    Everything here dies with the process (restart-losing by construction)
    and nothing is ever serialized. ``now`` is injectable so tests drive TTL
    expiry without sleeping, the ``GrantStore`` precedent."""

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._now = now
        self._refs: dict[str, _Reference] = {}
        self._inflight: dict[tuple[str, str], _InFlight] = {}

    # -- sweeping ---------------------------------------------------------
    def _sweep_locked(self) -> None:
        now = self._now()
        for ref, r in list(self._refs.items()):
            if r.used or r.expires_at <= now:
                del self._refs[ref]
        for key, call in list(self._inflight.items()):
            if call.opened_at + INFLIGHT_CAP_S <= now:
                # A swept call's result, if it ever arrives, is withheld
                # (no call in flight): its deposits go with it, exactly as
                # `drop_call_deposits` drops them on any withholding.
                for ref in call.deposits.values():
                    self._refs.pop(ref, None)
                del self._inflight[key]

    def sweep(self) -> None:
        with self._lock:
            self._sweep_locked()

    # -- in-flight calls --------------------------------------------------
    def open_call(self, *, client_id: str, artifact_id: str, tool_name: str,
                  tool_use_id: str, identity, provides: tuple,
                  delivers: dict | None = None, contract_map: Any = None,
                  protected: Any = None, entry: Any = None) -> None:
        """Register a ``capability`` call at admission. The identity is stored
        HERE, atomically with the call, so a deposit can bind it: the plugin's
        deposit request carries no identity and asserts none. ``delivers``
        (#1015) is the contract's ``{slot: kind}`` — the slot whose deposit
        the result hook delivers instead of passing."""
        with self._lock:
            self._sweep_locked()
            self._inflight[(client_id, tool_use_id)] = _InFlight(
                client_id=client_id, artifact_id=artifact_id,
                tool_name=tool_name, tool_use_id=tool_use_id,
                identity=identity, provides=tuple(provides),
                opened_at=self._now(), delivers=dict(delivers or {}),
                contract_map=contract_map, protected=protected, entry=entry)

    def close_call(self, client_id: str, tool_use_id: str):
        """Close a call (PostToolUse / PostToolUseFailure). Returns the record
        or ``None``. Deposits the result did not reference die with it."""
        with self._lock:
            return self._inflight.pop((client_id, tool_use_id), None)

    # -- deposit ----------------------------------------------------------
    def deposit(self, *, client_id: str, slot: str, value: str,
                caption: str | None = None,
                label: str | None = None,
                kind: Any = None,
                filename: Any = None,
                key: Any = None) -> tuple[str | None, str | None]:
        """Bind ``value`` to the UNIQUE in-flight capability call of
        ``client_id`` whose contract provides ``slot``; mint and return a
        reference. Zero or more than one such call ⇒ refused (fail closed).
        Returns ``(reference, None)`` or ``(None, error_code)``.

        For a slot the call DELIVERS the deposit is judged by the declared
        kind, BEFORE any reference is minted: ``operator_link`` (#1015) —
        an ``https`` link Casa can post, with optional ``caption``/``label``
        as printable single lines that cannot read as a link themselves
        (``bad_link`` / ``bad_caption`` / ``bad_label``); ``operator_message``
        (S3) — a non-blank body within the character cap with no control
        character but newline and tab (``bad_message``; caption, label and
        kind ignored — the plugin cannot influence the label);
        ``operator_file`` (S3) — ``kind`` a media-policy key (``bad_kind``)
        and an optional caption whose COMPOSED form fits the cap
        (``bad_caption``), and (S7a, INV-PLUG-047) an optional delivered
        ``filename``: refused ``filename_not_declared`` unless the call's
        entry declares ``"filename": true``, else ``bad_filename`` unless
        ``send_media``'s own predicate accepts it for the kind; the path
        itself is judged at delivery, never read here. For any other slot every extra member is ignored, so a
        producer library can send them uniformly."""
        with self._lock:
            self._sweep_locked()
            matches = [c for c in self._inflight.values()
                       if c.client_id == client_id and slot in c.provides]
            if not matches:
                return None, "no_call_in_flight"
            if len(matches) > 1:
                return None, "ambiguous_call"
            call = matches[0]
            if call.identity is None:
                return None, "no_identity"
            if slot in call.deposits:
                return None, "slot_already_deposited"
            caption_s, label_s, kind_s, filename_s, key_s = "", "", "", "", ""
            proposal_obj = None
            dkind = call.delivers.get(slot)
            if dkind is not None and key is not None:
                # #1312: a delivered slot's deposit may name the post; a
                # remembered name is not sent twice (delivery_keys)
                from delivery_keys import key_ok
                if not key_ok(key):
                    return None, "bad_key"
                key_s = key
            if dkind == OPERATOR_MESSAGE:
                if not _message_ok(value):
                    return None, "bad_message"
            elif dkind == OPERATOR_PROPOSAL:
                proposal_obj, _why = proposal_ok(value, call)
                if proposal_obj is None:
                    return None, "bad_proposal"
            elif dkind == OPERATOR_FILE:
                if not isinstance(kind, str) or kind not in MEDIA_POLICIES:
                    return None, "bad_kind"
                if caption is not None and caption != "":
                    if not _file_caption_ok(
                            caption, post_label(call.identity.enforcement_role)):
                        return None, "bad_caption"
                    caption_s = caption
                kind_s = kind
                if filename is not None and filename != "":
                    if not getattr(call.entry, "filename", False):
                        return None, "filename_not_declared"
                    import tools as tools_mod
                    if (not isinstance(filename, str)
                            or tools_mod._validate_delivery_filename(filename, kind) is None):
                        return None, "bad_filename"
                    filename_s = filename
            elif dkind is not None:
                # Only a DELIVERED slot judges the metadata (type included):
                # for any other slot both fields are ignored whatever they
                # are, exactly as the base ignored unknown request members.
                if not _link_ok(value):
                    return None, "bad_link"
                if caption is not None and caption != "":
                    if not _caption_ok(caption):
                        return None, "bad_caption"
                    caption_s = caption
                if label is not None and label != "":
                    if not _label_ok(label):
                        return None, "bad_label"
                    label_s = label
            ref = new_reference()
            now = self._now()
            self._refs[ref] = _Reference(
                value=value, slot=slot, identity=call.identity,
                minted_at=now, expires_at=now + reference_ttl_s(),
                caption=caption_s, label=label_s, media_kind=kind_s,
                proposal=proposal_obj, filename=filename_s, key=key_s)
            call.deposits[slot] = ref
            return ref, None

    def take_for_delivery(self, reference: str):
        """#1015: release a delivered slot's deposit ONCE to the result hook —
        the reference must exist, be unexpired, unused and unarmed; it is
        marked used (so ``arm``, ``redeem`` and a second take all refuse it;
        the next sweep removes it) and its value blanked. Returns
        ``(value, caption, label, identity, media_kind, proposal, filename, key)``
        or ``None``."""
        with self._lock:
            self._sweep_locked()
            r = self._refs.get(reference)
            if r is None or r.used or r.armed is not None:
                return None
            r.used = True
            value, r.value = r.value, ""
            return (value, r.caption, r.label, r.identity, r.media_kind, r.proposal,
                    r.filename, r.key)

    def validate_result(self, call: _InFlight, parsed: dict) -> bool:
        """True iff every declared slot of ``call`` is present in ``parsed``
        with EXACTLY the reference of the deposit bound to this call."""
        if not isinstance(parsed, dict):
            return False
        for slot in call.provides:
            expected = call.deposits.get(slot)
            if expected is None or parsed.get(slot) != expected:
                return False
        return True

    def is_no_link_result(self, call: _InFlight, parsed: dict) -> bool:
        """#1015 addendum (v0.319.0): True iff ``call`` delivers a slot, no
        deposit is bound to it, and ``parsed`` carries EVERY slot the call
        provides as JSON ``null`` — the producer's explicit statement that it
        created no link this time. A missing member or any other falsy value
        is not that statement."""
        if not isinstance(parsed, dict):
            return False
        with self._lock:
            return (bool(call.delivers) and not call.deposits
                    and bool(call.provides)
                    and all(slot in parsed and parsed[slot] is None
                            for slot in call.provides))

    def drop_call_deposits(self, call: _InFlight) -> None:
        """A capability result that failed validation was withheld: the
        references it minted must not be redeemable through a notice the
        model never saw."""
        with self._lock:
            for ref in call.deposits.values():
                self._refs.pop(ref, None)

    # -- arming and redemption ---------------------------------------------
    def arm(self, *, reference: str, identity, slot: str, client_id: str,
            tool_use_id: str) -> str | None:
        """Arm ``reference`` for redemption by the call ``tool_use_id`` in
        ``client_id``. Requires the same grant identity and the same slot.
        Re-arming replaces the previous arm and its ticket. Returns the
        ticket, or ``None`` when the reference is not redeemable here."""
        with self._lock:
            self._sweep_locked()
            r = self._refs.get(reference)
            if r is None or r.used or r.identity != identity or r.slot != slot:
                return None
            ticket = secrets.token_hex(16)
            r.armed = (client_id, tool_use_id, ticket)
            return ticket

    def redeem(self, *, client_id: str, reference: str, ticket: str) -> tuple[str | None, str | None]:
        """Release the value ONCE to the consumer whose admission armed it:
        same client, same ticket, arming call still in flight. Returns
        ``(value, None)`` or ``(None, error_code)``; never a value on error."""
        with self._lock:
            self._sweep_locked()
            r = self._refs.get(reference)
            if r is None or r.used:
                return None, "unknown_or_used"
            if r.armed is None:
                return None, "not_armed"
            armed_client, armed_call, armed_ticket = r.armed
            if armed_client != client_id:
                return None, "wrong_client"
            if not (isinstance(ticket, str)
                    and hmac.compare_digest(armed_ticket, ticket)):
                return None, "bad_ticket"
            if (armed_client, armed_call) not in self._inflight:
                return None, "call_not_in_flight"
            r.used = True
            value, r.value = r.value, ""
            del self._refs[reference]
            return value, None

    # -- lifecycle --------------------------------------------------------
    def purge_artifact(self, artifact_id: str) -> int:
        with self._lock:
            victims = [k for k, r in self._refs.items()
                       if getattr(r.identity, "artifact_id", None) == artifact_id]
            for k in victims:
                del self._refs[k]
            calls = [k for k, c in self._inflight.items()
                     if c.artifact_id == artifact_id]
            for k in calls:
                del self._inflight[k]
            return len(victims)

    def purge_role(self, role: str) -> int:
        with self._lock:
            victims = [k for k, r in self._refs.items()
                       if getattr(r.identity, "enforcement_role", None) == role]
            for k in victims:
                del self._refs[k]
            calls = [k for k, c in self._inflight.items()
                     if getattr(c.identity, "enforcement_role", None) == role]
            for k in calls:
                del self._inflight[k]
            return len(victims)

    # -- counts (tests) ---------------------------------------------------
    def reference_count(self) -> int:
        with self._lock:
            self._sweep_locked()
            return len(self._refs)

    def inflight_count(self) -> int:
        with self._lock:
            self._sweep_locked()
            return len(self._inflight)


STORE = ReferenceStore()


# ---------------------------------------------------------------------------
# Hook payload helpers
# ---------------------------------------------------------------------------


def _withheld(plugin_seg: str, reason: str) -> dict[str, Any]:
    """The PostToolUse replacement. Quotes no result bytes and no field names;
    the plugin segment is the one the model already sees in the tool name."""
    body = json.dumps({
        "casa_result_withheld": True,
        "plugin": plugin_seg,
        "reason": reason,
    })
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "updatedToolOutput": body}}


_REASON_NON_ADOPTING = (
    "The plugin has not adopted the Casa result contract (casa.resultContract), "
    "so its tool results are withheld. The tool ran; its result was not "
    "delivered. Tell the operator the plugin needs updating; do not retry.")
_REASON_UNDECLARED = (
    "The tool is not declared under the plugin's result contract, so its "
    "result is withheld. Tell the operator the plugin needs updating; do not "
    "retry.")
_REASON_UNKNOWN_PLUGIN = (
    "The plugin could not be matched to a resolved artifact, so its result is "
    "withheld. Tell the operator; do not retry.")
_REASON_BAD_CAPABILITY = (
    "The tool is declared to return a capability, but its result did not carry "
    "the references its contract declares, so the result is withheld. Tell the "
    "operator the plugin needs updating; do not retry.")
_REASON_LINK_NOT_DELIVERED = (
    "The tool produced a link for the operator, but Casa could not confirm it "
    "reached their chat, so the result is withheld. Tell the operator: if a "
    "link message arrived just now it is valid; otherwise ask again for a "
    "fresh one. Do not retry on this turn.")
# S3: the same shape for a message and a file — withheld, dropped, no retry.
_REASON_MESSAGE_NOT_DELIVERED = (
    "The tool produced a message for the operator, but Casa could not confirm "
    "it reached their chat complete, so the result is withheld. Tell the "
    "operator: if a message headed by the specialist's name arrived just now "
    "it is theirs; otherwise ask again. Do not retry on this turn.")
_REASON_FILE_NOT_DELIVERED = (
    "The tool produced a file for the operator, but Casa could not confirm it "
    "reached their chat, so the result is withheld and the file consumed. Tell "
    "the operator: if a file headed by the specialist's name arrived just now "
    "it is theirs; otherwise ask again for a fresh one. Do not retry on this "
    "turn.")
_NOT_DELIVERED_REASONS = {
    OPERATOR_LINK: _REASON_LINK_NOT_DELIVERED,
    OPERATOR_MESSAGE: _REASON_MESSAGE_NOT_DELIVERED,
    OPERATOR_FILE: _REASON_FILE_NOT_DELIVERED,
}
_REASON_PROPOSAL_NOT_DELIVERED = (
    "casa could not post the proposal to the operator's chat; it was withheld "
    "and holds no stored call — do not retry blindly")
_REASON_PROPOSAL_TOO_MANY = (
    "casa withheld the proposal: too many open proposals in this chat; let the "
    "operator answer or let them expire")
_NOT_DELIVERED_REASONS[OPERATOR_PROPOSAL] = _REASON_PROPOSAL_NOT_DELIVERED
# #1341: ``_post_proposal``'s withheld value for an in-place edit whose landing
# is unconfirmed — only ever returned with ``edit_message_id``
EDIT_UNCONFIRMED = "edit unconfirmed"
_KIND_WORDS = {OPERATOR_LINK: "link", OPERATOR_MESSAGE: "message", OPERATOR_FILE: "file",
               OPERATOR_PROPOSAL: "proposal"}

_DENY_NON_ADOPTING = (
    "not executed: this plugin has not adopted the Casa result contract "
    "(casa.resultContract), so its tools other than its setup tool are refused. "
    "Tell the operator the plugin needs updating; do not retry.")
_DENY_UNDECLARED = (
    "not executed: this tool is not declared under the plugin's result "
    "contract. Tell the operator the plugin needs updating; do not retry.")
_DENY_UNKNOWN_PLUGIN = (
    "not executed: the plugin could not be matched to a resolved artifact. "
    "Tell the operator; do not retry.")
_DENY_NO_IDENTITY = (
    "not executed: a tool that returns a capability can only be called on a "
    "turn bound to the configured operator (a direct message, a button, or an "
    "active engagement). Do not retry on this turn.")
_DENY_STALE_REFERENCE = (
    "not executed: a capability reference passed to this call is not "
    "redeemable here (unknown, already used, expired, minted for another "
    "session, or for a different capability). Fetch a fresh one; do not retry "
    "with the same reference.")
_DENY_INTERNAL = "not executed: internal result-broker error"
_DENY_ERASE_BINDING = (
    "not executed: this erase turn may run only the eraser the operator "
    "approved, for the plugin version the approval named, while Casa is still "
    "waiting for its result — nothing was erased")


_DENY_ERASING = (
    "not executed: this plugin's data is being erased for an uninstall the "
    "operator chose, so its tools are refused until the uninstall finishes or "
    "the erasure stops. Tell the operator; do not retry.")


def _erasing(name: str) -> bool:
    from plugin_erasure import FENCE
    return FENCE.fenced(name)


def _erase_turn() -> "tuple[str, str] | None":
    """``(tapped artifact, run id)`` on an erase-marked turn (#1046), else
    None."""
    import agent as agent_mod
    from plugin_erasure import erase_turn
    return erase_turn(agent_mod.origin_var.get(None))


def _erase_run_of(contract_map, tool_name: str) -> str | None:
    """On an erase-marked turn, its run id when this session's binding carries
    the plugin of *tool_name* at the artifact the tap named; else None."""
    turn = _erase_turn()
    if not turn or not turn[0] or not turn[1]:
        return None
    seg = contract_map.plugin_seg_of(tool_name)
    plugin = contract_map.plugins.get(seg) if seg is not None else None
    if plugin is None or plugin.artifact_id != turn[0]:
        return None
    return turn[1]


def _deny(reason: str) -> dict[str, Any]:
    from hooks import _deny as _hooks_deny
    return _hooks_deny(reason)


def _response_text(tool_response: Any) -> str | None:
    """Fold the CLI's projection of an MCP result to one string: a ``str``
    (measured: a JSON string for a structured result) or a list of content
    blocks (measured: a dict-returning tool). Anything else ⇒ ``None``."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, list):
        parts = []
        for block in tool_response:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    return None
                parts.append(text)
            else:
                return None
        return "".join(parts)
    return None


def _parse_object(text: str) -> dict | None:
    if len(text) > MAX_RESPONSE_BYTES:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# The hooks
# ---------------------------------------------------------------------------


def make_plugin_admission_hook(
    role: str,
    contract_map,
    *,
    client_id: str,
    authz_hook: Callable | None = None,
    protected: dict | None = None,
    store: ReferenceStore | None = None,
    owner=None,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """The composite PreToolUse callback for plugin tools (#792 §3.3).

    ``owner`` (S5 §5.3): on a pinned one-call turn the ``PinnedRun`` whose pin
    is this hook's FIRST check — a plugin call that is not the stored call is
    denied here, before the erase binding, the fence, protection, the
    authorization decision (which posts a challenge) or reference arming;
    the body runs inside the owner's guard (sealed ⇒ no effect).

    In order: contract admission (setup exemption; non-adopting or undeclared
    ⇒ deny before execution; a capability call registers in flight), then the
    authorization decision for a PROTECTED tool (``authz_hook`` — the
    ``make_resident_authz_hook`` callable, invoked as a function; its texts and
    behaviour unchanged), then — only if both allowed — arming of the
    references in declared ``consumes`` parameters, with ``updatedInput``.
    Every exception is a deny, never a pass."""
    store = store or STORE
    protected = protected or {}

    async def _body(input_data, tool_use_id, context):
        tool_name = (input_data or {}).get("tool_name", "")
        if not isinstance(tool_name, str) or not tool_name.startswith(PLUGIN_TOOL_PREFIX):
            return {}
        try:
            if owner is not None:
                # S5 §5.3: the pin, before anything with a side effect
                why = owner.pin(tool_name, (input_data or {}).get("tool_input") or {})
                if why is not None:
                    return _deny(why)
            seg = contract_map.plugin_seg_of(tool_name)
            plugin = contract_map.plugins.get(seg) if seg is not None else None
            entry = contract_map.tools.get(tool_name) if plugin is not None else None
            # #1046: an erase-marked turn exists to run ONE eraser: a plugin
            # tool runs on it only when an erase episode is waiting for that
            # exact tool on the artifact the operator's tap named, and this
            # session's binding carries that artifact. Anything else — another
            # version (a publish between the tap and the session build), an
            # episode that already timed out and stopped waiting, another tool
            # — is refused before execution; an unprotected eraser meets no
            # grant check at all, so this is its only gate.
            erase_admitted = False
            if _erase_turn() is not None:
                import agent as agent_mod
                from plugin_erasure import WATCH, erase_turn_question_open
                run_id = _erase_run_of(contract_map, tool_name)
                if (run_id is None or not WATCH.is_armed(run_id, tool_name)
                        or not erase_turn_question_open(
                            agent_mod.origin_var.get(None))):
                    return _deny(_DENY_ERASE_BINDING)
                # The operator's Erase tap on the open question IS the approval
                # of exactly this call, and the checks above bind it to this
                # run: a protected eraser is admitted here, never through a
                # grant an ordinary turn could consume.
                erase_admitted = True
            # #1070: while a plugin's erasure runs for an uninstall — until the
            # uninstall removes it, or the erasure ends not complete — every
            # other call of its tools, on any turn, is refused (its setup tool
            # included): a write in that window would outlive the erasure.
            if (not erase_admitted and plugin is not None and plugin.name
                    and _erasing(plugin.name)):
                return _deny(_DENY_ERASING)
            # The exempt setup tool: declared absent or safe. A setup tool
            # declared as a CAPABILITY (#1015, it delivers its link) takes
            # the capability path like any other tool.
            is_setup = (plugin is not None and tool_name in plugin.setup_tools
                        and (entry is None or entry.kind != "capability"))
            if is_setup:
                entry = None
            else:
                if plugin is None:
                    return _deny(_DENY_UNKNOWN_PLUGIN)
                if not plugin.adopted:
                    return _deny(_DENY_NON_ADOPTING)
                if entry is None:
                    return _deny(_DENY_UNDECLARED)

            # Authorization for a protected tool — sequenced BEFORE arming.
            if (authz_hook is not None and tool_name in protected
                    and not erase_admitted):
                verdict = await authz_hook(input_data, tool_use_id, context)
                if verdict:
                    return verdict

            if entry is None:
                return {}                      # the exempt setup tool
            tool_input = (input_data or {}).get("tool_input") or {}
            if not isinstance(tool_input, dict):
                tool_input = {}
            needs_identity = entry.kind == "capability" or bool(entry.consumes)
            identity = None
            if needs_identity:
                from authz_grants import resolve_grant_identity
                identity, _why = resolve_grant_identity(
                    role, artifact_id=entry.artifact_id)
                if identity is None:
                    return _deny(_DENY_NO_IDENTITY)
            if entry.kind == "capability":
                store.open_call(
                    client_id=client_id, artifact_id=entry.artifact_id,
                    tool_name=tool_name, tool_use_id=str(tool_use_id or ""),
                    identity=identity, provides=entry.provides,
                    delivers=getattr(entry, "delivers", None),
                    # S5 §2: a proposal's buttons are judged against the
                    # session's own maps and this tool's server
                    contract_map=contract_map, protected=protected, entry=entry)
            if entry.consumes:
                updated = dict(tool_input)
                armed_any = False
                for param, slot in entry.consumes.items():
                    value = tool_input.get(param)
                    if not is_reference(value):
                        continue           # an inert string; not ours to judge
                    ticket = store.arm(
                        reference=value, identity=identity, slot=slot,
                        client_id=client_id, tool_use_id=str(tool_use_id or ""))
                    if ticket is None:
                        return _deny(_DENY_STALE_REFERENCE)
                    updated[param] = f"{value}:{ticket}"
                    armed_any = True
                if armed_any:
                    if entry.kind != "capability":
                        # The consumer's redemption window: open until this
                        # call's PostToolUse/PostToolUseFailure (or the cap).
                        # provides=() so no deposit can ever bind to it.
                        store.open_call(
                            client_id=client_id, artifact_id=entry.artifact_id,
                            tool_name=tool_name,
                            tool_use_id=str(tool_use_id or ""),
                            identity=identity, provides=())
                    return {"hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "updatedInput": updated,
                    }}
            return {}
        except Exception:  # noqa: BLE001 — fail closed, never let it escape
            logger.exception(
                "result broker admission error (tool=%s role=%s) — denying",
                tool_name, role)
            return _deny(_DENY_INTERNAL)

    _hook = owner.guard(_body) if owner is not None else _body
    _hook._casa_pinned = owner                        # type: ignore[attr-defined]
    _hook._casa_result_broker = "admission"          # type: ignore[attr-defined]
    _hook._casa_result_broker_client = client_id     # type: ignore[attr-defined]
    if authz_hook is not None:
        # The option-wiring tests find the authz decision by these markers.
        _hook._casa_authz_role = getattr(authz_hook, "_casa_authz_role", role)  # type: ignore[attr-defined]
        _hook._casa_authz_deps_factory = getattr(                              # type: ignore[attr-defined]
            authz_hook, "_casa_authz_deps_factory", None)
    return _hook


def compose_operator_link(value: str, *, caption: str = "", label: str = ""):
    """#1015: the ONE message Casa posts for a delivered link — ``(text,
    entities, plain)`` for ``TelegramChannel.deliver_operator_link``. The
    link text is ``"<label> (<host>)"`` with the host printed by Casa from
    the URL (lower-cased ``urlsplit().hostname``), never supplied by the
    plugin; one ``text_link`` entity spans it, its length in UTF-16 code
    units (Telegram's unit); the caption follows on its own line as the
    bytes it is (this never goes through the markdown renderer). ``plain``
    is the fallback the channel sends when the entity is refused: the same
    text with the URL spelled out."""
    from telegram import MessageEntity
    from text_util import utf16_len
    host = (urlsplit(value).hostname or "").lower()
    label = label or DEFAULT_LINK_LABEL
    link_text = f"{label} ({host})"
    tail = f"\n{caption}" if caption else ""
    entities = [MessageEntity(type=MessageEntity.TEXT_LINK, offset=0,
                              length=utf16_len(link_text), url=value)]
    return link_text + tail, entities, f"{link_text}: {value}{tail}"


async def _post_operator_link(chat_id: int, text: str, entities, plain: str,
                              post: "PostRecord | None" = None):
    """Reach the Telegram channel the way the delegated authz factory does
    (``tools._channel_manager``); absent ⇒ ``NOT_DELIVERED``. ``post`` (S4)
    is the record the channel files each landed message under."""
    from channels import DeliveryOutcome
    channel = _telegram_channel()
    if channel is None:
        return DeliveryOutcome.NOT_DELIVERED
    return await channel.deliver_operator_link(chat_id, text, entities, plain, post=post)


def _telegram_channel():
    """The Telegram channel the way the delegated authz factory reaches it
    (``tools._channel_manager``); ``None`` when absent."""
    import tools as tools_mod
    manager = getattr(tools_mod, "_channel_manager", None)
    return manager.get("telegram") if manager is not None else None


async def _post_operator_message(chat_id: int, text: str,
                                 post: "PostRecord | None" = None):
    """S3: the channel judges the whole physical plan of *text* before the
    first send and posts every page; absent ⇒ ``NOT_DELIVERED``. ``post``
    (S4) is the record the channel files each landed page under."""
    from channels import DeliveryOutcome
    channel = _telegram_channel()
    if channel is None:
        return DeliveryOutcome.NOT_DELIVERED
    return await channel.deliver_operator_message(chat_id, text, post=post)


async def _post_operator_page(chat_id: int, text: str, post: "PostRecord | None" = None):
    """#1377: one page before a card, as ONE message; absent ⇒ ``NOT_DELIVERED``."""
    from channels import DeliveryOutcome
    channel = _telegram_channel()
    if channel is None:
        return DeliveryOutcome.NOT_DELIVERED
    return await channel.deliver_operator_page(chat_id, text, post=post)


async def _post_operator_proposal(chat_id: int, text: str, labels: list, rid: str,
                                  post: "PostRecord | None" = None):
    """S5: the channel posts the labelled text with one button per label
    (``v1|proposal|<rid>|<i>``) and files the sent message under *post*;
    returns the message id, or ``None`` when nothing landed."""
    channel = _telegram_channel()
    if channel is None:
        return None
    return await channel.deliver_operator_proposal(chat_id, text, labels, rid, post=post)


async def _edit_operator_proposal(chat_id: int, message_id: int, text: str, labels: list,
                                  rid: str, post: "PostRecord | None" = None):
    """#1339: the channel edits *message_id* to the labelled text with one
    button per label (``v1|proposal|<rid>|<i>``); returns the message id, or
    ``None`` when the edit did not land."""
    channel = _telegram_channel()
    if channel is None:
        return None
    return await channel.replace_operator_proposal(chat_id, message_id, text, labels, rid,
                                                   post=post)


def _claim_and_capture(outbox, path: str, kind: str, delivered_name: str = ""):
    """ONE synchronous unit, run off the loop: claim *path*, validate the
    staged name for *kind* (S7a: the deposit's validated *delivered_name*,
    when given, is only the name the file is sent under — never which file), capture the bytes through the kind's policy,
    and remove the claim in its own ``finally``. Returns ``((content,
    filename), None)`` or ``(None, why)``. One unit because cancelling a
    thread does not stop it: if the caller's bound ends while the claim is
    waiting on the outbox lock, the claim still lands — and is still
    consumed here, which an awaited sequence of three threads could not
    promise."""
    import tools as tools_mod
    from plugin_outbox import OutboxError
    try:
        claim = outbox.claim(path)
    except OutboxError as exc:
        return None, f"claim refused ({exc.kind})"
    try:
        filename = tools_mod._validate_delivery_filename(os.path.basename(path), kind)
        if filename is None:
            return None, f"name not valid for kind {kind}"
        filename = delivered_name or filename
        try:
            content = outbox.capture(claim, kind)
        except OutboxError as exc:
            return None, f"policy refused ({exc.kind})"
        return (content, filename), None
    finally:
        try:
            outbox.remove_claim(claim)
        except Exception as exc:  # noqa: BLE001 — cleanup best-effort
            logger.warning("operator file claim cleanup failed: %s", type(exc).__name__)


async def _post_operator_file(chat_id: int, path: str, kind: str, caption: str,
                              post: "PostRecord | None" = None, delivered_name: str = ""):
    """S3: claim *path* from the plugin outbox exactly as ``send_media``
    claims it (the outbox derived from the authenticated engagement, else
    the shared one), run the kind's policy through ``capture``, and send
    the bytes with the composed caption. The claim is removed on EVERY
    outcome — the file is consumed whether or not it was delivered, even
    when the claim lands after the hook's bound. A claim, name or policy
    refusal is ``NOT_DELIVERED``; a send error propagates and the hook
    reads it as not proven."""
    import tools as tools_mod
    from channels import DeliveryOutcome
    channel = _telegram_channel()
    if channel is None:
        return DeliveryOutcome.NOT_DELIVERED
    outbox = tools_mod.outbox_for_current_context()
    if outbox is None:
        return DeliveryOutcome.NOT_DELIVERED
    captured, why = await asyncio.to_thread(_claim_and_capture, outbox, path, kind,
                                            delivered_name)
    if captured is None:
        logger.warning("operator file not sent: %s", why)
        return DeliveryOutcome.NOT_DELIVERED
    content, filename = captured
    return await channel.deliver_operator_file(chat_id, content, kind, filename, caption,
                                               post=post)


async def _post_proposal(identity, seg: str, slot: str, call: _InFlight, proposal: dict,
                         head: str, post: "PostRecord", warning: str | None = None,
                         on_proven: "Callable[..., None] | None" = None,
                         edit_message_id: int | None = None) -> tuple:
    """S5 §3: ONE synchronous block — count, supersede by revision, register
    with the finish hook — then the post; a post that is not proven
    unregisters at once (``unregister`` fires no hook; nothing is on screen).
    #1305: a post whose landing is UNCONFIRMED (the bound fired, or the
    channel raised ``UnconfirmedDelivery``) keeps its registration until the
    TTL — its card may be on screen, and a tap binds its message id — and is
    returned as not delivered, since nothing proves it.
    Returns ``(delivered, detail, event, withheld_reason)``.

    ``warning`` (§14.7, the ``More`` exception with a rewritten input): the
    tell line is composed INTO the message above the label when the result
    still fits one page; otherwise the proposal lands without it and the
    tell goes out as ONE labelled desk notice right after the post — the
    landed proposal stays the sole visible receipt and the tell is never
    silently dropped.

    ``edit_message_id`` (#1339): the card replaces the tapped card in place —
    the channel EDITS that message instead of sending one. The target is
    known in advance, so the synchronous block also binds the record to it
    and files it in the post map under *post*, before the edit: an
    unconfirmed edit's record can then still be marked by its finish hook,
    and a swipe-reply on the message routes (design round 1).

    ``pages`` (#1377): after the synchronous block and before the card, each
    page goes as one labelled message (``deliver_operator_page``, filed
    under *post* as it lands), all under one ``DELIVERY_TIMEOUT_S`` bound; a
    page that does not land sends no card and the record is unregistered. The
    proof — and the receipt's ``pages`` count — needs every page and the card."""
    from channels.tg_richtext import render_paged
    from text_util import utf16_len
    from verdict_broker import BROKER
    chat_id, role = int(identity.chat_id), str(identity.enforcement_role)
    scope = f"proposal:{chat_id}"
    revision = str(proposal.get("revision") or "")
    labels = [b["label"] for b in proposal["buttons"]]
    text = compose_operator_message(proposal["text"], head)
    tell_after = False
    if warning:
        with_tell = f"{warning}\n{text}"
        if len(render_paged(with_tell)) == 1 and utf16_len(with_tell) <= 4096 - PROPOSAL_SETTLE_RESERVE:
            text = with_tell
        else:
            tell_after = True
    # the synchronous block: no await between the count and the register
    if len(BROKER.pending(namespace="proposal", scope=scope)) >= PROPOSAL_MAX_LIVE:
        return False, {}, None, _REASON_PROPOSAL_TOO_MANY
    if revision:
        BROKER.cancel_where(
            namespace="proposal", reason="superseded",
            predicate=lambda req: (req.scope == scope
                                   and req.meta.get("plugin_seg") == seg
                                   and req.meta.get("role") == role
                                   and req.meta.get("revision") == revision))
    rid = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    meta = {
        "deadline": loop.time() + PROPOSAL_TTL_S, "chat_id": chat_id,
        "operator_id": int(identity.operator_id), "role": role,
        "artifact_id": str(getattr(identity, "artifact_id", "") or ""),
        "plugin_seg": seg,
        # S6 §2.4: `calls` keeps one entry per button (None at an arm_file or close index, so every
        # reader keeps its indexing); `kinds` is the parallel per-button kind
        "calls": [dict(b["call"]) if "call" in b else None for b in proposal["buttons"]],
        # #1362: a `keep_card` button's tap settles nothing (INV-PROP-010); #1375: a
        # `close` button's tap only removes the keyboard (INV-PROP-011)
        "kinds": ["arm_file" if b.get("arm_file") else "keep_card" if b.get("keep_card")
                  else "close" if b.get("close") else "call" for b in proposal["buttons"]],
        "revision": revision, "label": head, "text": text, "options": list(labels),
        "message_id": None, "owner": _post_owner(identity), "_scope": scope,
    }
    req, _created = BROKER.register(
        namespace="proposal", scope=scope, request_id=rid,
        timeout_s=PROPOSAL_TTL_S, detached=True, supersede=False, meta=meta)
    if edit_message_id is not None:
        req.meta["message_id"] = edit_message_id
        POST_MAP.record(chat_id, edit_message_id, post)
    channel = _telegram_channel()
    factory = getattr(channel, "proposal_finish_hook", None)
    if factory is not None:
        BROKER.set_finish_hook(req, factory(rid=rid, req=req))
    from channels import DeliveryOutcome, UnconfirmedDelivery
    delivered = unconfirmed = False
    # #1377: the plain pages, each one labelled message, go before the card
    pages = [compose_operator_message(p, head) for p in proposal.get("pages") or ()]
    landed = 0
    try:
        if pages:
            async def _send_pages() -> bool:
                nonlocal landed
                for page in pages:
                    outcome = await _post_operator_page(chat_id, page, post=post)
                    if outcome is not DeliveryOutcome.DELIVERED:
                        return False
                    landed += 1
                return True
            try:
                pages_ok = await asyncio.wait_for(_send_pages(), DELIVERY_TIMEOUT_S)
            except asyncio.TimeoutError:
                pages_ok = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — not proven: no card follows
                logger.warning("proposal page send failed: %s", type(exc).__name__)
                pages_ok = False
            if not pages_ok:
                logger.warning("proposal withheld: %d of %d pages landed", landed, len(pages))
                return False, {}, None, _pages_reason(landed, len(pages))
        try:
            send = (_post_operator_proposal(chat_id, text, labels, rid, post=post)
                    if edit_message_id is None else
                    _edit_operator_proposal(chat_id, edit_message_id, text, labels, rid,
                                            post=post))
            mid = await asyncio.wait_for(send, DELIVERY_TIMEOUT_S)
        except (asyncio.TimeoutError, UnconfirmedDelivery) as exc:
            # #1305: the card may be on screen. It stays registered until its
            # TTL, and a tap binds its message id; the caller is told what it
            # is told for a failure, since nothing here proves the post.
            unconfirmed = True
            logger.warning("proposal post unconfirmed (%s); its buttons stay live until "
                           "the TTL", type(exc).__name__)
            mid = None
        delivered = isinstance(mid, int) and not isinstance(mid, bool)
        if delivered:
            req.meta["message_id"] = mid        # the broker's own dict, by reference
            if on_proven is not None:
                # #1312: the proof point — before the mark or the tell below await
                on_proven(_proposal_detail(rid, len(labels), len(pages)), (scope, rid),
                          PostEvent(call.tool_use_id, seg, slot, head, len(pages) or None,
                                    None, buttons=len(labels)))
    finally:
        if not delivered and not unconfirmed:
            BROKER.unregister(namespace="proposal", scope=scope, request_id=rid)
    if not delivered:
        if unconfirmed and edit_message_id is not None:
            return False, {}, None, EDIT_UNCONFIRMED
        return False, {}, None, (_pages_reason(landed, len(pages)) if landed else None)
    settled = req.meta.get("settled_line")
    if settled:
        # the record settled (superseded, expired) while this send was in
        # flight: its keyboard landed after the finish hook ran, so the
        # terminal line is applied here, where the message id is known
        mark = getattr(channel, "mark_proposal", None)
        if mark is not None:
            try:
                await asyncio.wait_for(mark(req.meta, settled), DELIVERY_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 — the post is proven; the mark is logged
                logger.warning("late proposal mark failed: %s", type(exc).__name__)
    if tell_after:
        notice = getattr(channel, "deliver_desk_notice", None)
        if notice is not None:
            try:
                await asyncio.wait_for(notice(chat_id, f"{head} {warning}"), DELIVERY_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 — the post is proven; the tell is logged
                logger.warning("proposal tell notice failed: %s", type(exc).__name__)
    n = len(labels)
    event = PostEvent(call.tool_use_id, seg, slot, head, len(pages) or None, None, buttons=n)
    return True, _proposal_detail(rid, n, len(pages)), event, None


def _proposal_detail(rid: str, buttons: int, pages: int) -> dict:
    """A proven proposal's receipt detail; ``pages`` only for a card that had them."""
    detail: dict[str, Any] = {"proposal_id": rid, "buttons": buttons}
    if pages:
        detail["pages"] = pages
    return detail


def _pages_reason(landed: int, total: int) -> str | None:
    """#1377: what a withheld card with pages tells the call — the pages that
    landed are on screen; ``None`` (the plain withheld reason) when none did."""
    if not landed:
        return None
    return (f"casa posted {landed} of {total} pages but could not post the card after "
            "them; the card was withheld and holds no stored call — do not retry blindly")


def _repeat_of(seg: str, identity: Any, key: str, dkind: str) -> dict | None:
    """#1312: the remembered delivery this keyed deposit repeats, or None. A
    proposal repeats only while its original keyboard is live, unclaimed and
    registered under the same artifact as this call — after an update the tap
    path refuses the old card, so a fresh one is posted instead."""
    from delivery_keys import KEYS
    try:
        hit = KEYS.lookup(seg, int(identity.operator_id), key)
    except Exception:  # noqa: BLE001 — an unusable memory never blocks a post
        logger.warning("delivery key lookup failed; delivering fresh", exc_info=True)
        return None
    if hit is None or hit.get("kind") != dkind:
        return None
    if dkind == OPERATOR_PROPOSAL:
        from verdict_broker import BROKER
        scope, rid = (hit.get("proposal") or [None, None])[:2]
        if not (isinstance(scope, str) and isinstance(rid, str)):
            return None
        if not BROKER.is_live_unclaimed(namespace="proposal", scope=scope, request_id=rid):
            return None
        meta = BROKER.get_meta(namespace="proposal", scope=scope, request_id=rid) or {}
        if meta.get("artifact_id") != str(getattr(identity, "artifact_id", "") or ""):
            return None
        # the tap path's own expiry reading: past its deadline a keyboard
        # answers "expired" even before the broker's timer has retired it
        deadline = meta.get("deadline")
        if (not isinstance(deadline, (int, float))
                or asyncio.get_running_loop().time() >= deadline):
            return None
    return hit


async def _settle_repeat(seg: str, identity: Any, dkind: str, value: str,
                         warning: str | None) -> None:
    """#1312: what a repeat still owes, nothing of it sent as the post again.
    A file deposit's staged file is consumed (best-effort: the original
    delivery stands either way); a ``More`` call's rewritten-input tell goes
    out as one labelled desk notice, since no card carries it this time."""
    if dkind == OPERATOR_FILE:
        try:
            import tools as tools_mod
            outbox = tools_mod.outbox_for_current_context()
            if outbox is not None:
                def _consume() -> None:
                    claim = outbox.claim(value)
                    outbox.remove_claim(claim)
                await asyncio.to_thread(_consume)
        except Exception as exc:  # noqa: BLE001 — the original delivery stands
            logger.warning("repeat file not consumed: %s", type(exc).__name__)
    elif dkind == OPERATOR_PROPOSAL and warning:
        channel = _telegram_channel()
        notice = getattr(channel, "deliver_desk_notice", None)
        if notice is not None:
            try:
                head = post_label(identity.enforcement_role)
                await asyncio.wait_for(notice(int(identity.chat_id), f"{head} {warning}"),
                                       DELIVERY_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 — the tell is logged
                logger.warning("repeat proposal tell failed: %s", type(exc).__name__)


async def _deliver_and_replace(store: ReferenceStore, seg: str, call: _InFlight,
                               parsed: dict, warning: str | None = None) -> dict[str, Any]:
    """#1015, after the structural check passed: take the delivered slot's
    deposit once, post it, and REPLACE the result — with the receipt on
    proven delivery, with the not-delivered notice on anything else (the
    deposit dropped either way: a delivered reference is used, a withheld
    one must not be redeemable through a notice). The hook's own
    cancellation (the CLI's deadline) drops the deposit and re-raises: no
    replacement, the model holds the delivery-neutral original.

    S3 dispatches on the delivered slot's kind; everything around the
    dispatch — take-once, the bound, withhold-and-drop, the receipt,
    cancellation — is the link path's. A message receipt carries the page
    count of the plan that was judged, a file receipt the media kind; a
    proven message or file post is also recorded on the echo ledger under
    the call's owner (§6)."""
    from channels import DeliveryOutcome
    slot = next(iter(call.delivers))
    dkind = call.delivers[slot]
    delivered = False
    detail: dict[str, Any] = {}
    event: PostEvent | None = None
    withheld_reason: str | None = None
    identity = None
    echoed = False          # #1312: a proposal's echo filed at its proof
    try:
        taken = store.take_for_delivery(call.deposits.get(slot, ""))
        key = taken[7] if taken is not None else ""
        if not key:
            hold = contextlib.nullcontext()
        else:
            from delivery_keys import KEYS
            hold = KEYS.hold(seg, int(taken[3].operator_id), key)
        async with hold:
            if taken is not None and key:
                hit = _repeat_of(seg, taken[3], key, dkind)
                if hit is not None:
                    # #1312: this post was already delivered — nothing is sent
                    # again; the call gets the original delivery's receipt
                    identity = taken[3]
                    await _settle_repeat(seg, identity, dkind, taken[0], warning)
                    # the settle awaited: a proposal's original may have been
                    # superseded or claimed meanwhile — decide again, with no
                    # await before the receipt; a card no longer usable is
                    # posted fresh (its tell already went out). Other kinds
                    # keep the first decision: a file's staged copy is consumed
                    if dkind == OPERATOR_PROPOSAL:
                        hit = _repeat_of(seg, identity, key, dkind)
                    if hit is not None:
                        delivered = True
                        detail = {**hit["detail"], "repeat": True}
                        taken = None
                    elif dkind == OPERATOR_PROPOSAL:
                        warning = None

            def _remember(proven: dict, proposal_key: tuple | None = None,
                          proven_event: "PostEvent | None" = None) -> None:
                """#1312: synchronous, at the instant the send is proven — before
                any later await, so a cancellation after the proof cannot skip it.
                A proposal's echo is filed here too: a repeat never re-files it."""
                nonlocal echoed
                if proven_event is not None:
                    POSTS.record(_post_owner(identity), proven_event)
                    echoed = True
                if not key:
                    return
                try:
                    from delivery_keys import KEYS
                    KEYS.record(seg, int(identity.operator_id), key, kind=dkind,
                                detail=proven, proposal=proposal_key)
                except Exception:  # noqa: BLE001 — never undoes the delivery
                    logger.warning("delivery key not recorded", exc_info=True)

            if taken is not None:
                value, caption, label, identity, media_kind, proposal, filename, _key = taken
                # S4 §2: the record the channel files every landed message under
                # — the identity's role and operator, never anything the plugin
                # authored.
                post = PostRecord(
                    role=identity.enforcement_role, operator_id=identity.operator_id,
                    plugin=seg, slot=slot, tool_use_id=call.tool_use_id,
                    owner=_post_owner(identity), posted_at=time.time(), kind=dkind)
                if dkind == OPERATOR_MESSAGE:
                    head = post_label(identity.enforcement_role)
                    text = compose_operator_message(value, head)
                    outcome = await asyncio.wait_for(
                        _post_operator_message(identity.chat_id, text, post=post),
                        DELIVERY_TIMEOUT_S)
                    delivered = outcome is DeliveryOutcome.DELIVERED
                    if delivered:
                        from channels.tg_richtext import render_paged
                        pages = len(render_paged(text))
                        detail = {"pages": pages}
                        _remember(detail)
                        event = PostEvent(call.tool_use_id, seg, slot, head, pages, None)
                elif dkind == OPERATOR_FILE:
                    head = post_label(identity.enforcement_role)
                    outcome = await asyncio.wait_for(
                        _post_operator_file(identity.chat_id, value, media_kind,
                                            compose_file_caption(head, caption),
                                            post=post, delivered_name=filename),
                        FILE_DELIVERY_TIMEOUT_S)
                    delivered = outcome is DeliveryOutcome.DELIVERED
                    if delivered:
                        detail = {"kind": media_kind}
                        _remember(detail)
                        event = PostEvent(call.tool_use_id, seg, slot, head, None, media_kind)
                elif dkind == OPERATOR_PROPOSAL:
                    head = post_label(identity.enforcement_role)
                    delivered, detail, event, withheld_reason = await _post_proposal(
                        identity, seg, slot, call, proposal, head, post, warning=warning,
                        on_proven=_remember)
                else:
                    text, entities, plain = compose_operator_link(
                        value, caption=caption, label=label)
                    outcome = await asyncio.wait_for(
                        _post_operator_link(identity.chat_id, text, entities, plain,
                                            post=post),
                        DELIVERY_TIMEOUT_S)
                    delivered = outcome is DeliveryOutcome.DELIVERED
                    if delivered:
                        _remember(detail)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — not proven ⇒ withheld
        # The class only: an upstream error's text could quote the request
        # (the URL is in the entity and the plain fallback, a body in a
        # page), and no error echoes a value.
        logger.warning(
            "operator %s delivery failed (plugin=%s slot=%s): %s — withholding",
            _KIND_WORDS.get(dkind, "link"), seg, slot, type(exc).__name__)
        delivered = False
    finally:
        if not delivered:
            store.drop_call_deposits(call)
    if not delivered:
        return _withheld(seg, withheld_reason
                         or _NOT_DELIVERED_REASONS.get(dkind, _REASON_LINK_NOT_DELIVERED))
    if event is not None and not echoed:
        POSTS.record(_post_owner(identity), event)
    receipt = dict(parsed)
    receipt["casa_delivery"] = {
        "slot": slot, "status": "delivered", "to": DELIVERED_TO, **detail}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "updatedToolOutput": json.dumps(receipt)}}


def make_result_hook(
    contract_map, *, client_id: str, store: ReferenceStore | None = None, owner=None,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """The PostToolUse callback (#792 §3.3): the second boundary. Any
    exception is the withheld replacement, never a pass. A ``capability``
    result whose call delivers a slot (#1015) is, after the structural
    check, replaced by the delivery receipt or the not-delivered notice —
    never passed as returned.

    ``owner`` (S5 §5.4): on a pinned one-call turn, the stored call's
    reported input is compared with the stored canonical at ENTRY (trust and
    tell, §14.7: told and logged at ERROR, never prevented) and the watch is
    resolved at the END with this hook's OWN effective result — the
    response text for a ``safe`` tool, the delivery receipt / the withheld
    replacement / the no-post pass for the ``More`` exception — inside the
    owner's guard."""
    store = store or STORE

    async def _body(input_data, tool_use_id, context, warning=None):
        tool_name = (input_data or {}).get("tool_name", "")
        if not isinstance(tool_name, str) or not tool_name.startswith(PLUGIN_TOOL_PREFIX):
            return {}
        seg = "?"
        try:
            seg = contract_map.plugin_seg_of(tool_name) or "?"
            plugin = contract_map.plugins.get(seg)
            entry = contract_map.tools.get(tool_name) if plugin is not None else None
            # #1046: an erase episode's capture, before any early return (an
            # eraser is declared ``safe``). The result itself passes unchanged.
            run_id = _erase_run_of(contract_map, tool_name)
            if run_id is not None:
                from plugin_erasure import WATCH
                WATCH.resolve(run_id, tool_name, text=_response_text(
                    (input_data or {}).get("tool_response")))
            if (plugin is not None and tool_name in plugin.setup_tools
                    and (entry is None or entry.kind != "capability")):
                return {}      # the exempt setup tool (declared absent or safe)
            if plugin is None:
                return _withheld(seg, _REASON_UNKNOWN_PLUGIN)
            if not plugin.adopted:
                return _withheld(seg, _REASON_NON_ADOPTING)
            if entry is None:
                return _withheld(seg, _REASON_UNDECLARED)
            call = store.close_call(client_id, str(tool_use_id or ""))
            if entry.kind == "safe":
                return {}
            text = _response_text((input_data or {}).get("tool_response"))
            parsed = _parse_object(text) if text is not None else None
            if (call is not None and parsed is not None
                    and store.is_no_link_result(call, parsed)):
                # A delivering tool that created no link says so with null
                # slots and no deposit: its result passes unchanged, with no
                # receipt and nothing sent (INV-PLUG-028).
                return {}
            if call is None or parsed is None or not store.validate_result(call, parsed):
                if call is not None:
                    store.drop_call_deposits(call)
                return _withheld(seg, _REASON_BAD_CAPABILITY)
            if not call.delivers:
                return {}
            return await _deliver_and_replace(store, seg, call, parsed, warning=warning)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — fail closed
            logger.exception(
                "result broker replacement error (tool=%s) — withholding",
                tool_name)
            return _withheld(seg, _REASON_BAD_CAPABILITY)

    if owner is None:
        _hook = _body
    else:
        async def _hook(input_data, tool_use_id, context):
            tool_name = (input_data or {}).get("tool_name", "")
            mine = tool_name == owner.runtime_name
            warning = None
            if mine:
                from stored_calls import TELL_LINE
                _compare_reported(owner, input_data, tool_name)
                warning = TELL_LINE if owner.rewritten else None
            out = await _body(input_data, tool_use_id, context, warning)
            if mine:
                owner.resolve(_capture_of(contract_map, tool_name, input_data, out,
                                          owner.rewritten))
            return out
        _hook = owner.guard(_hook)
    _hook._casa_pinned = owner                        # type: ignore[attr-defined]
    _hook._casa_result_broker = "result"             # type: ignore[attr-defined]
    return _hook


def _compare_reported(owner, input_data, tool_name: str) -> None:
    """S5 §14.7 trust and tell: the CLI's reported input of the stored call
    compared with the stored canonical — told (``owner.rewritten``) and
    logged at ERROR, never prevented. Both post-hooks run it (#1303: a call
    that ended in PostToolUseFailure ran with those arguments too)."""
    from stored_calls import canonical_json
    try:
        reported = canonical_json((input_data or {}).get("tool_input") or {})
    except (TypeError, ValueError):
        reported = "<unserialisable>"
    if reported != owner.canonical:
        owner.rewritten = True
        logger.error(
            "stored call %s: the CLI reported this call's arguments changed by "
            "an installed hook (tool=%s stored=%s reported=%s)",
            owner.run_id, tool_name, owner.canonical, reported)


def _receipt_of(text: str) -> tuple[str, str]:
    """#1200: the operator-readable receipt of a stored call's passed-through
    response — the top-level ``receipt`` string of a JSON-object response when
    it has a non-whitespace character, otherwise the text verbatim. A ``safe``
    response was never parsed before this, so a nesting too deep for the
    parser falls back rather than raising inside the hook.

    #1302: returns ``(receipt, next, in_place)`` — ``next`` is the response's
    ``next`` object, JSON-encoded, only beside a usable ``receipt`` string;
    ``""`` otherwise (nothing is judged here). #1339: ``in_place`` is True only
    when ``next`` is an object and the response's ``in_place`` is JSON ``true``."""
    try:
        parsed = _parse_object(text)
    except RecursionError:
        parsed = None
    receipt = parsed.get("receipt") if parsed is not None else None
    if not (isinstance(receipt, str) and receipt.strip()):
        return text, "", False
    nxt = parsed.get("next")
    if not isinstance(nxt, dict):
        return receipt, "", False
    return receipt, json.dumps(nxt, ensure_ascii=False), parsed.get("in_place") is True


def _capture_of(contract_map, tool_name: str, input_data, out, rewritten: bool):
    """S5 §5.4: the result hook's EFFECTIVE result as a ``Capture`` — never the
    raw response of a delivering tool (a broker reference must not be
    posted). A passed-through result's receipt is its ``receipt`` sentence
    when it carries one (#1200), else its text verbatim."""
    from pinned_run import Capture
    entry = contract_map.tools.get(tool_name)
    if not out:
        kind = "receipt" if entry is None or entry.kind == "safe" else "no_post"
        text = _response_text((input_data or {}).get("tool_response")) or ""
        receipt, nxt, in_place = _receipt_of(text)
        # #1302: only a ``safe`` tool's receipt carries a next card; the
        # ``More`` exception's no-post shape keeps its own path
        if kind != "receipt":
            nxt, in_place = "", False
        return Capture(kind, receipt, rewritten, next=nxt, in_place=in_place)
    body = (out.get("hookSpecificOutput") or {}).get("updatedToolOutput") if isinstance(out, dict) else None
    parsed = None
    if isinstance(body, str):
        # the hook's OWN replacement, not a raw response: no size ceiling (a
        # receipt is the producer's result PLUS Casa's delivery field, so a
        # response at the raw ceiling has a receipt above it), and Casa's
        # delivery status read FIRST — the producer's fields ride inside the
        # receipt, and one of them may carry Casa's withheld key by accident
        # (Astra, diff round 1)
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
    if isinstance(parsed, dict):
        delivery = parsed.get("casa_delivery")
        if isinstance(delivery, dict) and delivery.get("status") == "delivered":
            # #1303: the delivered kind from the tool's own contract, never the response
            delivers = dict(getattr(entry, "delivers", None) or {})
            dkind = next(iter(delivers.values()), "") if len(delivers) == 1 else ""
            return Capture("delivered", str(dkind), rewritten=rewritten)
        if parsed.get("casa_result_withheld"):
            return Capture("withheld", str(parsed.get("reason") or "withheld"), rewritten)
    return Capture("withheld", "unexpected result", rewritten)


def make_failure_hook(
    contract_map, *, client_id: str, store: ReferenceStore | None = None, owner=None,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """PostToolUseFailure housekeeping: the event carries no ``tool_response``
    and its output has no replacement field (measured: an MCP tool error
    fires this event and the error text reaches the model), so this only
    closes the in-flight call so its deposits do not outlive it. ``owner``
    (S5 §5.4): the stored call's failure resolves the watch as ``error`` with
    a class token only — never the error text, which may be the plugin's."""
    store = store or STORE

    async def _body(input_data, tool_use_id, context):
        try:
            tool_name = (input_data or {}).get("tool_name", "")
            if isinstance(tool_name, str) and tool_name.startswith(PLUGIN_TOOL_PREFIX):
                call = store.close_call(client_id, str(tool_use_id or ""))
                if call is not None:
                    store.drop_call_deposits(call)
                run_id = _erase_run_of(contract_map, tool_name)
                if run_id is not None:
                    from plugin_erasure import WATCH
                    error = (input_data or {}).get("error")
                    WATCH.resolve(run_id, tool_name,
                                  error=str(error) if error else "tool error")
        except Exception:  # noqa: BLE001
            logger.exception("result broker failure-hook error (tool=%s)",
                             (input_data or {}).get("tool_name"))
        return {}

    if owner is None:
        _hook = _body
    else:
        async def _hook(input_data, tool_use_id, context):
            out = await _body(input_data, tool_use_id, context)
            tool_name = (input_data or {}).get("tool_name")
            if tool_name == owner.runtime_name:
                from pinned_run import Capture
                _compare_reported(owner, input_data, tool_name)
                owner.resolve(Capture("error", "tool_error", owner.rewritten))
            return out
        _hook = owner.guard(_hook)
    _hook._casa_pinned = owner                        # type: ignore[attr-defined]
    _hook._casa_result_broker = "failure"            # type: ignore[attr-defined]
    return _hook


def broker_matchers(
    role: str, resolution, *, client_id: str,
    authz_hook: Callable | None = None, protected: dict | None = None,
    store: ReferenceStore | None = None, owner=None,
) -> dict[str, list]:
    """The three ``HookMatcher`` lists a plugin-bearing SDK session appends —
    code-side, beside the authz hook, never from a hooks document. ``owner``
    (S5): on a pinned one-call turn the contract map is the one the tap
    CAPTURED under the desk lock — never rebuilt here — and the three
    callbacks run inside the owner's guard."""
    from claude_agent_sdk import HookMatcher

    if owner is not None:
        contract_map = owner.build_input.contract_map
    else:
        from plugin_grants import result_contract_map
        contract_map = result_contract_map(resolution)
    # #1015: the timeout is set explicitly — the result hook now awaits a
    # Telegram send (bounded by DELIVERY_TIMEOUT_S) and the CLI cancels a
    # hook past this deadline and proceeds with the ORIGINAL result.
    return {
        "PreToolUse": [HookMatcher(
            matcher=PLUGIN_TOOL_MATCHER, timeout=HOOK_TIMEOUT_S,
            hooks=[make_plugin_admission_hook(
                role, contract_map, client_id=client_id,
                authz_hook=authz_hook, protected=protected, store=store, owner=owner)])],
        "PostToolUse": [HookMatcher(
            matcher=PLUGIN_TOOL_MATCHER, timeout=HOOK_TIMEOUT_S,
            hooks=[make_result_hook(contract_map, client_id=client_id, store=store,
                                    owner=owner)])],
        "PostToolUseFailure": [HookMatcher(
            matcher=PLUGIN_TOOL_MATCHER, timeout=HOOK_TIMEOUT_S,
            hooks=[make_failure_hook(contract_map, client_id=client_id, store=store,
                                     owner=owner)])],
    }


def broker_env(client_id: str) -> dict[str, str]:
    """The two environment keys the CLI's stdio MCP servers inherit (measured:
    they do) so a producer can deposit and a consumer can redeem."""
    return {ENV_CLIENT: client_id, ENV_SOCKET: BROKER_SOCKET_PATH}


# ---------------------------------------------------------------------------
# The two routes on the internal Unix socket
# ---------------------------------------------------------------------------


def _bad(code: str, status: int = 200) -> web.Response:
    return web.json_response({"error": code}, status=status)


def build_broker_deposit_handler(store: ReferenceStore | None = None):
    """``POST /internal/broker/deposit`` ``{"client", "slot", "value"}`` plus
    the optional ``"caption"``/``"label"`` strings a delivered slot may carry
    (#1015) and the ``"kind"`` an ``operator_file`` slot requires (S3) ⇒
    ``{"reference"}`` or ``{"error"}``. Write-only: nothing here reads a
    value back, and no error echoes one."""
    store = store or STORE

    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _bad("bad_json")
        if not isinstance(body, dict):
            return _bad("bad_json")
        client = body.get("client")
        slot = body.get("slot")
        value = body.get("value")
        if not isinstance(client, str) or not client:
            return _bad("bad_client")
        if not isinstance(slot, str) or not slot:
            return _bad("bad_slot")
        if not isinstance(value, str) or not value:
            return _bad("bad_value")
        if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            return _bad("value_too_large")
        # Passed through as they are: the store judges them only for a
        # delivered slot (#1015); for any other slot they are ignored, so a
        # producer library can send them uniformly and the base's behaviour
        # for such a deposit is unchanged.
        ref, err = store.deposit(client_id=client, slot=slot, value=value,
                                 caption=body.get("caption"),
                                 label=body.get("label"),
                                 kind=body.get("kind"),
                                 filename=body.get("filename"),
                                 key=body.get("key"))
        if err:
            return _bad(err)
        return web.json_response({"reference": ref})

    return handler


def build_broker_redeem_handler(store: ReferenceStore | None = None):
    """``POST /internal/broker/redeem`` ``{"client", "reference", "ticket"}``
    ⇒ ``{"value"}`` exactly once, or ``{"error"}``."""
    store = store or STORE

    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _bad("bad_json")
        if not isinstance(body, dict):
            return _bad("bad_json")
        client = body.get("client")
        reference = body.get("reference")
        ticket = body.get("ticket")
        if not isinstance(client, str) or not client:
            return _bad("bad_client")
        if not is_reference(reference):
            return _bad("bad_reference")
        if not isinstance(ticket, str) or not _TICKET_RE.fullmatch(ticket):
            return _bad("bad_ticket")
        value, err = store.redeem(client_id=client, reference=reference, ticket=ticket)
        if err:
            return _bad(err)
        return web.json_response({"value": value})

    return handler
