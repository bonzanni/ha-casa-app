"""The phone line's public entry point: OpenAI's signed incoming-call webhook.

OpenAI posts a Standard Webhooks request when a call arrives. The handler
checks, in this order: the rate limit, a body cap, the signature (fail
closed: no secret means 503, any doubt about the signature means 401), that
the body is JSON, that the event is not stale, and that it is not a repeat.
Only then does it hand an :class:`Incoming` to the callback. Every verified
event, whatever its type, is answered 200 so OpenAI does not retry it.

Log lines never carry the secret, a signature, or more than the last four
digits of a phone number.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from aiohttp import web

from contacts import numbers as phone_numbers
from rate_limit import RateLimiter, rate_limit_response

logger = logging.getLogger(__name__)

PHONE_WEBHOOK_PATH = "/phone/incoming"

_BODY_MAX = 64 * 1024
# A retried webhook for a call that ended long ago must not be answered.
_STALE_AFTER_S = 120
_DEDUP_SIZE = 500
_INCOMING_TYPES = ("live.transport.incoming", "live.call.incoming")
_URI_SCHEMES = ("sips:", "sip:", "tel:")
# Longer than any real epoch timestamp; also keeps int arithmetic finite.
_MAX_TIMESTAMP_CHARS = 20
_SIGNATURE_HEADERS = ("webhook-id", "webhook-timestamp", "webhook-signature")


@dataclass(frozen=True)
class Incoming:
    session_id: str
    from_number: str
    to_number: str


def _tail(number: str) -> str:
    return f"…{number[-4:]}" if number else "(none)"


def verify_standard(
    *,
    body: bytes,
    headers: Mapping[str, str],
    secret: str,
    now: float,
    tolerance_s: int = 300,
) -> bool:
    """True when *body* carries a valid Standard Webhooks ``v1`` signature."""
    lowered = {str(k).lower(): v for k, v in headers.items()}
    webhook_id = lowered.get("webhook-id")
    timestamp = lowered.get("webhook-timestamp")
    signatures = lowered.get("webhook-signature")
    if not webhook_id or not timestamp or not signatures:
        return False
    if len(timestamp) > _MAX_TIMESTAMP_CHARS:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(now - ts) > tolerance_s:
        return False
    if not secret:
        return False
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except (binascii.Error, ValueError):
        return False
    if not key:
        return False
    signed = f"{webhook_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(
        hmac.new(key, signed, hashlib.sha256).digest()
    )
    found = False
    for candidate in signatures.split():
        version, _, sig = candidate.partition(",")
        if version != "v1":
            continue
        # Compare every candidate rather than stopping at the first match.
        if hmac.compare_digest(expected, sig.encode()):
            found = True
    return found


def _header_value(sip_headers: object, name: str) -> str:
    if not isinstance(sip_headers, list):
        return ""
    for header in sip_headers:
        if (isinstance(header, dict)
                and str(header.get("name", "")).lower() == name.lower()):
            value = header.get("value")
            return value if isinstance(value, str) else ""
    return ""


def _uri_part(value: str) -> str:
    """The URI of a SIP name-addr or addr-spec, or ``""``.

    A value that starts with a known scheme is a bare address and is returned
    as written, so a bracket inside one of its parameters is never read as an
    address. Otherwise a leading quoted display name (with ``\\"`` escapes) is
    skipped, so nothing inside it is read as the address; then the URI is the
    text inside the next ``<...>``, or the remaining value when there is no
    bracket.
    """
    rest = value.strip()
    if rest.lower().startswith(_URI_SCHEMES):
        return rest
    if rest.startswith('"'):
        i = 1
        while i < len(rest):
            if rest[i] == "\\":
                i += 2
                continue
            if rest[i] == '"':
                break
            i += 1
        else:
            return ""
        rest = rest[i + 1:]
    start = rest.find("<")
    if start == -1:
        return rest.strip()
    end = rest.find(">", start)
    if end == -1:
        return ""
    return rest[start + 1:end].strip()


def _number_from_header(value: str, default_region: str) -> str:
    """The E.164 number in a SIP ``From``/``To`` value, or ``""``.

    Only the user part of the URI counts: the URI must start with a known
    scheme, and the user part (after the scheme) ends at the first ``@``,
    ``;``, ``?``, ``>`` or ``:`` (a password follows a colon).
    """
    uri = _uri_part(value)
    lowered = uri.lower()
    for scheme in _URI_SCHEMES:
        if lowered.startswith(scheme):
            user = uri[len(scheme):]
            break
    else:
        return ""
    for stop in ("@", ";", "?", ">", ":"):
        user = user.split(stop, 1)[0]
    return phone_numbers.to_e164(user.strip(), default_region) or ""


def parse_incoming(event: dict, *, default_region: str) -> Incoming | None:
    """The incoming call in *event*, or None when it is not one."""
    if not isinstance(event, dict) or event.get("type") not in _INCOMING_TYPES:
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    headers = data.get("sip_headers")
    return Incoming(
        session_id=session_id,
        from_number=_number_from_header(
            _header_value(headers, "From"), default_region),
        to_number=_number_from_header(
            _header_value(headers, "To"), default_region),
    )


def _remember(seen: OrderedDict[str, None], key: str) -> None:
    seen[key] = None
    while len(seen) > _DEDUP_SIZE:
        seen.popitem(last=False)


def make_handler(
    *,
    secret: Callable[[], Awaitable[str | None]],
    on_incoming: Callable[[Incoming], None],
    limiter: RateLimiter,
    default_region: Callable[[], str],
    now: Callable[[], float] = time.time,
):
    """The aiohttp handler for :data:`PHONE_WEBHOOK_PATH`."""
    seen_webhooks: OrderedDict[str, None] = OrderedDict()
    seen_sessions: OrderedDict[str, None] = OrderedDict()

    async def handler(request: web.Request) -> web.Response:
        limited = rate_limit_response(limiter, "phone")
        if limited is not None:
            return limited

        # Refuse a declared oversize body early, and cap the streamed read so
        # a chunked request cannot buffer past the limit either.
        too_large = web.json_response({"error": "payload too large"}, status=413)
        if request.content_length is not None and request.content_length > _BODY_MAX:
            return too_large
        chunks: list[bytes] = []
        read = 0
        async for chunk in request.content.iter_chunked(8192):
            read += len(chunk)
            if read > _BODY_MAX:
                return too_large
            chunks.append(chunk)
        body = b"".join(chunks)

        try:
            key = await secret()
        except Exception:
            logger.warning("phone webhook: the signing secret could not be read",
                           exc_info=True)
            key = None
        if not key:
            return web.json_response({"error": "unavailable"}, status=503)

        # Each signature header is read once and the same values serve both
        # verification and dedup; a repeated header is refused outright.
        signed_headers: dict[str, str] = {}
        for name in _SIGNATURE_HEADERS:
            values = request.headers.getall(name, [])
            if len(values) > 1:
                logger.warning("phone webhook: refused a request with a "
                               "repeated %s header", name)
                return web.json_response({"error": "invalid signature"},
                                         status=401)
            if values:
                signed_headers[name] = values[0]
        if not verify_standard(body=body, headers=signed_headers,
                               secret=key, now=now()):
            logger.warning("phone webhook: refused a request with an invalid "
                           "signature")
            return web.json_response({"error": "invalid signature"}, status=401)

        try:
            event = json.loads(body)
        except ValueError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(event, dict):
            return web.json_response({"error": "invalid json"}, status=400)

        ok = web.json_response({"ok": True})
        created_at = event.get("created_at")
        if (isinstance(created_at, int) and not isinstance(created_at, bool)
                and (len(str(abs(created_at))) > _MAX_TIMESTAMP_CHARS
                     or now() - created_at > _STALE_AFTER_S)):
            logger.info("phone webhook: ignored a stale %s event",
                        event.get("type"))
            return ok

        webhook_id = signed_headers["webhook-id"]
        if webhook_id in seen_webhooks:
            return ok
        _remember(seen_webhooks, webhook_id)

        incoming = parse_incoming(event, default_region=default_region())
        if incoming is None:
            return ok
        if incoming.session_id in seen_sessions:
            return ok
        _remember(seen_sessions, incoming.session_id)

        logger.info("phone webhook: incoming call from %s to %s",
                    _tail(incoming.from_number), _tail(incoming.to_number))
        try:
            on_incoming(incoming)
        except Exception:
            # The event is acknowledged anyway: a retry would be a duplicate.
            logger.exception("phone webhook: the incoming-call handler failed")
        return ok

    return handler
