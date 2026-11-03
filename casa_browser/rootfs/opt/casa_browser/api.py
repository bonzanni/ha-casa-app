"""The versioned ``/v1`` HTTP API (protocol 1) on the internal port, for trusted Casa code.

``make_app(controller)`` is the one application constructor. Every route requires the
pairing secret as ``Authorization: Bearer <secret>`` — except ``POST /v1/pair/claim``
while the operator's two-minute pairing window is open. Every ``POST`` also requires
``X-Casa-Browser-Protocol: 1`` (``protocol_mismatch`` otherwise) before anything happens;
``GET /v1/hello`` reports compatibility without it.

Every refusal is ``{"error": "<code>"}`` with the status ``protocol.ERROR_STATUS`` names
for the code. Bodies are parsed by ``protocol.parse_*`` (unknown or missing members and
out-of-bound values → ``unsupported`` before any side effect). No route echoes a request
value back in an error, and the server is run with access logging off, so neither the
secret nor a query string reaches a log.

Control routes (``/v1/control/…``) are for Casa's trusted operator callbacks (taps,
registration, forget, deliveries, screenshot and source delivery). They must never become
model tools; the bearer secret is the only thing that separates them, and it is held by
Fido's plugin code, never by a model (Plan 2).

The routes await a controller (``Controller`` below: every method is a coroutine).
``casa_browser.controller.RunController`` is the app's; ``casa_browser/tests/fake_controller.py``
serves the same routes with canned browser work for contract tests. Neither changes these wire
shapes.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Protocol

from aiohttp import HttpVersion11, web

from . import __version__
from .protocol import (
    ERROR_STATUS,
    ORIGIN_KINDS,
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    ProtocolError,
    is_app_id,
    is_caller_id,
    parse_action,
    parse_answer,
    parse_archive,
    parse_cancel,
    parse_request_cancel,
    parse_claim,
    parse_delivery,
    parse_download,
    parse_finish,
    parse_judgement_verdict,
    parse_live,
    parse_login_code,
    parse_sign_in,
    parse_operator_ref,
    parse_question_live,
    parse_run_spec,
    parse_search,
    parse_site_registration,
    spec_digest,
)

MAX_BODY_BYTES = 256 * 1024
CLAIM_PATH = "/v1/pair/claim"
_TOKEN = re.compile(r"[\x21-\x7e]{1,512}")
CONTROLLER_KEY = web.AppKey("controller", object)


class Controller(Protocol):
    """What the API needs from the controller. Every method raises ``ProtocolError`` for
    a refusal; ``not_found`` when an id is unknown or belongs to another run."""

    pairing: Any

    async def hello(self) -> dict: ...
    async def list_sites(self) -> list: ...
    async def register_site(self, payload: dict) -> str: ...
    async def start_run(self, spec) -> tuple: ...
    async def list_active(self) -> list: ...
    async def get_run(self, run_id: str): ...
    async def events(self, run_id: str, after: int) -> list: ...
    async def screenshot(self, run_id: str, screenshot_id: str) -> bytes: ...
    async def take_screenshot(self, run_id: str) -> str: ...
    async def sources(self, run_id: str) -> list: ...
    async def observe(self, run_id: str, sign_in: bool = False): ...
    async def act(self, run_id: str, action): ...
    async def download(self, run_id: str, handle: str): ...
    async def search(self, run_id: str, query: str): ...
    async def archive(self, run_id: str, file_ids: tuple): ...
    async def request_live(self, run_id: str, purpose: str): ...
    async def live_complete(self, run_id: str, operator_ref: str): ...
    async def finish(self, run_id: str, manifest): ...
    async def cancel(self, run_id: str): ...
    async def cancel_requests(self, request_ids: tuple) -> dict: ...
    async def request_stopped(self, request_id: str) -> bool: ...
    async def forget(self, site_id: str, operator_ref: str) -> dict: ...
    async def records(self, requester_id: str, since: datetime) -> list: ...
    async def file_bytes(self, file_id: str) -> tuple: ...
    async def ack_delivery(self, file_id: str, body: dict) -> dict: ...
    async def notices(self, after: int) -> list: ...
    async def apply_judgement(self, run_id: str, judgement_id: str, body: dict) -> dict: ...
    async def answer_question(self, run_id: str, question_id: str, body: dict) -> dict: ...
    async def pending_judgements(self, run_id: str) -> dict: ...
    async def get_question(self, run_id: str, question_id: str) -> dict: ...
    async def live_from_question(self, run_id: str, question_id: str, body: dict) -> dict: ...
    async def login_code(self, run_id: str, login): ...
    async def ask_sign_in(self, run_id: str, site_name: str): ...


# Every aiohttp logger that can carry request bytes (a request line, a header value, a body
# excerpt in an exception message). aiohttp's own HTTP parser answers a malformed request
# itself — before the app or this module's guard sees it, outside the /v1 contract — and logs
# the failure; access logging is off in ``serve`` but filtered too.
LIBRARY_LOGGERS = ("aiohttp.access", "aiohttp.client", "aiohttp.internal", "aiohttp.server",
                   "aiohttp.web", "aiohttp.websocket")


class _WithholdRequestBytes(logging.Filter):
    """Keeps the record (level, logger, time) but replaces its text and drops its exception,
    so no request bytes reach a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = f"{record.name}: {record.levelname.lower()} event (details withheld)"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


_WITHHOLD = _WithholdRequestBytes()


def quiet_library_logs() -> None:
    """Install the filter on every library logger (idempotent)."""
    for name in LIBRARY_LOGGERS:
        logger = logging.getLogger(name)
        if _WITHHOLD not in logger.filters:
            logger.addFilter(_WITHHOLD)


def error_response(code: str) -> web.Response:
    return web.json_response({"error": code}, status=ERROR_STATUS[code])


def _ctl(request: web.Request) -> Controller:
    return request.app[CONTROLLER_KEY]


def _bearer(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme != "Bearer" or not _TOKEN.fullmatch(token):
        return None
    return token


@web.middleware
async def guard(request: web.Request, handler):
    """See ``_guard``. A request whose body could not be read (too large, undecodable
    content encoding) is answered and its connection closed: the rest of that stream cannot
    be trusted to frame a next request."""
    response, bad_body = await _guard(request, handler)
    if bad_body:
        response.force_close()
    return response


async def _guard(request: web.Request, handler):
    """Auth, then protocol header, then the handler; every failure as a typed code.

    The whole body is read BEFORE the secret is checked, and nothing between the check and
    the controller call awaits I/O (the handler's own read returns the cached bytes). So an
    operator re-pairing while a request is still arriving revokes it: the check sees the
    secret that is current when the request is complete. A controller command that then waits
    for the controller's command lock re-checks its run's state once it holds it, and a
    re-pair interrupts every run first."""
    ctl = _ctl(request)
    bad_body = False
    try:
        if request.method == "POST":
            try:
                await request.read()
            except Exception:  # noqa: BLE001 — too large, bad encoding: refused after auth
                bad_body = True
        # The window exempts only the claim route reached by its literal path: the resolved
        # handler AND the raw (undecoded) path, so no encoded spelling takes the exemption.
        is_claim = (getattr(request.match_info, "handler", None) is pair_claim
                    and request.raw_path == CLAIM_PATH)
        if not (is_claim and ctl.pairing.window_open()):
            if not ctl.pairing.verify(_bearer(request)):
                raise ProtocolError("unauthorized")
        if request.method == "POST" and request.headers.get(PROTOCOL_HEADER) != str(PROTOCOL_VERSION):
            raise ProtocolError("protocol_mismatch")
        if bad_body:
            raise ProtocolError("unsupported")
        return await handler(request), bad_body
    except ProtocolError as err:
        return error_response(err.code), bad_body
    except web.HTTPNotFound:
        return error_response("not_found"), bad_body
    except web.HTTPMethodNotAllowed:
        return error_response("unsupported"), bad_body
    except web.HTTPRequestEntityTooLarge:
        return error_response("unsupported"), bad_body
    except web.HTTPException as exc:
        if exc.status < 400:
            raise
        return error_response("unsupported"), bad_body
    except Exception:  # noqa: BLE001 — the text could carry a value; never echo it
        return error_response("internal"), bad_body


def _unique_members(pairs: list) -> dict:
    """A JSON object with a repeated member is ambiguous (which value was admitted?)."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate member")
        out[key] = value
    return out


async def _expect(request: web.Request) -> None:
    """``Expect: 100-continue`` is answered as HTTP/1.1 requires; any other ``Expect`` value
    is ignored, so the guard still answers the request with a typed code."""
    if request.version == HttpVersion11 and request.headers.get("Expect", "").lower() == "100-continue":
        await request.writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        request.writer.output_size = 0


async def not_found(request: web.Request) -> web.Response:
    raise ProtocolError("not_found")


async def _body(request: web.Request) -> Any:
    raw = await request.read()
    if len(raw) > MAX_BODY_BYTES:
        raise ProtocolError("unsupported")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_members,
                          parse_constant=lambda _c: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ProtocolError("unsupported") from None


def _query(request: web.Request, allowed: set[str]) -> dict[str, str]:
    keys = list(request.query.keys())
    if len(keys) != len(set(keys)) or not set(keys) <= allowed:
        raise ProtocolError("unsupported")
    return dict(request.query)


def _no_query(request: web.Request) -> None:
    _query(request, set())


def _int_param(value: str | None, default: int = 0) -> int:
    if value is None:
        return default
    if not (value.isascii() and value.isdigit()) or len(value) > 12:
        raise ProtocolError("unsupported")
    return int(value)


def _id(request: web.Request, name: str) -> str:
    value = request.match_info[name]
    if not is_app_id(value):
        raise ProtocolError("not_found")
    return value


async def _run_id(request: web.Request) -> str:
    run_id = _id(request, "run")
    if await _ctl(request).get_run(run_id) is None:
        raise ProtocolError("not_found")
    return run_id


def _ok(payload: Any, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


# -- handlers ---------------------------------------------------------------------------------


async def hello(request: web.Request) -> web.Response:
    _no_query(request)
    info = await _ctl(request).hello()
    return _ok({"protocol_version": PROTOCOL_VERSION, "app_version": __version__,
                "configured": bool(info["configured"]), "needs_locale": bool(info["needs_locale"]),
                "language_source": info.get("language_source")})


async def pair_claim(request: web.Request) -> web.Response:
    _no_query(request)
    label = parse_claim(await _body(request))
    peer = request.remote
    secret = _ctl(request).pairing.claim(label, peer)
    if secret is None:
        raise ProtocolError("unauthorized")
    return _ok({"secret": secret, "protocol_version": PROTOCOL_VERSION})


async def list_sites(request: web.Request) -> web.Response:
    _no_query(request)
    return _ok({"sites": [s.to_wire() for s in await _ctl(request).list_sites()]})


async def register_site(request: web.Request) -> web.Response:
    _no_query(request)
    payload = parse_site_registration(await _body(request))
    return _ok({"site_id": await _ctl(request).register_site(payload)}, status=201)


async def create_run(request: web.Request) -> web.Response:
    _no_query(request)
    spec, members = parse_run_spec(await _body(request))
    if spec.admission.origin_kind not in ORIGIN_KINDS:
        raise ProtocolError("unsupported")
    try:
        digest = spec_digest(members)
    except ValueError:
        raise ProtocolError("unsupported") from None
    if digest != spec.admission.admitted_spec_digest:
        raise ProtocolError("blocked")
    run, created = await _ctl(request).start_run(spec)
    return _ok(run.to_wire(), status=201 if created else 200)


async def list_runs(request: web.Request) -> web.Response:
    q = _query(request, {"state"})
    if q.get("state") != "active":
        raise ProtocolError("unsupported")
    return _ok({"runs": [r.to_wire() for r in await _ctl(request).list_active()]})


async def get_run(request: web.Request) -> web.Response:
    """The run, and whether the operator's Stop stopped its request (``stopped``: its request id
    is in ``cancelled_requests``) — the one fact the plugin reads to know whose ending it was."""
    _no_query(request)
    ctl = _ctl(request)
    run = await ctl.get_run(await _run_id(request))
    return _ok({**run.to_wire(), "stopped": await ctl.request_stopped(run.spec.request_id)})


async def run_events(request: web.Request) -> web.Response:
    q = _query(request, {"after"})
    run_id = await _run_id(request)
    return _ok({"events": [e.to_wire() for e in await _ctl(request).events(run_id, _int_param(q.get("after")))]})


async def screenshot(request: web.Request) -> web.Response:
    _no_query(request)
    data = await _ctl(request).screenshot(await _run_id(request), _id(request, "screenshot"))
    return web.Response(body=data, content_type="image/png",
                        headers={"Cache-Control": "no-store"})


async def take_screenshot(request: web.Request) -> web.Response:
    """Slice 5b (F-j): the run's page as it stands now, as an operator screenshot; its id."""
    _no_query(request)
    run_id = await _run_id(request)
    _empty(await _body(request))
    return _ok({"screenshot_id": await _ctl(request).take_screenshot(run_id)})


async def sources(request: web.Request) -> web.Response:
    _no_query(request)
    return _ok({"sources": [s.to_wire() for s in await _ctl(request).sources(await _run_id(request))]})


def _empty(body: Any) -> None:
    if body != {}:
        raise ProtocolError("unsupported")


def _observe_body(body: Any) -> bool:
    """``{}`` (Fido 0.1.20: the page) or ``{"sign_in": <bool>}`` (Fido 0.1.21: the page, looked
    at first for the site's sign-in); anything else is refused."""
    if body == {}:
        return False
    if not isinstance(body, dict) or set(body) != {"sign_in"} or type(body["sign_in"]) is not bool:
        raise ProtocolError("unsupported")
    return body["sign_in"]


async def observe(request: web.Request) -> web.Response:
    _no_query(request)
    run_id = await _run_id(request)
    if _observe_body(await _body(request)):
        return _ok((await _ctl(request).observe(run_id, sign_in=True)).to_wire())
    return _ok((await _ctl(request).observe(run_id)).to_wire())


async def act(request: web.Request) -> web.Response:
    _no_query(request)
    run_id = await _run_id(request)
    action = parse_action(await _body(request))
    result = await _ctl(request).act(run_id, action)
    return _ok(result.to_wire())


async def sign_in(request: web.Request) -> web.Response:
    """Slice 5n: the sign-in card for the public page the run is on (a site not set up). A
    normal run route: the model asks; the host, the screenshot and the item are the app's."""
    _no_query(request)
    run_id = await _run_id(request)
    site_name = parse_sign_in(await _body(request))
    return _ok((await _ctl(request).ask_sign_in(run_id, site_name)).to_wire())


async def login_code(request: web.Request) -> web.Response:
    """Slice 6 (S6-3): the sign-in code the site sent, for the site that asked for it in this
    run. A normal run route: the model supplies the code. Never echoed (a refusal is its code
    word only), never logged."""
    _no_query(request)
    run_id = await _run_id(request)
    login = parse_login_code(await _body(request))
    return _ok((await _ctl(request).login_code(run_id, login)).to_wire())


async def download(request: web.Request) -> web.Response:
    _no_query(request)
    run_id = await _run_id(request)
    handle = parse_download(await _body(request))
    return _ok((await _ctl(request).download(run_id, handle)).to_wire())


async def search(request: web.Request) -> web.Response:
    _no_query(request)
    run_id = await _run_id(request)
    query = parse_search(await _body(request))
    return _ok((await _ctl(request).search(run_id, query)).to_wire())


async def archive(request: web.Request) -> web.Response:
    _no_query(request)
    run_id = await _run_id(request)
    ids = parse_archive(await _body(request))
    return _ok((await _ctl(request).archive(run_id, ids)).to_wire())


async def live(request: web.Request) -> web.Response:
    _no_query(request)
    run_id = await _run_id(request)
    purpose = parse_live(await _body(request))
    return _ok((await _ctl(request).request_live(run_id, purpose)).to_wire(), status=201)


async def live_complete(request: web.Request) -> web.Response:
    _no_query(request)
    run_id = await _run_id(request)
    ref = parse_operator_ref(await _body(request))
    return _ok((await _ctl(request).live_complete(run_id, ref)).to_wire())


async def finish(request: web.Request) -> web.Response:
    _no_query(request)
    run_id = await _run_id(request)
    manifest = parse_finish(await _body(request))
    return _ok((await _ctl(request).finish(run_id, manifest)).to_wire())


async def cancel(request: web.Request) -> web.Response:
    _no_query(request)
    run_id = await _run_id(request)
    parse_cancel(await _body(request))
    return _ok((await _ctl(request).cancel(run_id)).to_wire())


async def cancel_requests(request: web.Request) -> web.Response:
    """The operator's Stop of one request: every request id the requester chose for it, in one
    call (runs of some may not exist yet). The paired requester's, like
    ``/v1/runs/{run}/cancel``."""
    _no_query(request)
    request_ids = parse_request_cancel(await _body(request))
    return _ok(await _ctl(request).cancel_requests(request_ids))


async def forget(request: web.Request) -> web.Response:
    _no_query(request)
    site_id = _id(request, "site")
    ref = parse_operator_ref(await _body(request))
    return _ok(await _ctl(request).forget(site_id, ref))


def _since(value: str | None) -> datetime:
    if value is None or len(value) > 40:
        raise ProtocolError("unsupported")
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ProtocolError("unsupported") from None
    if at.tzinfo is None:
        raise ProtocolError("unsupported")
    return at


async def records(request: web.Request) -> web.Response:
    q = _query(request, {"requester_id", "since"})
    requester = q.get("requester_id")
    if not is_caller_id(requester):
        raise ProtocolError("unsupported")
    rows = await _ctl(request).records(requester, _since(q.get("since")))
    return _ok({"records": [r.to_wire() for r in rows]})


async def file_bytes(request: web.Request) -> web.Response:
    _no_query(request)
    record, data = await _ctl(request).file_bytes(_id(request, "file"))
    return web.Response(body=data, content_type="application/octet-stream",
                        headers={"Cache-Control": "no-store",
                                 "X-Casa-Browser-File-Sha256": record.sha256})


async def delivery(request: web.Request) -> web.Response:
    _no_query(request)
    file_id = _id(request, "file")
    body = parse_delivery(await _body(request))
    return _ok(await _ctl(request).ack_delivery(file_id, body))


async def judgement(request: web.Request) -> web.Response:
    """A verdict from the plugin's own relay (action model S2-7): a control route, so no model
    tool maps to it."""
    _no_query(request)
    run_id = await _run_id(request)
    judgement_id = _id(request, "judgement")
    body = parse_judgement_verdict(await _body(request))
    return _ok(await _ctl(request).apply_judgement(run_id, judgement_id, body))


async def answer(request: web.Request) -> web.Response:
    """The operator's answer to a question (S2-10): a control route (Casa's trusted tap)."""
    _no_query(request)
    run_id = await _run_id(request)
    question_id = _id(request, "question")
    body = parse_answer(await _body(request))
    return _ok(await _ctl(request).answer_question(run_id, question_id, body))


async def question_live(request: web.Request) -> web.Response:
    """The operator's [Open live view] on a question (slice 3, S3-3): a control route."""
    _no_query(request)
    run_id = await _run_id(request)
    question_id = _id(request, "question")
    body = parse_question_live(await _body(request))
    return _ok(await _ctl(request).live_from_question(run_id, question_id, body))


async def pending_judgements(request: web.Request) -> web.Response:
    """The judgements the app started itself (slice 5: ``login_item``, ``signin``), for the
    plugin's code to relay to Casa's check: a control route."""
    _no_query(request)
    return _ok(await _ctl(request).pending_judgements(await _run_id(request)))


async def question(request: web.Request) -> web.Response:
    _no_query(request)
    return _ok(await _ctl(request).get_question(await _run_id(request), _id(request, "question")))


async def notices(request: web.Request) -> web.Response:
    q = _query(request, {"after"})
    rows = await _ctl(request).notices(_int_param(q.get("after")))
    return _ok({"notices": [n.to_wire() for n in rows]})


_GET = {"allow_head": False, "expect_handler": _expect}


def make_app(controller: Controller) -> web.Application:
    """The one ``/v1`` application constructor (Plan 2's contract test imports it)."""
    quiet_library_logs()
    app = web.Application(middlewares=[guard], client_max_size=MAX_BODY_BYTES)
    app[CONTROLLER_KEY] = controller
    # Spelled out inline: the coverage ledger enumerates this list's call sites. No implicit
    # HEAD; every route (and the catch-all, which makes any other method or path a typed
    # not_found after the guard) uses the Expect handler above.
    app.add_routes([
        web.get("/v1/hello", hello, **_GET),
        web.post("/v1/pair/claim", pair_claim, expect_handler=_expect),
        web.get("/v1/sites", list_sites, **_GET),
        web.post("/v1/control/sites", register_site, expect_handler=_expect),
        web.post("/v1/runs", create_run, expect_handler=_expect),
        web.get("/v1/runs", list_runs, **_GET),
        web.get("/v1/runs/{run}", get_run, **_GET),
        web.get("/v1/runs/{run}/events", run_events, **_GET),
        web.get("/v1/control/runs/{run}/screenshots/{screenshot}", screenshot, **_GET),
        web.post("/v1/control/runs/{run}/screenshots", take_screenshot, expect_handler=_expect),
        web.get("/v1/control/runs/{run}/sources", sources, **_GET),
        web.post("/v1/runs/{run}/observe", observe, expect_handler=_expect),
        web.post("/v1/runs/{run}/action", act, expect_handler=_expect),
        web.post("/v1/runs/{run}/download", download, expect_handler=_expect),
        web.post("/v1/runs/{run}/login-code", login_code, expect_handler=_expect),
        web.post("/v1/runs/{run}/sign-in", sign_in, expect_handler=_expect),
        web.post("/v1/runs/{run}/search", search, expect_handler=_expect),
        web.post("/v1/runs/{run}/archive", archive, expect_handler=_expect),
        web.post("/v1/runs/{run}/live", live, expect_handler=_expect),
        web.post("/v1/control/runs/{run}/live-complete", live_complete, expect_handler=_expect),
        web.post("/v1/runs/{run}/finish", finish, expect_handler=_expect),
        web.post("/v1/runs/{run}/cancel", cancel, expect_handler=_expect),
        web.post("/v1/requests/cancel", cancel_requests, expect_handler=_expect),
        web.post("/v1/control/sites/{site}/forget", forget, expect_handler=_expect),
        web.get("/v1/records", records, **_GET),
        web.get("/v1/files/{file}", file_bytes, **_GET),
        web.post("/v1/control/files/{file}/delivery", delivery, expect_handler=_expect),
        web.get("/v1/notices", notices, **_GET),
        web.get("/v1/control/runs/{run}/judgements/pending", pending_judgements, **_GET),
        web.post("/v1/control/runs/{run}/judgements/{judgement}", judgement,
                 expect_handler=_expect),
        web.post("/v1/control/runs/{run}/questions/{question}/answer", answer,
                 expect_handler=_expect),
        web.get("/v1/control/runs/{run}/questions/{question}", question, **_GET),
        web.post("/v1/control/runs/{run}/questions/{question}/live", question_live,
                 expect_handler=_expect),
        web.route("*", "/{tail:.*}", not_found, expect_handler=_expect),
    ])
    return app


async def serve(app: web.Application, host: str, port: int) -> web.AppRunner:
    """Start the listener with access logging off (no path, query or header in a log)."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
