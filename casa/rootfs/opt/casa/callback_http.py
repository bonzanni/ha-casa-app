"""The public ``GET /callback/{name}`` endpoint (INV-CB-001/002/
004/005/006).

This is the authorization-callback facility's only unauthenticated surface,
and the only one an attacker can address directly. Three properties shape
every line below.

**One response, always.** Success and every refusal cause — unknown name,
missing/malformed state, unknown state, expired pending, replay, an existing
result, a write failure, an internal fault — return the *same* 303 to the
fixed, query-less ``/callback/done``. A differentiated status, header or
target would be an enumeration oracle: it would tell a prober which effective
names are routed and which states are live. That is also why there is no 429:
flood handling damps internal *emission* (below), never the response
(INV-CB-005). Nothing on the request path can raise past the
outer guard, because a 500 is a differentiated response.

**Nothing query-derived is logged, and nothing request-derived is rendered.**
The query string carries a bearer credential (an OAuth authorization code).
Handler logs carry a reason enum, a cid and the *effective* name only —
except ``unknown_name``, which logs a fixed sentinel, since the route
component is attacker-controlled. The access logger suppresses the query for
``/callback/`` (see ``casa_core_middleware.QUERY_SUPPRESSED_PREFIXES``);
setup-nginx.sh adds the matching nginx ``map`` rule for nginx's own access log. The
two pages are static constants: zero interpolation of request data
(INV-CB-006).

One INV-CB-006 leak lives below aiohttp, on the ``aiohttp.server`` logger:
a request line over 8190 bytes raises ``LineTooLong`` *before* the handler
runs, and aiohttp's ``handle_error`` logs the exception at ERROR — the
exception message embeds the offending bytes, i.e. the query with the code.
:func:`install_callback_log_redaction` closes it: an idempotent
``logging.Filter`` on the ``aiohttp.server`` logger redacts the whole
request TARGET (path AND query) of a request-line target in a record's message
*and its exception traceback* before any handler formats it. Redacting the
entire token — not just the query — is what makes it robust against an inner
quote in the query, which no longer leaves a boundary to leak the tail after
it. The match is on the request-target SHAPE in REQUEST-LINE context (a
bytes-literal opener or an HTTP method right before the ``/``-target), not the
literal ``/callback`` — a percent-encoded path (``/%63allback/…``) routes to
/callback but keeps its raw encoded form in the pre-routing traceback, so a
``/callback``-anchored pattern would miss it. Requiring request-line context is
what keeps the filter from eating a plain ``/path?x=y`` inside an unrelated
diagnostic, while still closing the whole encoding-bypass class. Casa wires it
at app setup; the residual is therefore closed in-container (the outer proxy's
own logs remain operator-configured).

**Opaque relay.** Casa interprets exactly one parameter — ``state``, parsed
strictly from the RAW query string, never the framework's decoded view, so a
percent-encoded or duplicated ``state`` cannot smuggle a different value past
the charset gate. Everything else is recorded verbatim (``raw_query``) plus
an ordered decoded view (``query``) for the consumer; no provider-specific
parsing exists here (INV-CB-004).

The route produces no turn: no ingress-identity row, no clearance, no
provenance. That is deliberate — a browser redirect is not an authenticated
principal, and the deposit is picked up by the plugin that minted the state.
"""

from __future__ import annotations

import logging
import re
import os
import time
import traceback
from typing import Any, Callable

from aiohttp import web

import callback_episodes
import callback_spool
from rate_limit import RateLimiter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

DONE_PATH = "/callback/done"

#: Sampling threshold, per key, per minute. ``0`` (and any negative or
#: unparseable value) clamps to the default rather than disabling the bucket:
#: ``RateLimiter`` reads capacity 0 as "no limit", which would turn the
#: sampler into an unbounded log amplifier under exactly the flood it exists
#: to damp.
RATE_ENV = "CALLBACK_RATE_PER_MIN"
DEFAULT_RATE_PER_MIN = 60

#: Logged in place of an unrouted route component (INV-CB-006): the component
#: is attacker-controlled and must never reach a log line.
UNKNOWN_SENTINEL = "<unknown>"

# Outcome vocabulary: ``ok``, ``unknown_name``, ``bad_state``, ``no_pending``,
# ``expired``, ``result_exists``, ``write_failed``. ``expired`` and
# ``result_exists`` are part of the facility's vocabulary but are NOT
# reachable from this handler:
# ``CallbackSpool.claim`` refuses expired, replayed and never-minted states
# identically by design, so the handler sees one refusal and logs
# ``no_pending`` (the sweep/recovery passes own the ``expired`` case); and
# the EEXIST anomaly inside ``publish_result`` — a result already existing
# for a claimed state — cleans up and reports ``PUBLISHED``, so it logs
# ``ok``. Publish failures log ``write_failed`` on every arm (recorded,
# unrecorded, and raised alike).

#: State grammar — checked against the RAW value, so a
#: percent-escape is a rejection rather than a decode.
STATE_MIN = 22
STATE_MAX = 256
_STATE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

#: Pre-filesystem caps. aiohttp already bounds the request
#: line, but the record is written to ``/data`` and read by a plugin, so the
#: captured size is bounded here too. A violation is a neutral refusal — no
#: truncated record is ever stored, because a truncated credential is worse
#: than none.
MAX_RAW_QUERY = 4096
MAX_PAIRS = 32
MAX_KEY = 128
MAX_VALUE = 2048

#: Sent on BOTH the 303 and the done page.
SECURITY_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store, private",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
}

#: The one user-visible string of the whole facility. Deliberately neutral:
#: it is shown for a provider error and for a replayed link just as for a
#: success, and it nudges the operator back to the chat where the plugin is
#: waiting. Static — no request data reaches it.
DONE_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Casa</title>
<style>
:root { color-scheme: light dark; }
body { margin: 0; min-height: 100vh; display: flex; align-items: center;
       justify-content: center; font-family: system-ui, -apple-system,
       "Segoe UI", Roboto, sans-serif; background: #f6f7f9; color: #14161a; }
main { max-width: 30rem; padding: 2rem; text-align: center; }
p { font-size: 1.125rem; line-height: 1.6; margin: 0; }
@media (prefers-color-scheme: dark) {
  body { background: #14161a; color: #f6f7f9; }
}
</style>
</head>
<body>
<main>
<p>Response received. You can close this tab and return to your chat.</p>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# aiohttp.server log redaction (INV-CB-006, the below-the-handler surface)
# ---------------------------------------------------------------------------

#: The origin-form request TARGET of an actual HTTP request line — the only
#: shape that can leak a callback credential onto the ``aiohttp.server``
#: logger. The WHOLE target (path AND query) is redacted, not just the query:
#: a diagnostic never needs the target's path or query, and redacting the
#: entire token is what makes the redaction robust against an inner quote in
#: the query (``?x='&code=…``) — there is no "stop at a quote" boundary left to
#: leak the tail after it. The target appears two ways, and this pattern covers
#: both while leaving unrelated diagnostics (``/data/file?reason=…``,
#: ``https://api/token?error=…``) untouched:
#:
#: * the C parser (the container default) raises ``LineTooLong`` from its URL
#:   callback with the raw target wrapped in a Python bytes repr and NO method
#:   — ``bytearray(b'/callback/x?code=…')``, or ``bytearray(b"…")`` when the
#:   bytes contain a single quote (repr flips the delimiter). The opening quote
#:   is CAPTURED so the target is consumed up to the MATCHING closing quote,
#:   escape-aware (``\'``, ``\\``), never a mismatched inner one;
#: * the pure-python parser embeds the whole request line, method included
#:   (``…b'GET /callback/x?code=…'`` — or a bare ``GET /callback/x?… HTTP/1.1``),
#:   so a method token + whitespace immediately before the target matches too,
#:   the target ending at the first whitespace.
#:
#: Requiring one of those two request-line anchors — a bytes-literal opener or
#: a word-boundary HTTP method — is what keeps a bare ``/path?x=y`` sitting
#: inside an ordinary diagnostic message (no method, no bytes repr) intact.
#: Matching the target SHAPE rather than the literal ``/callback`` still closes
#: the encoding-bypass class: a percent-encoded ``/%63allback/…`` keeps its raw
#: encoded form in the pre-routing error and is redacted the same way.
_HTTP_METHODS = "GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH|TRACE|CONNECT"
_REQUEST_TARGET_RE = re.compile(
    # Bytes-repr form: b'…' / b"…" (optional embedded method); consume the
    # target to the matching closing quote, escape-aware.
    r"(?P<pre>b(?P<q>['\"])(?:(?:" + _HTTP_METHODS + r")[ \t]+)?)"
    r"/(?:(?!(?P=q))[^\\]|\\.)*"
    r"|"
    # Bare request line: METHOD SP target (…SP HTTP/x). Word-boundary method so
    # it does not fire inside a longer word; the target ends at whitespace.
    r"(?P<bare>\b(?:" + _HTTP_METHODS + r")[ \t]+)"
    r"/\S*"
)


def _redact_target(match: "re.Match[str]") -> str:
    """Keep the request-line anchor, replace the whole target with a marker."""
    anchor = match.group("pre")
    if anchor is None:
        anchor = match.group("bare")
    return anchor + "<redacted>"


class _AiohttpServerRedactor(logging.Filter):
    """Strip a ``/callback`` request target from an ``aiohttp.server`` record
    before any handler formats it — INV-CB-006's third surface.

    aiohttp raises ``LineTooLong`` on an over-long request line *before* the
    route is matched, and ``handle_error`` logs it at ERROR with the offending
    bytes in the exception; the target (with the code) rides the traceback, not
    the ``%``-message. This filter therefore redacts BOTH the formatted
    message and the exception text, and — because a logger's filters run in
    ``Logger.handle`` before ``callHandlers`` — the mutation reaches every
    handler, including root, already sanitized.

    The trigger is an origin-form target in REQUEST-LINE context
    (:data:`_REQUEST_TARGET_RE`, which requires a bytes-literal opener or an
    HTTP method immediately before the target), not the bare ``/callback``
    substring — so a percent-encoded path is covered too, while an unrelated
    ``aiohttp.server`` record mentioning a plain ``/data/file?reason=…`` path
    (no method, no bytes repr) does not match and keeps its ``exc_info``
    untouched. When it DOES match, the WHOLE target (path and query) is
    replaced, the redacted traceback is pre-rendered onto ``record.exc_text``
    and ``exc_info`` is cleared; ``JsonFormatter`` (casa's production default)
    and ``HumanFormatter`` both fall back to ``exc_text``, so the ``exc`` field
    survives — redacted, not dropped.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a broken record must still be logged
            message = str(record.msg)
        redacted = _REQUEST_TARGET_RE.sub(_redact_target, message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        if record.exc_info or record.exc_text:
            text = record.exc_text
            if text is None and record.exc_info:
                text = "".join(traceback.format_exception(*record.exc_info))
            if text and _REQUEST_TARGET_RE.search(text):
                record.exc_text = _REQUEST_TARGET_RE.sub(_redact_target, text)
                record.exc_info = None
        return True


def install_callback_log_redaction() -> None:
    """Attach :class:`_AiohttpServerRedactor` to the ``aiohttp.server`` logger
    once. Idempotent across reloads and repeated boots — the guard inspects
    the live filter list rather than a module flag, so a second casa app in
    the same process (tests) shares the one filter."""
    srv = logging.getLogger("aiohttp.server")
    if not any(isinstance(f, _AiohttpServerRedactor) for f in srv.filters):
        srv.addFilter(_AiohttpServerRedactor())


# ---------------------------------------------------------------------------
# query parsing
# ---------------------------------------------------------------------------


def _segments(raw_query: str) -> list[str] | None:
    """Split a raw query on ``&`` and apply the pre-filesystem caps.

    ``None`` means "refuse" — the caller renders the same neutral response it
    renders for everything else.
    """
    if len(raw_query) > MAX_RAW_QUERY:
        return None
    segments = [seg for seg in raw_query.split("&") if seg]
    if len(segments) > MAX_PAIRS:
        return None
    for seg in segments:
        key, _sep, value = seg.partition("=")
        if len(key) > MAX_KEY or len(value) > MAX_VALUE:
            return None
    return segments


def _extract_state(raw_query: str) -> str | None:
    """The single ``state`` value, or ``None`` for every malformed case.

    Parsed from the RAW query on purpose: the framework's
    decoded view merges duplicates and percent-decodes, either of which would
    let a prober present one value to casa's charset gate and another to the
    filesystem. Requires *exactly one* ``state`` occurrence — a bare ``state``
    counts as one — length 22-256 and the URL-safe charset. No decoding
    happens, so ``%20`` or ``+`` is a rejection, not a space.
    """
    segments = _segments(raw_query)
    if segments is None:
        return None
    found: str | None = None
    seen = 0
    for seg in segments:
        key, sep, value = seg.partition("=")
        if key != "state":
            continue
        seen += 1
        if seen > 1:
            return None
        found = value if sep else ""
    if found is None:
        return None
    if not (STATE_MIN <= len(found) <= STATE_MAX):
        return None
    if not all(ch in _STATE_CHARS for ch in found):
        return None
    return found


def _decode_component(raw: str) -> str:
    """Decode one key or value of the query string.

    ``+`` becomes a space; a valid ``%XX`` percent-decodes to that byte; a
    malformed ``%`` sequence is left verbatim (a provider that emits one is
    buggy, not hostile, and the consumer still has ``raw_query`` to work
    from). The assembled bytes decode as UTF-8 with ``errors="replace"``, so
    an invalid encoding yields a record rather than an exception on the
    request path.
    """
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == "+":
            out.append(0x20)
            i += 1
            continue
        if ch == "%":
            pair = raw[i + 1:i + 3]      # short at the end of the string
            if len(pair) == 2 and all(c in _HEX_DIGITS for c in pair):
                out.append(int(pair, 16))
                i += 3
                continue
        out += ch.encode("utf-8", "replace")
        i += 1
    return out.decode("utf-8", errors="replace")


def _decoded_pairs(raw_query: str) -> list[list[str]]:
    """The ordered decoded view: duplicates, order and bare keys preserved
    (a bare key decodes to ``["key", ""]``). Only the FIRST ``=`` splits, so
    an unencoded ``=`` inside a value survives."""
    pairs: list[list[str]] = []
    for seg in raw_query.split("&"):
        if not seg:
            continue
        key, _sep, value = seg.partition("=")
        pairs.append([_decode_component(key), _decode_component(value)])
    return pairs


# ---------------------------------------------------------------------------
# outcome sampler
# ---------------------------------------------------------------------------


def _rate_per_min(env: dict[str, str] | None = None) -> int:
    """Read :data:`RATE_ENV`, clamping 0, negatives and junk to the default.

    Same shape as ``casa_core._env_int_or`` but with the clamp the sampler
    needs: this bucket must never be *disabled*, only sized.
    """
    env = env if env is not None else os.environ
    raw = env.get(RATE_ENV)
    if raw is None or raw == "":
        return DEFAULT_RATE_PER_MIN
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d",
                       RATE_ENV, raw, DEFAULT_RATE_PER_MIN)
        return DEFAULT_RATE_PER_MIN
    if value <= 0:
        logger.warning("%s=%d disables log sampling; using default %d",
                       RATE_ENV, value, DEFAULT_RATE_PER_MIN)
        return DEFAULT_RATE_PER_MIN
    return value


class OutcomeSampler:
    """Token bucket over *log emission only*.

    Keys are bounded by construction: one per routed effective name plus one
    shared :data:`UNKNOWN_SENTINEL` key for every unrouted probe, so an
    attacker cycling names cannot grow the bucket dict. The keying is
    invisible externally — it gates ``logger.info``, never the response — so
    it creates no oracle.

    :meth:`should_log` returns ``(emit, suppressed_since_last_emit)``; the
    caller emits a one-line summary when the second value is non-zero, so a
    flood stays diagnosable from the logs it did not print.
    """

    def __init__(self, *, capacity: int | None = None,
                 now: Callable[[], float] | None = None) -> None:
        self._limiter = RateLimiter(
            capacity=capacity if capacity is not None else _rate_per_min(),
            window_s=60.0, now=now,
        )
        self._suppressed: dict[str, int] = {}

    def should_log(self, key: str) -> tuple[bool, int]:
        if self._limiter.check(key).allowed:
            # pop, not read: a key with nothing suppressed leaves no residue,
            # so the dict tracks only keys currently under damping.
            return True, self._suppressed.pop(key, 0)
        self._suppressed[key] = self._suppressed.get(key, 0) + 1
        return False, 0


# ---------------------------------------------------------------------------
# delivery-nudge seam
# ---------------------------------------------------------------------------


def _nudge(plugin: str, result_hash: str) -> None:
    """Signal the in-process delivery-nudge worker that a result landed.

    Deliberately a module-level function (not a handler argument) so the
    wiring — ``callback_episodes`` — replaces one seam rather than every
    construction site, and so tests can observe the kick.

    Stays **non-durable and O(1) on the request path**:
    :func:`callback_episodes.kick` only sets the worker's wake event — no ``_load``/``_save`` — because the episode ledger
    is a load-scan-rewrite JSON store and writing it here would make request
    work grow with ledger size. Durability comes from the recovery invariant
    instead — any result without a settled episode is re-enqueued by the boot
    and periodic passes — so a lost kick converges rather than dropping the
    flow.
    """
    callback_episodes.kick(plugin, result_hash)


# ---------------------------------------------------------------------------
# responses
# ---------------------------------------------------------------------------


def _neutral_redirect() -> web.Response:
    """THE response. Every outcome returns this and only this — one
    constructor, so no early return can drift from another (INV-CB-005).

    Built explicitly rather than via ``HTTPSeeOther`` so the body, charset and
    content type are pinned here instead of tracking aiohttp's exception
    rendering.
    """
    return web.Response(
        status=303, body=b"", content_type="text/plain", charset="utf-8",
        headers={"Location": DONE_PATH, **SECURITY_HEADERS},
    )


def make_done_handler():
    """``GET /callback/done`` — the static neutral page.

    Registered BEFORE the wildcard in ``casa_core`` so it can never fall
    through to it. Effective names always carry the ``plg-`` prefix, so
    ``done`` can never be a real callback name either; the ordering is belt
    and braces, and a redirect-following test pins the no-loop property.
    """
    async def done_handler(request: web.Request) -> web.Response:
        return web.Response(
            text=DONE_PAGE, content_type="text/html", charset="utf-8",
            headers=dict(SECURITY_HEADERS),
        )

    return done_handler


# ---------------------------------------------------------------------------
# the handler
# ---------------------------------------------------------------------------


def make_callback_handler(
    *,
    trigger_registry: Any,
    spool_provider: Callable[[], Any] | None = None,
    sampler: OutcomeSampler | None = None,
    clock: Callable[[], float] = time.time,
):
    """Build the ``GET /callback/{name}`` handler.

    ``spool_provider`` is resolved per request (default
    :func:`callback_spool.get_spool`) so a boot that has not wired the spool
    yet — or a reload that replaced it — degrades to a neutral refusal
    instead of holding a stale reference.
    """
    provider = spool_provider or callback_spool.get_spool
    sampler = sampler if sampler is not None else OutcomeSampler()

    def _emit(key: str, reason: str, cid: str) -> None:
        """One INFO per outcome, sampled. Carries the reason enum, the key
        and the cid — never the state, its hash, or any query content."""
        try:
            emit, suppressed = sampler.should_log(key)
            if not emit:
                return
            if suppressed:
                logger.info(
                    "callback: %d outcome lines suppressed for name=%s "
                    "since the last one (flood damping)", suppressed, key)
            logger.info("callback: outcome=%s name=%s cid=%s",
                        reason, key, cid)
        except Exception:  # noqa: BLE001 — observability must not shape the response
            pass

    async def callback_handler(request: web.Request) -> web.Response:
        cid = "-"
        key = UNKNOWN_SENTINEL
        reason = "write_failed"
        try:
            cid = request.get("cid") or "-"
            key, reason = _process(request, cid)
        except Exception as exc:  # noqa: BLE001
            # A 500 is a differentiated response, i.e. an oracle. The
            # exception TYPE is all that is logged: a traceback renders the
            # exception's repr, and an OSError from the spool carries its
            # operand names — i.e. a state hash, which INV-CB-006 keeps out
            # of the logs along with the state and the query itself.
            logger.warning(
                "callback: handler fault (%s); request data withheld",
                type(exc).__name__)
            key, reason = UNKNOWN_SENTINEL, "write_failed"
        _emit(key, reason, cid)
        return _neutral_redirect()

    def _process(request: web.Request, cid: str) -> tuple[str, str]:
        """Run the flow, returning ``(sampler_key, reason)``. Never returns a
        response: the caller renders the one neutral 303 for every path, so a
        forgotten header here is structurally impossible."""
        received_at = float(clock())
        name = request.match_info.get("name", "")

        # 1. Name lookup. A miss is neutral — deliberately
        #    unlike the webhook route's 404: probes must not be able to tell
        #    an unknown name from an unknown state.
        entry = trigger_registry.get_callback(name)
        plugin = entry.get("plugin") if isinstance(entry, dict) else None
        if not isinstance(plugin, str) or not plugin:
            # Includes a malformed overlay entry: the name may be routed, but
            # without a plugin there is no spool to deposit into, and pairing
            # `unknown_name` with the sentinel unconditionally is what makes
            # the INV-CB-006 rule mechanical.
            return UNKNOWN_SENTINEL, "unknown_name"
        # The reconciler writes ``effective`` into the overlay entry (it is
        # also the key), so this is the live value; the routed-name fallback
        # stays as defense against an entry shape that predates that, since a
        # missing effective must never surface as an empty log field.
        effective = entry.get("effective")
        if not isinstance(effective, str) or not effective:
            effective = name

        # 2. State extraction from the raw query.
        raw_query = request.rel_url.raw_query_string
        state = _extract_state(raw_query)
        if state is None:
            return effective, "bad_state"

        spool = provider()
        if spool is None:
            logger.warning("callback: no spool wired; refusing neutrally")
            return effective, "write_failed"

        # 3. Atomic claim. Replay, expired and
        #    never-minted all lose identically inside claim().
        try:
            claim = spool.claim(plugin, callback_spool.state_hash(state),
                                now=received_at)
        except (OSError, ValueError):
            # ValueError = an unsafe plugin name reached the overlay; OSError
            # = a filesystem fault. Neither may become a 500.
            logger.warning("callback: claim failed for name=%s", effective)
            return effective, "write_failed"
        if claim is None:
            return effective, "no_pending"

        # 4. Result record + attempt-first tri-state publish. The decode
        #    runs only for a won claim, so a probe flood never pays for it.
        record = {
            "v": 1,
            "plugin": plugin,
            "effective": effective,
            "received_at": received_at,
            "raw_query": raw_query,
            "query": _decoded_pairs(raw_query),
        }
        try:
            outcome = spool.publish_result(claim, record)
        except Exception:  # noqa: BLE001
            # An exception may predate any durable record, so the claim is
            # LEFT for the recovery pass — only a durably RECORDED failure
            # authorizes a discard (see below). The response stays the one
            # neutral 303 regardless (INV-CB-005).
            logger.warning("callback: result publish raised for name=%s",
                           effective)
            return effective, "write_failed"
        if outcome is callback_spool.PublishOutcome.FAILED_RECORDED:
            # The failure is durably recorded (a done/publish_failed attempt,
            # strict-fsynced): discarding keeps the state consumed
            # (INV-CB-002, fail-closed single-use for an unauthenticated
            # endpoint) while the attempt file carries the flow's record.
            try:
                spool.discard_claim(claim)
            except (OSError, ValueError):
                logger.warning("callback: claim discard failed for name=%s",
                               effective)
            return effective, "write_failed"
        if outcome is not callback_spool.PublishOutcome.PUBLISHED:
            # FAILED_UNRECORDED: nothing durable exists — leave the claim so
            # recovery restores the flow instead of eating it traceless.
            return effective, "write_failed"

        # 5. Nudge kick — non-durable, and never load-bearing for
        #    the response: the result is already published and durable.
        try:
            _nudge(plugin, claim.state_hash)
        except Exception:  # noqa: BLE001
            logger.warning("callback: delivery nudge kick failed for name=%s",
                           effective)
        return effective, "ok"

    return callback_handler
