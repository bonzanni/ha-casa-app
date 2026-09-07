"""Unit tests for CasaAccessLogger (spec 5.5 §3.2.2–3)."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from casa_core_middleware import CasaAccessLogger
from log_cid import cid_var, install_logging

# #898: install_logging mutates process-global logging state; the guard fails
# any test in this module that leaves that state altered for the next test on
# its xdist worker. Autouse applies only where the name is imported.
from logging_state import casa_logging_guard  # noqa: F401


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal aiohttp-Request-like object supporting __getitem__.

    aiohttp's ``web.BaseRequest`` behaves as a ``MutableMapping``, which
    ``cid_middleware`` uses via ``request["cid"] = cid``. The real
    ``CasaAccessLogger`` reads that back via ``request["cid"]``, so the
    fake must support subscript access too.
    """

    def __init__(
        self,
        method: str = "GET",
        path: str = "/healthz",
        cid: str | None = None,
        path_only: str | None = None,
    ) -> None:
        self.method = method
        self.path_qs = path
        # aiohttp's ``request.path`` is the query-free (decoded) path; the
        # single-argument form keeps them equal, which is what every
        # pre-existing test wants.
        self.path = path_only if path_only is not None else path
        self._store: dict[str, str] = {}
        if cid is not None:
            self._store["cid"] = cid

    def __getitem__(self, key: str) -> str:
        return self._store[key]

    def __contains__(self, key: str) -> bool:
        return key in self._store


def _fake_request(
    method: str = "GET", path: str = "/healthz", cid: str | None = None,
    path_only: str | None = None,
) -> _FakeRequest:
    return _FakeRequest(method=method, path=path, cid=cid, path_only=path_only)


def _fake_response(status: int = 200, body_length: int = 42) -> SimpleNamespace:
    return SimpleNamespace(status=status, body_length=body_length)


# ---------------------------------------------------------------------------
# TestFormat
# ---------------------------------------------------------------------------


class TestFormat:
    def test_line_contains_method_path_status_duration_bytes(self, caplog):
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(_fake_request("POST", "/invoke/assistant"),
                   _fake_response(200, 117), 1.842)
        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert "method=POST" in msg
        assert "path=/invoke/assistant" in msg
        assert "status=200" in msg
        assert "duration_ms=1842" in msg
        assert "bytes=117" in msg

    def test_path_includes_query_string(self, caplog):
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(_fake_request("GET", "/probe?x=1&y=2"),
                   _fake_response(200, 0), 0.005)
        msg = caplog.records[0].getMessage()
        assert "path=/probe?x=1&y=2" in msg


# ---------------------------------------------------------------------------
# TestQuerySuppression (INV-CB-006, first of the three log surfaces)
# ---------------------------------------------------------------------------


class TestQuerySuppression:
    """The access line for a query-sensitive prefix logs ``request.path``,
    never ``path_qs``. The authorization-callback route deposits a bearer
    credential in the query string, so an access line carrying it would
    persist the credential in the container logs (INV-CB-006).
    """

    def test_prefix_set_is_module_level_and_frozen(self):
        from casa_core_middleware import QUERY_SUPPRESSED_PREFIXES

        assert QUERY_SUPPRESSED_PREFIXES == frozenset({"/callback/"})
        assert isinstance(QUERY_SUPPRESSED_PREFIXES, frozenset)

    def test_callback_query_is_suppressed(self, caplog):
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(
            _fake_request(
                "GET",
                "/callback/plg-demo--auth?code=SECRETVALUE&state=abc",
                path_only="/callback/plg-demo--auth",
            ),
            _fake_response(303, 0), 0.004,
        )
        msg = caplog.records[0].getMessage()
        assert "path=/callback/plg-demo--auth " in msg
        assert "SECRETVALUE" not in msg
        assert "?" not in msg

    def test_done_page_is_also_suppressed(self, caplog):
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(
            _fake_request("GET", "/callback/done?leak=1",
                          path_only="/callback/done"),
            _fake_response(200, 250), 0.001,
        )
        msg = caplog.records[0].getMessage()
        assert "path=/callback/done " in msg
        assert "leak=1" not in msg

    def test_bare_callback_path_is_suppressed(self, caplog):
        """``GET /callback?code=…`` 404s but still carries the credential in
        the query; the prefix match is segment-aware so the trailing-slash-less
        path is suppressed too."""
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(
            _fake_request("GET", "/callback?code=SECRETVALUE",
                          path_only="/callback"),
            _fake_response(404, 0), 0.001,
        )
        msg = caplog.records[0].getMessage()
        assert "path=/callback " in msg
        assert "SECRETVALUE" not in msg

    def test_non_matching_prefix_still_logs_the_query(self, caplog):
        """The suppression is prefix-scoped: every other route keeps the
        diagnostic value of a full request target."""
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(_fake_request("GET", "/callbackish?x=1",
                                 path_only="/callbackish"),
                   _fake_response(200, 0), 0.001)
        assert "path=/callbackish?x=1" in caplog.records[0].getMessage()

    def test_encoded_path_is_logged_not_the_decoded_one(self, caplog):
        """``request.path`` is DECODED, so ``/callback/x%0Aevil`` decodes to a
        path with a literal newline — logging it would let an
        unauthenticated caller forge access-log records. The prefix match
        runs on the decoded path (so an encoded ``/callback/`` cannot dodge
        suppression) but the logged value is the encoded ``raw_path``."""
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        request = _fake_request(
            "GET", "/callback/x%0AFORGED?code=SECRETVALUE",
            path_only="/callback/x\nFORGED",
        )
        request.rel_url = SimpleNamespace(raw_path="/callback/x%0AFORGED")
        access.log(request, _fake_response(303, 0), 0.001)
        msg = caplog.records[0].getMessage()
        assert "path=/callback/x%0AFORGED " in msg
        assert "\n" not in msg
        assert "SECRETVALUE" not in msg

    def test_request_without_rel_url_falls_back_to_path(self, caplog):
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(_fake_request("GET", "/callback/demo?code=SECRETVALUE",
                                 path_only="/callback/demo"),
                   _fake_response(303, 0), 0.001)
        msg = caplog.records[0].getMessage()
        assert "path=/callback/demo " in msg
        assert "SECRETVALUE" not in msg

    def test_request_without_path_attribute_falls_back(self, caplog):
        """Defensive: aiohttp always supplies both, but the logger must not
        raise if handed a stripped request-like object."""
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        request = SimpleNamespace(method="GET", path_qs="/healthz?x=1")
        access.log(request, _fake_response(), 0.001)
        assert "path=/healthz?x=1" in caplog.records[0].getMessage()


# ---------------------------------------------------------------------------
# TestCidInRecord
# ---------------------------------------------------------------------------


class TestCidInRecord:
    """CasaAccessLogger reads cid from ``request["cid"]`` (set by
    ``cid_middleware`` at ingress) and passes it via ``extra={}`` —
    not from ``cid_var``, which has been reset by the middleware's
    ``finally`` before aiohttp fires ``access_log.log()``.
    """

    def test_cid_from_request_dict(self, caplog):
        install_logging(level=logging.INFO)
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(_fake_request(cid="abcdef01"), _fake_response(), 0.010)
        assert caplog.records[0].cid == "abcdef01"

    def test_missing_request_cid_falls_back_to_dash(self, caplog):
        install_logging(level=logging.INFO)
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(_fake_request(), _fake_response(), 0.010)
        assert caplog.records[0].cid == "-"

    def test_ignores_contextvar(self, caplog):
        """Even if ``cid_var`` happens to be bound (e.g. during a
        nested test), the access logger reads from the request dict.
        This pins the behaviour against the aiohttp lifecycle quirk.
        """
        install_logging(level=logging.INFO)
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        token = cid_var.set("deadbeef")
        try:
            caplog.set_level(logging.INFO, logger="casa.access")
            access.log(_fake_request(cid="abcdef01"), _fake_response(), 0.010)
        finally:
            cid_var.reset(token)
        # request["cid"] wins, not cid_var
        assert caplog.records[0].cid == "abcdef01"


# ---------------------------------------------------------------------------
# TestLoggerWiring
# ---------------------------------------------------------------------------


class TestLoggerWiring:
    def test_emits_on_casa_access_logger(self, caplog):
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        caplog.set_level(logging.INFO, logger="casa.access")
        access.log(_fake_request(), _fake_response(), 0.001)
        assert caplog.records[0].name == "casa.access"
        assert caplog.records[0].levelno == logging.INFO


# ---------------------------------------------------------------------------
# TestJsonMode
# ---------------------------------------------------------------------------


class TestJsonMode:
    def test_json_line_parses_with_fields(
        self, monkeypatch, capsys
    ):
        monkeypatch.setenv("LOG_FORMAT", "json")
        install_logging(level=logging.INFO)
        logger = logging.getLogger("casa.access")
        access = CasaAccessLogger(logger)
        access.log(_fake_request("POST", "/healthz", cid="facefeed"),
                   _fake_response(200, 5), 0.002)
        captured = capsys.readouterr().out
        last = [line for line in captured.splitlines() if line.strip()][-1]
        payload = json.loads(last)
        assert payload["level"] == "INFO"
        assert payload["logger"] == "casa.access"
        assert payload["cid"] == "facefeed"
        assert "method=POST" in payload["msg"]
        assert "path=/healthz" in payload["msg"]
        assert "status=200" in payload["msg"]
