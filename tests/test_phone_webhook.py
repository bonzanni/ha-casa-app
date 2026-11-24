"""The phone line's signed webhook: verification, parsing, dedup, staleness."""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict

from phone import webhook
from phone.webhook import Incoming
from rate_limit import RateLimiter

KEY = b"0123456789abcdef0123456789abcdef"
SECRET = "whsec_" + base64.b64encode(KEY).decode()
NOW = 1_800_000_000.0


def at(user: str, host: str) -> str:
    """``user@host`` for a SIP URI (kept apart so the source holds no address)."""
    return f"{user}@{host}"


ANONYMOUS = f'"Anonymous" <sip:{at("anonymous", "anonymous.invalid")}>'


def sign(secret: str, webhook_id: str, ts: int | str, body: bytes) -> str:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    mac = hmac.new(key, f"{webhook_id}.{ts}.".encode() + body, hashlib.sha256)
    return "v1," + base64.b64encode(mac.digest()).decode()


def event(
    *,
    session_id: str = "live_abc",
    type_: str = "live.transport.incoming",
    from_value: str = (
        f'"Someone" <sip:{at("+31612345678", "sip.example.com")};user=phone>;tag=x'),
    to_value: str = f"<sip:{at('+31201234567', 'sip.example.com')}>",
    created_at: int | None = None,
) -> dict:
    ev: dict = {
        "type": type_,
        "data": {
            "type": "sip",
            "session_id": session_id,
            "sip_headers": [
                {"name": "From", "value": from_value},
                {"name": "To", "value": to_value},
            ],
        },
    }
    if created_at is not None:
        ev["created_at"] = created_at
    return ev


def signed_headers(body: bytes, *, webhook_id: str = "wh_1",
                   ts: int | None = None, secret: str = SECRET) -> dict:
    ts = int(NOW) if ts is None else ts
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sign(secret, webhook_id, ts, body),
        "content-type": "application/json",
    }


class Harness:
    def __init__(self, client: TestClient, calls: list[Incoming]):
        self.client = client
        self.calls = calls

    async def post(self, body: bytes, headers: dict):
        return await self.client.post(
            webhook.PHONE_WEBHOOK_PATH, data=body, headers=headers,
        )

    async def post_event(self, ev: dict, **kw):
        body = json.dumps(ev).encode()
        return await self.post(body, signed_headers(body, **kw))


@pytest.fixture
async def make_harness():
    clients: list[TestClient] = []

    async def _make(*, secret_value: str | None = SECRET,
                    capacity: int = 0, secret_raises: bool = False,
                    on_incoming_raises: bool = False) -> Harness:
        calls: list[Incoming] = []

        async def secret() -> str | None:
            if secret_raises:
                raise RuntimeError("vault down")
            return secret_value

        def on_incoming(inc: Incoming) -> None:
            calls.append(inc)
            if on_incoming_raises:
                raise RuntimeError("callback failed")

        handler = webhook.make_handler(
            secret=secret,
            on_incoming=on_incoming,
            limiter=RateLimiter(capacity=capacity),
            default_region=lambda: "NL",
            now=lambda: NOW,
        )
        app = web.Application()
        app.router.add_post(webhook.PHONE_WEBHOOK_PATH, handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return Harness(client, calls)

    yield _make
    for c in clients:
        await c.close()


# --- the handler ----------------------------------------------------------

async def test_valid_event_is_handed_over_once_with_parsed_numbers(make_harness):
    h = await make_harness()
    resp = await h.post_event(event())
    assert resp.status == 200
    assert h.calls == [Incoming(session_id="live_abc",
                                from_number="+31612345678",
                                to_number="+31201234567")]


async def test_deprecated_event_type_is_handled_alike(make_harness):
    h = await make_harness()
    resp = await h.post_event(event(type_="live.call.incoming"))
    assert resp.status == 200
    assert len(h.calls) == 1


async def test_wrong_signature_is_refused(make_harness):
    h = await make_harness()
    body = json.dumps(event()).encode()
    headers = signed_headers(body, secret="whsec_" + base64.b64encode(b"x" * 32).decode())
    resp = await h.post(body, headers)
    assert resp.status == 401
    assert h.calls == []


async def test_tampered_body_is_refused(make_harness):
    h = await make_harness()
    body = json.dumps(event()).encode()
    headers = signed_headers(body)
    resp = await h.post(json.dumps(event(session_id="live_other")).encode(), headers)
    assert resp.status == 401
    assert h.calls == []


async def test_timestamp_301_seconds_old_is_refused(make_harness):
    h = await make_harness()
    resp = await h.post_event(event(), ts=int(NOW) - 301)
    assert resp.status == 401
    assert h.calls == []


async def test_second_of_two_signatures_valid_is_accepted(make_harness):
    h = await make_harness()
    body = json.dumps(event()).encode()
    headers = signed_headers(body)
    bogus = "v1," + base64.b64encode(b"\0" * 32).decode()
    headers["webhook-signature"] = f"{bogus} {headers['webhook-signature']}"
    resp = await h.post(body, headers)
    assert resp.status == 200
    assert len(h.calls) == 1


async def test_same_webhook_id_twice_calls_once(make_harness):
    h = await make_harness()
    assert (await h.post_event(event(), webhook_id="wh_1")).status == 200
    assert (await h.post_event(event(), webhook_id="wh_1")).status == 200
    assert len(h.calls) == 1


async def test_repeated_webhook_id_is_dropped_on_its_own(make_harness):
    # Isolates the webhook-id table from the session table.
    h = await make_harness()
    assert (await h.post_event(event(session_id="live_a"), webhook_id="wh_1")).status == 200
    assert (await h.post_event(event(session_id="live_b"), webhook_id="wh_1")).status == 200
    assert [c.session_id for c in h.calls] == ["live_a"]


async def test_same_session_under_new_webhook_id_calls_once(make_harness):
    h = await make_harness()
    assert (await h.post_event(event(), webhook_id="wh_1")).status == 200
    assert (await h.post_event(event(), webhook_id="wh_2")).status == 200
    assert len(h.calls) == 1


async def test_refused_event_does_not_poison_dedup(make_harness):
    h = await make_harness()
    body = json.dumps(event()).encode()
    bad = signed_headers(body, secret="whsec_" + base64.b64encode(b"x" * 32).decode())
    assert (await h.post(body, bad)).status == 401
    stale = event(created_at=int(NOW) - 121)
    assert (await h.post_event(stale, webhook_id="wh_1")).status == 200
    assert h.calls == []
    # The same webhook id and session id, now valid and fresh, still go through.
    assert (await h.post_event(event(), webhook_id="wh_1")).status == 200
    assert len(h.calls) == 1


async def test_body_over_64_kib_is_413(make_harness):
    h = await make_harness()
    body = b"{" + b" " * (64 * 1024) + b"}"
    resp = await h.post(body, signed_headers(body))
    assert resp.status == 413
    assert h.calls == []


async def test_chunked_body_over_64_kib_is_413(make_harness):
    h = await make_harness()
    body = b"{" + b" " * (64 * 1024) + b"}"

    async def gen():
        for i in range(0, len(body), 4096):
            yield body[i:i + 4096]

    resp = await h.client.post(webhook.PHONE_WEBHOOK_PATH, data=gen(),
                               headers=signed_headers(body))
    assert resp.status == 413
    assert h.calls == []


async def test_no_secret_is_503(make_harness):
    h = await make_harness(secret_value=None)
    resp = await h.post_event(event())
    assert resp.status == 503
    assert h.calls == []


@pytest.mark.parametrize("kw", [{"secret_value": ""}, {"secret_raises": True}])
async def test_empty_or_failing_secret_is_503(make_harness, kw):
    h = await make_harness(**kw)
    resp = await h.post_event(event())
    assert resp.status == 503
    assert h.calls == []


async def test_failing_callback_still_acknowledges(make_harness):
    h = await make_harness(on_incoming_raises=True)
    resp = await h.post_event(event())
    assert resp.status == 200
    assert len(h.calls) == 1


@pytest.mark.parametrize("prepend", [True, False])
async def test_duplicate_webhook_id_header_is_refused(make_harness, prepend):
    h = await make_harness()
    body = json.dumps(event()).encode()
    valid = signed_headers(body)
    headers = CIMultiDict()
    if prepend:
        headers.add("webhook-id", "wh_other")
    headers.extend(valid)
    if not prepend:
        headers.add("webhook-id", "wh_other")
    resp = await h.post(body, headers)
    assert resp.status == 401
    assert h.calls == []


async def test_webhook_id_table_evicts_the_oldest(make_harness):
    h = await make_harness()
    assert (await h.post_event(event(session_id="live_0"), webhook_id="wh_0")).status == 200
    for i in range(1, 501):
        resp = await h.post_event(event(type_="live.session.closed"),
                                  webhook_id=f"wh_{i}")
        assert resp.status == 200
    # 501 ids seen: the first has been evicted and counts as new again.
    assert (await h.post_event(event(session_id="live_new"), webhook_id="wh_0")).status == 200
    assert [c.session_id for c in h.calls] == ["live_0", "live_new"]


async def test_session_table_evicts_the_oldest(make_harness):
    h = await make_harness()
    for i in range(501):
        resp = await h.post_event(event(session_id=f"live_{i}"),
                                  webhook_id=f"wh_{i}")
        assert resp.status == 200
    assert (await h.post_event(event(session_id="live_0"), webhook_id="wh_new")).status == 200
    assert len(h.calls) == 502
    assert h.calls[-1].session_id == "live_0"
    # A session still inside the table is dropped.
    assert (await h.post_event(event(session_id="live_500"), webhook_id="wh_new2")).status == 200
    assert len(h.calls) == 502


async def test_absurd_created_at_is_ignored(make_harness):
    h = await make_harness()
    for i, value in enumerate((10 ** 300, -(10 ** 300))):
        resp = await h.post_event(event(created_at=value), webhook_id=f"wh_{i}")
        assert resp.status == 200
    assert h.calls == []


async def test_validly_signed_event_121_seconds_old_is_ignored(make_harness):
    h = await make_harness()
    resp = await h.post_event(event(created_at=int(NOW) - 121))
    assert resp.status == 200
    assert h.calls == []


async def test_event_120_seconds_old_is_still_fresh(make_harness):
    h = await make_harness()
    resp = await h.post_event(event(created_at=int(NOW) - 120))
    assert resp.status == 200
    assert len(h.calls) == 1


async def test_other_event_type_is_acknowledged_and_ignored(make_harness):
    h = await make_harness()
    resp = await h.post_event(event(type_="live.session.closed"))
    assert resp.status == 200
    assert h.calls == []


async def test_signed_non_json_is_400(make_harness):
    h = await make_harness()
    body = b"not json"
    resp = await h.post(body, signed_headers(body))
    assert resp.status == 400
    assert h.calls == []


async def test_anonymous_caller_has_empty_number(make_harness):
    h = await make_harness()
    ev = event(from_value=ANONYMOUS)
    assert (await h.post_event(ev)).status == 200
    assert h.calls[0].from_number == ""


async def test_31st_request_in_a_minute_is_429(make_harness):
    h = await make_harness(capacity=30)
    for i in range(30):
        resp = await h.post_event(event(session_id=f"live_{i}"),
                                  webhook_id=f"wh_{i}")
        assert resp.status == 200
    resp = await h.post_event(event(session_id="live_x"), webhook_id="wh_x")
    assert resp.status == 429
    assert len(h.calls) == 30


# --- verify_standard ------------------------------------------------------

def _verify(headers: dict, body: bytes = b"{}", secret: str = SECRET) -> bool:
    return webhook.verify_standard(body=body, headers=headers,
                                   secret=secret, now=NOW)


def test_verify_accepts_valid_and_mixed_case_headers():
    body = b"{}"
    h = signed_headers(body)
    assert _verify(h, body)
    assert _verify({k.title(): v for k, v in h.items()}, body)


def test_verify_accepts_secret_without_prefix():
    body = b"{}"
    bare = base64.b64encode(KEY).decode()
    assert _verify(signed_headers(body), body, secret=bare)


@pytest.mark.parametrize("missing", ["webhook-id", "webhook-timestamp",
                                     "webhook-signature"])
def test_verify_refuses_missing_header(missing):
    h = signed_headers(b"{}")
    del h[missing]
    assert not _verify(h)


def test_verify_refuses_bad_timestamps():
    body = b"{}"
    for ts in ("abc", "1.5", ""):
        h = signed_headers(body)
        h["webhook-timestamp"] = ts
        assert not _verify(h, body)
    assert not _verify(signed_headers(body, ts=int(NOW) + 301), body)
    assert _verify(signed_headers(body, ts=int(NOW) - 300), body)


def test_verify_refuses_overlong_timestamp():
    body = b"{}"
    h = signed_headers(body)
    h["webhook-timestamp"] = "9" * 400
    assert not _verify(h, body)
    h["webhook-timestamp"] = "1" + "0" * 20
    assert not _verify(h, body)


def test_verify_refuses_empty_or_non_base64_secret():
    h = signed_headers(b"{}")
    assert not _verify(h, secret="")
    assert not _verify(h, secret="whsec_")
    assert not _verify(h, secret="whsec_not*base64!")


def test_verify_refuses_non_v1_signature():
    body = b"{}"
    h = signed_headers(body)
    h["webhook-signature"] = h["webhook-signature"].replace("v1,", "v2,")
    assert not _verify(h, body)


# --- parse_incoming -------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ('"Name" <sip:+31612345678@host;user=phone>;tag=x', "+31612345678"),
    # The display name or a URI parameter never supplies the number.
    (f'"<sip:{at("+31600000001", "x.example")}>" <sip:{at("+447700900123", "y.example")}>',
     "+447700900123"),
    (f'"x<tel:+31600000001>" <sip:{at("anonymous", "anonymous.invalid")}>', ""),
    (f'"say \\"<tel:+31600000001>\\"" <sip:{at("anonymous", "anonymous.invalid")}>', ""),
    (f"<sip:{at('anonymous', 'anonymous.invalid')};x=sips:+31600000001>", ""),
    ("<sip:+31612345678?h=1>", "+31612345678"),
    # A bare address is read as written: a bracket in a parameter is not an address.
    (f'{at("sip:anonymous", "anonymous.invalid")};tag=abc;x="<tel:+31600000001>"', ""),
    (f'{at("sip:anonymous", "anonymous.invalid")};tag=abc;x="a<{at("sip:+31600000001", "h")}>"', ""),
    # The user part ends at a colon (a password follows it).
    (f"<{at('sip:anonymous:+31600000001', 'h')}>", ""),
    (f"<{at('sip:+31612345678:pw', 'h')}>", "+31612345678"),
    (f"<sip:<{at('sip:+31612345678', 'h')}>{'@'}anonymous.invalid>", ""),
    (f"<sip:{at('+31612345678', 'sip.twilio.com')};user=phone>", "+31612345678"),
    ("<sip:+31612345678@host>", "+31612345678"),
    ("sip:0612345678@host", "+31612345678"),
    ("<tel:+31612345678>", "+31612345678"),
    ("tel:+31612345678;phone-context=x", "+31612345678"),
    (ANONYMOUS, ""),
    ("", ""),
    ("garbage", ""),
])
def test_parse_numbers(value, expected):
    inc = webhook.parse_incoming(event(from_value=value), default_region="NL")
    assert inc is not None
    assert inc.from_number == expected


def test_parse_missing_headers_gives_empty_numbers():
    ev = event()
    ev["data"]["sip_headers"] = []
    inc = webhook.parse_incoming(ev, default_region="NL")
    assert inc == Incoming(session_id="live_abc", from_number="", to_number="")


@pytest.mark.parametrize("ev", [
    {"type": "live.transport.incoming", "data": {"session_id": ""}},
    {"type": "live.transport.incoming", "data": {"session_id": 5}},
    {"type": "live.transport.incoming", "data": "x"},
    {"type": "live.transport.incoming"},
    {"type": "something.else", "data": {"session_id": "live_a"}},
    {},
])
def test_parse_returns_none_for_non_incoming(ev):
    assert webhook.parse_incoming(ev, default_region="NL") is None


def test_incoming_is_frozen():
    inc = Incoming(session_id="s", from_number="", to_number="")
    with pytest.raises(dataclasses.FrozenInstanceError):
        inc.session_id = "t"  # type: ignore[misc]


def test_path_constant():
    assert webhook.PHONE_WEBHOOK_PATH == "/phone/incoming"

