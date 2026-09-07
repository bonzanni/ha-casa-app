"""The public ``GET /callback/{name}`` endpoint (INV-CB-001/002/
004/005/006).

The endpoint is the facility's only unauthenticated surface, so almost every
test here is a *negative* one: the response must not vary with the outcome,
and no log surface may carry the query string. Two red cases carry the
weight:

- **INV-CB-005** — the responses for success and for every refusal cause are
  compared field by field (status, headers, ``Location``) in ONE test, at a
  drained sampler bucket as well as a fresh one. Any early return that forgets
  a header, or a 500 escaping the handler, fails it.
- **INV-CB-006** — a request carrying ``code=SECRETVALUE`` is driven through a
  real aiohttp server wired with the real ``CasaAccessLogger``; every
  ``LogRecord`` the app emits is captured and searched for the value.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

import callback_http
import callback_spool
from casa_core_middleware import CasaAccessLogger, cid_middleware

# #898: install_logging mutates process-global logging state; the guard fails
# any test in this module that leaves that state altered for the next test on
# its xdist worker. Autouse applies only where the name is imported.
from logging_state import (  # noqa: F401 — autouse where imported
    casa_logging_guard,
    casa_logging_restored,
)

# ``asyncio_mode = auto`` (pytest.ini) runs the async tests; the module mixes
# them with sync unit tests, so no module-level asyncio mark.

PLUGIN = "demo"
EFFECTIVE = "plg-demo--auth"
STATE = "S" * 22
LONG_STATE = "s" * 256


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@pytest.fixture()
def spool(tmp_path):
    sp = callback_spool.init_spool(tmp_path / "callbacks")
    sp.ensure_plugin_dirs(PLUGIN)
    try:
        yield sp
    finally:
        sp.close()
        callback_spool._SPOOL = None


class _Clock:
    """Injectable monotonic-ish clock for the sampler's bucket."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _registry(entries: dict[str, dict] | None = None):
    entries = entries if entries is not None else {
        EFFECTIVE: {"plugin": PLUGIN, "declared": "auth", "effective": EFFECTIVE},
    }
    return SimpleNamespace(get_callback=lambda name: entries.get(name))


def _build_app(*, registry=None, sampler=None, clock=None, middlewares=()):
    app = web.Application(middlewares=list(middlewares))
    handler = callback_http.make_callback_handler(
        trigger_registry=registry if registry is not None else _registry(),
        sampler=sampler,
        clock=clock or time.time,
    )
    # Registration ORDER is load-bearing: the static route must be matched
    # before the wildcard can swallow it.
    app.router.add_get("/callback/done", callback_http.make_done_handler())
    app.router.add_get("/callback/{name}", handler)
    return app


@asynccontextmanager
async def _client(app, *, access_log=False):
    server = TestServer(app)
    if access_log:
        await server.start_server(
            access_log_class=CasaAccessLogger,
            access_log=logging.getLogger("casa.access"),
        )
    async with TestClient(server) as client:
        yield client


def _plugin_dir(spool) -> Path:
    return Path(spool.root) / PLUGIN


def _results_dir(spool) -> Path:
    return _plugin_dir(spool) / callback_spool.RESULTS_DIR


def _result_path(spool, state: str) -> Path:
    return _results_dir(spool) / f"{callback_spool.state_hash(state)}.json"


def _pending_path(spool, state: str) -> Path:
    return (_plugin_dir(spool) / callback_spool.PENDING_DIR
            / f"{callback_spool.state_hash(state)}.json")


async def _get(client, target: str, **kw):
    """GET a byte-exact request target (``encoded=True`` keeps ``%61``/``+``
    off the client's re-encoding path) without following the redirect."""
    kw.setdefault("allow_redirects", False)
    return await client.get(URL(target, encoded=True), **kw)


def _shape(response) -> tuple:
    """The comparable surface of a response: status + every header except the
    two the server stamps per-connection."""
    volatile = {"Date", "Server"}
    return (
        response.status,
        tuple(sorted(
            (k, v) for k, v in response.headers.items() if k not in volatile
        )),
    )


# ---------------------------------------------------------------------------
# success path
# ---------------------------------------------------------------------------


class TestSuccess:
    async def test_publishes_result_and_redirects(self, spool):
        callback_spool.mint(_plugin_dir(spool), STATE)
        app = _build_app()
        async with _client(app) as client:
            r = await _get(client, f"/callback/{EFFECTIVE}?code=abc&state={STATE}")
        assert r.status == 303
        assert r.headers["Location"] == "/callback/done"

        record = json.loads(_result_path(spool, STATE).read_text())
        assert record["v"] == 1
        assert record["plugin"] == PLUGIN
        assert record["effective"] == EFFECTIVE
        assert isinstance(record["received_at"], float)
        assert record["raw_query"] == f"code=abc&state={STATE}"
        assert record["query"] == [["code", "abc"], ["state", STATE]]
        # The pending twin is consumed, and no claim residue is left.
        assert not _pending_path(spool, STATE).exists()
        assert list((_plugin_dir(spool) / callback_spool.CLAIMS_DIR).iterdir()) == []
        assert spool.in_flight() == set()

    async def test_result_file_is_0600(self, spool):
        callback_spool.mint(_plugin_dir(spool), STATE)
        app = _build_app()
        async with _client(app) as client:
            await _get(client, f"/callback/{EFFECTIVE}?state={STATE}")
        mode = os.stat(_result_path(spool, STATE)).st_mode & 0o777
        assert mode == 0o600

    async def test_nudge_kicked_on_success_only(self, spool, monkeypatch):
        kicks: list[tuple[str, str]] = []
        monkeypatch.setattr(callback_http, "_nudge",
                            lambda plugin, h: kicks.append((plugin, h)))
        callback_spool.mint(_plugin_dir(spool), STATE)
        app = _build_app()
        async with _client(app) as client:
            await _get(client, f"/callback/{EFFECTIVE}?state={STATE}")
            assert kicks == [(PLUGIN, callback_spool.state_hash(STATE))]
            # A replay publishes nothing, so it must not kick either.
            await _get(client, f"/callback/{EFFECTIVE}?state={STATE}")
            await _get(client, f"/callback/{EFFECTIVE}?state={LONG_STATE}")
        assert kicks == [(PLUGIN, callback_spool.state_hash(STATE))]

    async def test_nudge_seam_delegates_to_episode_kick(self, monkeypatch):
        # The nudge seam delegates to callback_episodes.kick: it records an
        # in-memory hint and signals the worker, touching NO durable state
        # (request-path O(1)). Reaching for the spool at all — the only
        # filesystem the module has since v0.147 retired its JSON store —
        # would prove otherwise.
        import callback_episodes
        monkeypatch.setattr(
            callback_episodes, "_get_spool",
            lambda *a, **k: pytest.fail("kick reached for the spool"))
        callback_episodes._pending_hints.clear()
        h = "0" * 64
        assert callback_http._nudge(PLUGIN, h) is None
        assert (PLUGIN, h) in callback_episodes._pending_hints
        callback_episodes._pending_hints.clear()


# ---------------------------------------------------------------------------
# INV-CB-005 — uniform neutrality
# ---------------------------------------------------------------------------


class TestNeutralUniformity:
    async def _causes(self, spool, client, monkeypatch):
        """One (label, response) pair per outcome the handler can reach."""
        out: list[tuple[str, object]] = []

        callback_spool.mint(_plugin_dir(spool), STATE)
        out.append(("success", await _get(
            client, f"/callback/{EFFECTIVE}?code=x&state={STATE}")))
        out.append(("replay", await _get(
            client, f"/callback/{EFFECTIVE}?code=x&state={STATE}")))
        out.append(("unknown_name", await _get(
            client, f"/callback/plg-nope--nope?state={STATE}")))
        out.append(("missing_state", await _get(
            client, f"/callback/{EFFECTIVE}")))
        out.append(("empty_state", await _get(
            client, f"/callback/{EFFECTIVE}?state=")))
        out.append(("short_state", await _get(
            client, f"/callback/{EFFECTIVE}?state=tooshort")))
        out.append(("charset_state", await _get(
            client, f"/callback/{EFFECTIVE}?state=" + "%41" * 22)))
        out.append(("duplicate_state", await _get(
            client, f"/callback/{EFFECTIVE}?state={STATE}&state={STATE}")))
        # Over the module's own cap but under aiohttp's request-line limit,
        # so it genuinely REACHES the application (which is INV-CB-005's
        # scope); a longer line is refused by the parser — see
        # TestNonGet::test_overlong_request_line_is_the_framework_400.
        out.append(("oversized_query", await _get(
            client,
            f"/callback/{EFFECTIVE}?state={STATE}&pad="
            + "p" * (callback_http.MAX_RAW_QUERY + 100))))
        out.append(("unknown_state", await _get(
            client, f"/callback/{EFFECTIVE}?state=" + "u" * 22)))

        expired = "e" * 22
        minted = callback_spool.mint(_plugin_dir(spool), expired)
        old = time.time() - callback_spool.PENDING_TTL_S - 60
        os.utime(minted, (old, old))
        out.append(("expired", await _get(
            client, f"/callback/{EFFECTIVE}?state={expired}")))

        exists = "x" * 22
        callback_spool.mint(_plugin_dir(spool), exists)
        _result_path(spool, exists).write_bytes(b'{"v": 1}')
        out.append(("result_exists", await _get(
            client, f"/callback/{EFFECTIVE}?state={exists}")))

        failing = "w" * 22
        callback_spool.mint(_plugin_dir(spool), failing)

        def _boom(claim, record):
            raise OSError(28, "no space left on device")

        monkeypatch.setattr(spool, "publish_result", _boom)
        out.append(("write_failed", await _get(
            client, f"/callback/{EFFECTIVE}?state={failing}")))
        monkeypatch.undo()

        return out

    async def test_every_cause_is_byte_identical(self, spool, monkeypatch):
        app = _build_app()
        async with _client(app) as client:
            causes = await self._causes(spool, client, monkeypatch)
        shapes = {label: _shape(r) for label, r in causes}
        reference = shapes["success"]
        assert reference[0] == 303
        for label, shape in shapes.items():
            assert shape == reference, f"{label} differs from success"
        for _label, r in causes:
            assert r.headers["Location"] == "/callback/done"

    async def test_uniform_at_a_drained_sampler_bucket(self, spool, monkeypatch):
        """Flood response-uniformity: emission damping must never reach the
        response. Same comparison with the bucket exhausted first."""
        clock = _Clock()
        sampler = callback_http.OutcomeSampler(capacity=2, now=clock)
        app = _build_app(sampler=sampler)
        async with _client(app) as client:
            for _ in range(20):
                await _get(client, f"/callback/{EFFECTIVE}?state=" + "d" * 22)
            causes = await self._causes(spool, client, monkeypatch)
        shapes = {label: _shape(r) for label, r in causes}
        reference = shapes["success"]
        for label, shape in shapes.items():
            assert shape == reference, f"{label} differs under flood"

    async def test_handler_fault_still_neutral(self, spool, monkeypatch):
        """A registry that raises must not become a 500 (a differentiated
        status is an oracle)."""
        def _explode(name):
            raise RuntimeError("registry fault")

        app = _build_app(registry=SimpleNamespace(get_callback=_explode))
        async with _client(app) as client:
            r = await _get(client, f"/callback/{EFFECTIVE}?state={STATE}")
            good = _build_app()
            async with _client(good) as other:
                baseline = await _get(other, "/callback/plg-nope--nope")
        assert _shape(r) == _shape(baseline)

    async def test_observability_fault_does_not_differentiate(self, spool):
        """INV-CB-005's last defence: an exception from the outcome sampler
        (observability) must not escape into a 500 — a differentiated status
        is an oracle. The response stays byte-identical to the reference."""
        class _BrokenSampler:
            def should_log(self, key):
                raise RuntimeError("metrics backend down")

        callback_spool.mint(_plugin_dir(spool), STATE)
        broken = _build_app(sampler=_BrokenSampler())
        good = _build_app()
        async with _client(broken) as bad_client, _client(good) as ok_client:
            faulted = await _get(
                bad_client, f"/callback/{EFFECTIVE}?state={STATE}")
            reference = await _get(
                ok_client, "/callback/plg-nope--nope?state=" + "z" * 22)
        assert faulted.status == 303
        assert _shape(faulted) == _shape(reference)
        assert faulted.headers["Location"] == "/callback/done"

    async def test_neutral_when_spool_absent(self, monkeypatch):
        """Before boot wires a spool, the route still answers neutrally."""
        monkeypatch.setattr(callback_spool, "_SPOOL", None)
        app = _build_app()
        async with _client(app) as client:
            r = await _get(client, f"/callback/{EFFECTIVE}?state={STATE}")
        assert r.status == 303
        assert r.headers["Location"] == "/callback/done"


# ---------------------------------------------------------------------------
# INV-CB-001 / INV-CB-002 — no mutation, publish-once
# ---------------------------------------------------------------------------


class TestSpoolEffects:
    async def test_unknown_name_mutates_nothing(self, spool):
        callback_spool.mint(_plugin_dir(spool), STATE)
        before = _pending_path(spool, STATE).read_bytes()
        app = _build_app()
        async with _client(app) as client:
            await _get(client, f"/callback/plg-nope--nope?state={STATE}")
        assert _pending_path(spool, STATE).read_bytes() == before
        assert list(_results_dir(spool).iterdir()) == []

    async def test_replay_never_rewrites_the_result(self, spool):
        callback_spool.mint(_plugin_dir(spool), STATE)
        app = _build_app()
        async with _client(app) as client:
            await _get(client, f"/callback/{EFFECTIVE}?code=first&state={STATE}")
            published = _result_path(spool, STATE).read_bytes()
            stat_before = os.stat(_result_path(spool, STATE))
            # A replayed redirect carrying a DIFFERENT code loses identically.
            await _get(client, f"/callback/{EFFECTIVE}?code=second&state={STATE}")
        after = _result_path(spool, STATE).read_bytes()
        stat_after = os.stat(_result_path(spool, STATE))
        assert after == published
        assert b"second" not in after
        assert (stat_after.st_ino, stat_after.st_mtime) == (
            stat_before.st_ino, stat_before.st_mtime)
        assert len(list(_results_dir(spool).iterdir())) == 1

    async def test_publish_raise_leaves_the_claim_for_recovery(self, spool, monkeypatch):
        """Amendment 2 (changed contract): a RAISING publish may predate any
        durable record, so the handler must NOT discard — the claim survives
        for the recovery pass to restore. Only FAILED_RECORDED authorizes a
        discard. The fake clears the in-flight key exactly as the real
        publish's ``finally`` does even when raising."""
        callback_spool.mint(_plugin_dir(spool), STATE)
        h = callback_spool.state_hash(STATE)

        def _boom(claim, record):
            spool._in_flight.discard(claim.key)
            raise OSError(28, "no space left on device")

        monkeypatch.setattr(spool, "publish_result", _boom)
        app = _build_app()
        async with _client(app) as client:
            r = await _get(client, f"/callback/{EFFECTIVE}?state={STATE}")
        assert r.status == 303
        claims = _plugin_dir(spool) / callback_spool.CLAIMS_DIR
        assert [p.name for p in claims.iterdir()] == [h], \
            "the claim is LEFT — an exception authorizes no discard"
        assert not _pending_path(spool, STATE).exists()
        assert list(_results_dir(spool).iterdir()) == []
        # A leaked in-flight hash would make the recovery pass skip that
        # claim for the rest of the process's life.
        assert spool.in_flight() == set()

    async def test_in_flight_drains_on_every_path(self, spool):
        callback_spool.mint(_plugin_dir(spool), STATE)
        exists = "x" * 22
        callback_spool.mint(_plugin_dir(spool), exists)
        _result_path(spool, exists).write_bytes(b'{"v": 1}')
        app = _build_app()
        async with _client(app) as client:
            for raw in (f"state={STATE}", f"state={STATE}", "state=bad",
                        f"state={exists}", "state=" + "n" * 22):
                await _get(client, f"/callback/{EFFECTIVE}?{raw}")
                assert spool.in_flight() == set()

    async def test_refusals_leave_no_result(self, spool):
        app = _build_app()
        async with _client(app) as client:
            await _get(client, f"/callback/{EFFECTIVE}?state=" + "n" * 22)
            await _get(client, f"/callback/{EFFECTIVE}?state=short")
        assert list(_results_dir(spool).iterdir()) == []


# ---------------------------------------------------------------------------
# tri-state publish outcome wiring (amendments 1+2): only FAILED_RECORDED
# authorizes a discard; everything else leaves the claim (or needs nothing)
# ---------------------------------------------------------------------------


class TestPublishOutcomeWiring:
    """Each ``PublishOutcome`` drives exactly one handler behavior:
    PUBLISHED ⇒ nudge kick, no discard; FAILED_RECORDED ⇒ discard exactly
    once (the only outcome that authorizes it — the failure is durably
    recorded); FAILED_UNRECORDED and a raising publish ⇒ NO discard (the
    claim is left for recovery). The response is the one neutral 303 in
    every arm (INV-CB-005)."""

    async def _drive(self, spool, monkeypatch, *, outcome=None, raises=None):
        callback_spool.mint(_plugin_dir(spool), STATE)
        discards: list = []
        kicks: list[tuple[str, str]] = []

        def fake_publish(claim, record):
            # The real publish clears the in-flight key in its ``finally``
            # even when raising; the fake must mimic that contract.
            spool._in_flight.discard(claim.key)
            if raises is not None:
                raise raises
            return outcome

        monkeypatch.setattr(spool, "publish_result", fake_publish)
        monkeypatch.setattr(spool, "discard_claim",
                            lambda claim: discards.append(claim))
        monkeypatch.setattr(callback_http, "_nudge",
                            lambda plugin, h: kicks.append((plugin, h)))
        app = _build_app()
        async with _client(app) as client:
            r = await _get(client, f"/callback/{EFFECTIVE}?state={STATE}")
        return r, discards, kicks

    async def test_published_kicks_the_nudge_and_never_discards(
            self, spool, monkeypatch):
        r, discards, kicks = await self._drive(
            spool, monkeypatch,
            outcome=callback_spool.PublishOutcome.PUBLISHED)
        assert r.status == 303
        assert r.headers["Location"] == "/callback/done"
        assert kicks == [(PLUGIN, callback_spool.state_hash(STATE))]
        assert discards == []

    async def test_failed_recorded_discards_exactly_once(
            self, spool, monkeypatch, caplog):
        with caplog.at_level(logging.INFO, logger="callback_http"):
            r, discards, kicks = await self._drive(
                spool, monkeypatch,
                outcome=callback_spool.PublishOutcome.FAILED_RECORDED)
        assert r.status == 303
        assert len(discards) == 1, "a durably RECORDED failure discards"
        assert kicks == []
        assert any("outcome=write_failed" in rec.getMessage()
                   for rec in caplog.records), "reason is write_failed"

    async def test_failed_unrecorded_never_discards(self, spool, monkeypatch):
        r, discards, kicks = await self._drive(
            spool, monkeypatch,
            outcome=callback_spool.PublishOutcome.FAILED_UNRECORDED)
        assert r.status == 303
        assert discards == [], "nothing durable — the claim is LEFT"
        assert kicks == []

    async def test_publish_raise_never_discards(self, spool, monkeypatch):
        r, discards, kicks = await self._drive(
            spool, monkeypatch, raises=OSError(28, "no space left on device"))
        assert r.status == 303
        assert discards == [], "an exception may predate any durable record"
        assert kicks == []


# ---------------------------------------------------------------------------
# INV-CB-004 — opaque relay
# ---------------------------------------------------------------------------


class TestOpaqueRelay:
    async def test_provider_error_is_a_result_with_wire_fidelity(self, spool):
        callback_spool.mint(_plugin_dir(spool), STATE)
        raw = (f"error=access_denied&error_description=User+said+no"
               f"&state={STATE}&%61b=%7E&bare&pct=100%25&bad=%zz")
        app = _build_app()
        async with _client(app) as client:
            r = await _get(client, f"/callback/{EFFECTIVE}?{raw}")
        assert r.status == 303
        record = json.loads(_result_path(spool, STATE).read_text())
        assert record["raw_query"] == raw
        assert record["query"] == [
            ["error", "access_denied"],
            ["error_description", "User said no"],
            ["state", STATE],
            ["ab", "~"],
            ["bare", ""],
            ["pct", "100%"],
            ["bad", "%zz"],
        ]

    async def test_duplicate_keys_and_order_preserved(self, spool):
        callback_spool.mint(_plugin_dir(spool), STATE)
        raw = f"scope=a&scope=b&state={STATE}"
        app = _build_app()
        async with _client(app) as client:
            await _get(client, f"/callback/{EFFECTIVE}?{raw}")
        record = json.loads(_result_path(spool, STATE).read_text())
        assert record["query"] == [
            ["scope", "a"], ["scope", "b"], ["state", STATE]]

    async def test_utf8_and_invalid_bytes_decode_with_replacement(self, spool):
        callback_spool.mint(_plugin_dir(spool), STATE)
        raw = f"eur=%E2%82%AC&broken=%FF&state={STATE}"
        app = _build_app()
        async with _client(app) as client:
            await _get(client, f"/callback/{EFFECTIVE}?{raw}")
        record = json.loads(_result_path(spool, STATE).read_text())
        pairs = dict(record["query"])
        assert pairs["eur"] == "€"
        assert pairs["broken"] == "�"
        assert record["raw_query"] == raw


class TestDecodeRules:
    """Query-component decode rules, unit-level."""

    @pytest.mark.parametrize("raw,expected", [
        ("plain", "plain"),
        ("a+b", "a b"),
        ("a%20b", "a b"),
        ("%41%42", "AB"),
        ("%4a", "J"),          # lowercase hex digits are valid
        ("100%25", "100%"),
        ("%zz", "%zz"),        # malformed: verbatim
        ("%", "%"),
        ("%A", "%A"),          # truncated: verbatim
        ("%E2%82%AC", "€"),
        ("%FF", "�"),
        ("", ""),
    ])
    def test_component_decoding(self, raw, expected):
        assert callback_http._decode_component(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("", []),
        ("a=1", [["a", "1"]]),
        ("bare", [["bare", ""]]),
        ("a=1&&b=2", [["a", "1"], ["b", "2"]]),
        ("a=1=2", [["a", "1=2"]]),      # only the FIRST '=' splits
        ("=v", [["", "v"]]),
    ])
    def test_pair_split(self, raw, expected):
        assert callback_http._decoded_pairs(raw) == expected


class TestStateExtraction:
    @pytest.mark.parametrize("raw", [
        "",
        "code=x",
        "state=",
        "state",
        "state=short",
        "state=" + "a" * 21,
        "state=" + "a" * 257,
        "state=" + "a" * 21 + "%20",     # no decoding: '%' is off-charset
        "state=has space",
        "state=" + "a" * 21 + "/",
        "state=" + "a" * 21 + "+",
        "state=" + "a" * 22 + "&state=" + "b" * 22,
        "state=" + "a" * 22 + "&state",
    ])
    def test_rejected(self, raw):
        assert callback_http._extract_state(raw) is None

    @pytest.mark.parametrize("raw,expected", [
        ("state=" + "a" * 22, "a" * 22),
        ("state=" + "a" * 256, "a" * 256),
        ("code=1&state=" + "A-z_9.~" * 4, "A-z_9.~" * 4),
        ("x=1&state=" + "b" * 30 + "&y=2", "b" * 30),
        ("statement=1&state=" + "c" * 22, "c" * 22),
    ])
    def test_accepted(self, raw, expected):
        assert callback_http._extract_state(raw) == expected

    def test_pre_filesystem_caps(self):
        # Total captured size: each segment is individually legal (under the
        # pair, key and value caps), so only the whole-query cap can refuse
        # this one.
        chunk = "x" * (callback_http.MAX_VALUE - 1)
        segments = callback_http.MAX_RAW_QUERY // len(chunk) + 1
        over = "&".join(f"p{i}={chunk}" for i in range(segments))
        assert len(over) > callback_http.MAX_RAW_QUERY
        assert segments <= callback_http.MAX_PAIRS
        assert callback_http._extract_state(
            "state=" + "a" * 22 + "&" + over) is None
        many = "&".join(f"k{i}=v" for i in range(callback_http.MAX_PAIRS + 1))
        assert callback_http._extract_state(
            many + "&state=" + "a" * 22) is None
        long_value = "state=" + "a" * 22 + "&v=" + "x" * (
            callback_http.MAX_VALUE + 1)
        assert callback_http._extract_state(long_value) is None
        long_key = "state=" + "a" * 22 + "&" + "k" * (
            callback_http.MAX_KEY + 1) + "=v"
        assert callback_http._extract_state(long_key) is None


# ---------------------------------------------------------------------------
# the done page + no-loop
# ---------------------------------------------------------------------------


class TestDonePage:
    async def test_static_route_wins_over_the_wildcard(self, spool):
        app = _build_app()
        async with _client(app) as client:
            r = await _get(client, "/callback/done")
            body = await r.text()
        assert r.status == 200
        assert "Location" not in r.headers
        assert ("Response received. You can close this tab and return to "
                "your chat.") in body

    async def test_redirect_chain_terminates(self, spool):
        callback_spool.mint(_plugin_dir(spool), STATE)
        app = _build_app()
        async with _client(app) as client:
            r = await client.get(
                URL(f"/callback/{EFFECTIVE}?state={STATE}", encoded=True),
                allow_redirects=True, max_redirects=5)
            body = await r.text()
        assert r.status == 200
        assert len(r.history) == 1
        assert r.history[0].status == 303
        assert "Location" not in r.headers
        assert "Response received." in body

    async def test_page_interpolates_no_request_data(self, spool):
        app = _build_app()
        async with _client(app) as client:
            r = await _get(client, "/callback/done?code=SECRETVALUE&x=y")
            first = await r.text()
            r2 = await _get(client, "/callback/done")
            second = await r2.text()
        assert "SECRETVALUE" not in first
        assert first == second

    async def test_security_headers_on_both_responses(self, spool):
        app = _build_app()
        async with _client(app) as client:
            page = await _get(client, "/callback/done")
            redirect = await _get(client, f"/callback/{EFFECTIVE}?state=nope")
        for response in (page, redirect):
            for header, value in callback_http.SECURITY_HEADERS.items():
                assert response.headers[header] == value
        assert callback_http.SECURITY_HEADERS == {
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy":
                "default-src 'none'; style-src 'unsafe-inline'",
        }

    async def test_page_has_no_external_references(self, spool):
        app = _build_app()
        async with _client(app) as client:
            body = await (await _get(client, "/callback/done")).text()
        lowered = body.lower()
        for token in ("http://", "https://", "//cdn", "<script", "<img",
                      "<link", "url("):
            assert token not in lowered


# ---------------------------------------------------------------------------
# INV-CB-006 — log hygiene
# ---------------------------------------------------------------------------


class TestLogHygiene:
    async def test_no_log_record_carries_the_code(self, spool, caplog):
        callback_spool.mint(_plugin_dir(spool), STATE)
        caplog.set_level(logging.DEBUG)
        app = _build_app(middlewares=[cid_middleware])
        async with _client(app, access_log=True) as client:
            await _get(
                client,
                f"/callback/{EFFECTIVE}?code=SECRETVALUE&state={STATE}")
            # The access line is emitted from the connection task after the
            # handler returns; give the loop a turn to run it.
            await asyncio.sleep(0.05)
        assert any(r.name == "casa.access" for r in caplog.records), \
            "the access logger must have run for this request"
        for record in caplog.records:
            rendered = f"{record.getMessage()} {record.args!r}"
            assert "SECRETVALUE" not in rendered, record.name
            assert STATE not in rendered, record.name

    async def test_access_line_keeps_the_path(self, spool, caplog):
        caplog.set_level(logging.INFO)
        app = _build_app(middlewares=[cid_middleware])
        async with _client(app, access_log=True) as client:
            await _get(client, f"/callback/{EFFECTIVE}?code=SECRETVALUE")
            await asyncio.sleep(0.05)
        access = [r for r in caplog.records if r.name == "casa.access"]
        assert access
        assert f"path=/callback/{EFFECTIVE}" in access[0].getMessage()

    async def test_no_log_line_is_forgeable_through_the_route_component(
            self, spool, caplog):
        """End-to-end companion to the access-logger unit test: a route
        component decoding to a newline must not produce a second, forged log
        line on ANY surface."""
        caplog.set_level(logging.INFO)
        app = _build_app(middlewares=[cid_middleware])
        async with _client(app, access_log=True) as client:
            await _get(client,
                       "/callback/x%0AFORGED%20access%20line?code=SECRETVALUE")
            await asyncio.sleep(0.05)
        access = [r for r in caplog.records if r.name == "casa.access"]
        assert len(access) == 1
        for record in caplog.records:
            message = record.getMessage()
            assert "\n" not in message, record.name
            assert "SECRETVALUE" not in message, record.name
        # The escaped spelling is what lands in the line — one record, no
        # decoded newline, so no second (forged) line.
        assert "%0AFORGED" in access[0].getMessage()

    async def test_overlong_request_line_leaks_no_code_on_any_logger(
            self, spool, caplog):
        """INV-CB-006's third surface: a request line over 8190 bytes raises
        ``LineTooLong`` below the handler, and aiohttp's ``handle_error`` logs
        the offending bytes (query + code) at ERROR on ``aiohttp.server``.
        ``install_callback_log_redaction`` must scrub it before any handler
        formats the record."""
        callback_http.install_callback_log_redaction()
        caplog.set_level(logging.DEBUG)
        app = _build_app(middlewares=[cid_middleware])
        async with _client(app, access_log=True) as client:
            try:
                await _get(
                    client,
                    f"/callback/{EFFECTIVE}?code=SECRETVALUE&pad="
                    + "p" * 9000)
            except Exception:  # noqa: BLE001 — the connection is dropped
                pass
            await asyncio.sleep(0.05)
        assert any(r.name == "aiohttp.server" for r in caplog.records), \
            "the parser error must have been logged (else the test is moot)"
        formatter = logging.Formatter()
        for record in caplog.records:
            rendered = formatter.format(record)   # includes the traceback
            assert "SECRETVALUE" not in rendered, record.name

    async def test_overlong_line_with_an_inner_quote_leaks_no_code(
            self, caplog):
        """The inner-quote case end-to-end through a REAL server: a raw request
        line whose query carries a single quote before the code
        (``?x='&code=SECRETVALUE'MORESECRET…``) drives the real ``LineTooLong``
        (whose bytes repr escapes the quote and flips its delimiter). Neither
        the fragment before the quote nor the one after may survive on ANY
        logger — the whole target is redacted."""
        callback_http.install_callback_log_redaction()
        caplog.set_level(logging.DEBUG)
        app = _build_app(middlewares=[cid_middleware])
        server = TestServer(app)
        await server.start_server()
        try:
            reader, writer = await asyncio.open_connection(
                server.host, server.port)
            target = ("/%63allback/x?x='&code=SECRETVALUE'MORESECRET&pad="
                      + "p" * 9000)
            writer.write(
                f"GET {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            try:
                await writer.drain()
                await reader.read(200)
            except Exception:  # noqa: BLE001 — the connection is dropped
                pass
            writer.close()
            await asyncio.sleep(0.05)
        finally:
            await server.close()
        assert any(r.name == "aiohttp.server" for r in caplog.records), \
            "the parser error must have been logged (else the test is moot)"
        formatter = logging.Formatter()
        for record in caplog.records:
            rendered = formatter.format(record)   # includes the traceback
            assert "SECRETVALUE" not in rendered, record.name
            assert "MORESECRET" not in rendered, record.name

    async def test_install_callback_log_redaction_is_idempotent(self):
        srv = logging.getLogger("aiohttp.server")
        callback_http.install_callback_log_redaction()
        callback_http.install_callback_log_redaction()
        redactors = [f for f in srv.filters
                     if isinstance(f, callback_http._AiohttpServerRedactor)]
        assert len(redactors) == 1

    def test_redactor_leaves_unrelated_records_untouched(self):
        redactor = callback_http._AiohttpServerRedactor()
        record = logging.LogRecord(
            "aiohttp.server", logging.INFO, __file__, 0,
            "connection from %s established", ("a-peer",), None)
        assert redactor.filter(record) is True
        assert record.getMessage() == "connection from a-peer established"

    def _exc_record(self, message: str) -> logging.LogRecord:
        try:
            raise ValueError(message)
        except ValueError:
            import sys
            return logging.LogRecord(
                "aiohttp.server", logging.ERROR, __file__, 0,
                "Error handling request from 127.x", (), sys.exc_info())

    def test_json_formatter_keeps_exc_redacted_not_dropped(self):
        """The production default is ``JsonFormatter`` (LOG_FORMAT unset →
        JSON), which attaches ``exc`` only when ``exc_info`` is truthy. The
        redactor clears ``exc_info`` after caching ``exc_text``, so the
        formatter MUST fall back to the cache — otherwise the whole ``exc``
        field vanishes rather than being redacted."""
        from log_cid import JsonFormatter

        record = self._exc_record(
            "Got more than 8190 bytes when reading: bytearray(b'GET "
            "/callback/x?code=SECRETVALUE&pad=pppp')")
        callback_http._AiohttpServerRedactor().filter(record)
        assert record.exc_info is None            # cleared by the redactor
        payload = json.loads(JsonFormatter().format(record))
        assert "SECRETVALUE" not in json.dumps(payload)
        assert "exc" in payload, "the exc field must survive redaction"
        assert "<redacted>" in payload["exc"]
        # The WHOLE target is redacted (path AND query) — not just the query —
        # so no path fragment survives either.
        assert "/callback/x" not in payload["exc"]
        assert "b'GET <redacted>'" in payload["exc"]

    def test_redactor_keeps_an_unrelated_query_path_and_exc(self):
        """A plain diagnostic carrying a query-bearing path that is NOT in
        request-line form (no HTTP method, no bytes repr) — e.g.
        ``/data/file?reason=permission_denied`` — must keep its query tail AND
        its ``exc_info``. The redactor is scoped to request lines, not any
        ``/path?x=y`` substring, so unrelated diagnostics keep observability."""
        from log_cid import JsonFormatter

        record = self._exc_record(
            "operation failed for /data/file?reason=permission_denied")
        callback_http._AiohttpServerRedactor().filter(record)
        assert record.exc_info is not None            # untouched — not a request line
        assert record.getMessage() == "Error handling request from 127.x"
        payload = json.loads(JsonFormatter().format(record))
        assert "reason=permission_denied" in payload["exc"]
        assert "<redacted>" not in payload["exc"]

    def test_redactor_redacts_a_bare_request_line(self):
        """A real request line embedded in a message — method + origin-form
        target + HTTP version, no bytes repr — has its WHOLE target redacted:
        the method and version survive, the path and query do not."""
        record = logging.LogRecord(
            "aiohttp.server", logging.ERROR, __file__, 0,
            "bad request line: GET /callback/x?code=SECRET HTTP/1.1", (), None)
        callback_http._AiohttpServerRedactor().filter(record)
        msg = record.getMessage()
        assert "SECRET" not in msg
        assert "/callback/x" not in msg                # path gone too
        assert "GET <redacted> HTTP/1.1" in msg        # method + version kept

    def test_redactor_redacts_the_whole_target_past_an_inner_quote(self):
        """The inner-quote leak: a target whose query carries a quote before the
        code (``?x='&code=…``, the ``'`` rendered ``\\'`` inside the bytes repr,
        which flips the repr delimiter to ``"``). A 'stop at a quote' redaction
        would leave ``code=…`` behind; redacting the WHOLE target to the
        MATCHING closing quote leaves no fragment on either side of the quote."""
        record = logging.LogRecord(
            "aiohttp.server", logging.ERROR, __file__, 0,
            "Got more than 8190 bytes when reading: "
            r"""bytearray(b"/callback/x?x=\'&code=SECRETVALUE&pad=pppp")""",
            (), None)
        callback_http._AiohttpServerRedactor().filter(record)
        msg = record.getMessage()
        assert "SECRETVALUE" not in msg
        assert "code=" not in msg                      # nothing after the quote
        assert "/callback/x" not in msg                # nothing before it either
        assert 'b"<redacted>"' in msg

    def test_json_formatter_keeps_unrelated_exc_intact(self):
        """An ``aiohttp.server`` ERROR whose traceback
        merely mentions a ``…/callback_http.py`` frame — no query — must keep
        its full ``exc`` field. The redactor's trigger is the query-bearing
        request-line shape, not the bare ``/callback`` substring."""
        from log_cid import JsonFormatter

        record = self._exc_record(
            "unrelated failure raised from /opt/casa/callback_http.py:123")
        callback_http._AiohttpServerRedactor().filter(record)
        assert record.exc_info is not None        # untouched — no query present
        payload = json.loads(JsonFormatter().format(record))
        assert "exc" in payload
        assert "<redacted>" not in payload["exc"]
        assert "unrelated failure" in payload["exc"]

    async def test_overlong_line_redacted_through_production_json(
            self, spool, monkeypatch, capsys):
        """End-to-end through the REAL production pipeline: LOG_FORMAT unset
        (JSON default), the casa root handler installed, and the LineTooLong
        record emitted by a real server. The emitted JSON line must carry no
        code AND still carry a redacted ``exc`` field."""
        from log_cid import install_logging

        monkeypatch.delenv("LOG_FORMAT", raising=False)   # JSON is the default
        # #898: install_logging is process-global. Undo it here, or every later
        # test on this xdist worker logs at DEBUG through a handler bound to
        # this test's (by then closed) capture object.
        with casa_logging_restored():
            install_logging(level=logging.DEBUG)
            callback_http.install_callback_log_redaction()
            app = _build_app(middlewares=[cid_middleware])
            async with _client(app, access_log=True) as client:
                try:
                    await _get(
                        client,
                        f"/callback/{EFFECTIVE}?code=SECRETVALUE&pad="
                        + "p" * 9000)
                except Exception:  # noqa: BLE001 — the connection is dropped
                    pass
                await asyncio.sleep(0.05)
            out = capsys.readouterr().out
        assert "SECRETVALUE" not in out
        server_lines = [
            json.loads(line) for line in out.splitlines()
            if line.strip().startswith("{")
            and json.loads(line).get("logger") == "aiohttp.server"
        ]
        assert server_lines, "the parser error must have reached stdout"
        with_exc = [p for p in server_lines if "exc" in p]
        assert with_exc, "the LineTooLong record must keep an exc field"
        assert any("<redacted>" in p["exc"] for p in with_exc)

    async def test_overlong_percent_encoded_callback_line_is_redacted(
            self, spool, caplog):
        """Encoding-bypass red case: an overlong request line to a
        percent-encoded ``/%63allback`` path (``%63`` = 'c', decodes to
        /callback for routing) keeps its RAW encoded target in the pre-routing
        ``LineTooLong`` traceback, so a ``/callback``-anchored redactor misses
        it and the code leaks. Redacting by request-target SHAPE closes it: no
        record on ANY logger carries the code, and the ``aiohttp.server`` exc
        field survives redacted (not dropped)."""
        from log_cid import JsonFormatter

        callback_http.install_callback_log_redaction()
        caplog.set_level(logging.DEBUG)
        app = _build_app(middlewares=[cid_middleware])
        async with _client(app, access_log=True) as client:
            try:
                await _get(
                    client,
                    "/%63allback/x?code=SECRETVALUE&pad=" + "p" * 9000)
            except Exception:  # noqa: BLE001 — the connection is dropped
                pass
            await asyncio.sleep(0.05)
        server_records = [r for r in caplog.records
                          if r.name == "aiohttp.server"]
        assert server_records, \
            "the parser error must have been logged (else the test is moot)"
        formatter = logging.Formatter()
        for record in caplog.records:
            rendered = formatter.format(record)   # includes the traceback
            assert "SECRETVALUE" not in rendered, record.name
        # The exc field is still present under the production JSON formatter,
        # redacted rather than dropped.
        with_exc = []
        for record in server_records:
            payload = json.loads(JsonFormatter().format(record))
            assert "SECRETVALUE" not in json.dumps(payload)
            if "exc" in payload:
                with_exc.append(payload)
        assert with_exc, "the LineTooLong record must keep an exc field"
        assert any("<redacted>" in p["exc"] for p in with_exc)

    async def test_no_log_line_carries_the_state_hash(self, spool, caplog):
        """INV-CB-006 names state, hash values and query content together."""
        callback_spool.mint(_plugin_dir(spool), STATE)
        caplog.set_level(logging.DEBUG)
        app = _build_app()
        async with _client(app) as client:
            await _get(client, f"/callback/{EFFECTIVE}?code=x&state={STATE}")
            await _get(client, f"/callback/{EFFECTIVE}?code=x&state={STATE}")
        digest = callback_spool.state_hash(STATE)
        for record in caplog.records:
            assert digest not in record.getMessage()

    async def test_handler_fault_logs_no_request_data(self, spool, caplog):
        secret_state = "Q" * 22

        def _explode(name):
            raise RuntimeError(f"boom for {name} state={secret_state}")

        caplog.set_level(logging.DEBUG)
        app = _build_app(registry=SimpleNamespace(get_callback=_explode),
                         middlewares=[cid_middleware])
        # access_log=True installs the REAL access logger; without it aiohttp
        # falls back to its own, which logs the full request target (that
        # default is exactly what the middleware change exists to replace).
        async with _client(app, access_log=True) as client:
            await _get(client,
                       f"/callback/{EFFECTIVE}?code=SECRETVALUE"
                       f"&state={secret_state}")
            await asyncio.sleep(0.05)
        assert caplog.records
        for record in caplog.records:
            rendered = f"{record.getMessage()} {record.exc_text or ''}"
            assert "SECRETVALUE" not in rendered, record.name
            assert secret_state not in rendered, record.name
        assert any("handler fault" in r.getMessage() for r in caplog.records)

    async def test_unknown_name_logs_the_sentinel_not_the_name(
            self, spool, caplog):
        caplog.set_level(logging.INFO)
        app = _build_app()
        async with _client(app) as client:
            await _get(client, "/callback/plg-attacker--probe?state=" + STATE)
        lines = [r.getMessage() for r in caplog.records
                 if r.name == callback_http.logger.name]
        assert lines
        assert any("unknown_name" in line for line in lines)
        assert any(callback_http.UNKNOWN_SENTINEL in line for line in lines)
        for line in lines:
            assert "plg-attacker--probe" not in line

    async def test_routed_outcomes_log_the_effective_name(self, spool, caplog):
        callback_spool.mint(_plugin_dir(spool), STATE)
        caplog.set_level(logging.INFO)
        app = _build_app()
        async with _client(app) as client:
            await _get(client, f"/callback/{EFFECTIVE}?state={STATE}")
        lines = [r.getMessage() for r in caplog.records
                 if r.name == callback_http.logger.name]
        assert any("outcome=ok" in line and EFFECTIVE in line
                   for line in lines)

    async def test_reason_enum_is_the_spec_set(self):
        assert callback_http.REASONS == frozenset({
            "ok", "unknown_name", "bad_state", "no_pending", "expired",
            "result_exists", "write_failed"})

    @pytest.mark.parametrize("raw,reason", [
        ("state=" + "n" * 22, "no_pending"),
        ("state=short", "bad_state"),
    ])
    async def test_refusal_reasons(self, spool, caplog, raw, reason):
        caplog.set_level(logging.INFO)
        app = _build_app()
        async with _client(app) as client:
            await _get(client, f"/callback/{EFFECTIVE}?{raw}")
        lines = [r.getMessage() for r in caplog.records
                 if r.name == callback_http.logger.name]
        assert any(f"outcome={reason}" in line for line in lines)

    async def test_expired_is_collapsed_into_no_pending(self, spool, caplog):
        """Documented behaviour: ``CallbackSpool.claim`` refuses expired and
        never-minted identically (it must not differentiate), so the handler
        cannot log ``expired`` — the constant stays in the enum for the
        recovery/sweep vocabulary."""
        expired = "e" * 22
        minted = callback_spool.mint(_plugin_dir(spool), expired)
        old = time.time() - callback_spool.PENDING_TTL_S - 60
        os.utime(minted, (old, old))
        caplog.set_level(logging.INFO)
        app = _build_app()
        async with _client(app) as client:
            await _get(client, f"/callback/{EFFECTIVE}?state={expired}")
        lines = [r.getMessage() for r in caplog.records
                 if r.name == callback_http.logger.name]
        assert any("outcome=no_pending" in line for line in lines)


# ---------------------------------------------------------------------------
# sampler
# ---------------------------------------------------------------------------


class TestSampler:
    def test_env_default(self, monkeypatch):
        monkeypatch.delenv(callback_http.RATE_ENV, raising=False)
        assert callback_http._rate_per_min() == callback_http.DEFAULT_RATE_PER_MIN

    @pytest.mark.parametrize("raw", ["0", "-1", "-1000", "", "abc", "12.5"])
    def test_env_clamped_to_default(self, monkeypatch, raw):
        monkeypatch.setenv(callback_http.RATE_ENV, raw)
        assert callback_http._rate_per_min() == callback_http.DEFAULT_RATE_PER_MIN

    def test_env_honoured(self, monkeypatch):
        monkeypatch.setenv(callback_http.RATE_ENV, "5")
        assert callback_http._rate_per_min() == 5

    def test_bucket_is_never_disabled(self, monkeypatch):
        """RateLimiter treats capacity 0 as 'no limit' — the clamp is what
        keeps the sampler an actual sampler."""
        monkeypatch.setenv(callback_http.RATE_ENV, "0")
        sampler = callback_http.OutcomeSampler(now=_Clock())
        allowed = sum(1 for _ in range(200) if sampler.should_log("k")[0])
        assert allowed == callback_http.DEFAULT_RATE_PER_MIN

    def test_suppressed_summary_on_reopen(self):
        clock = _Clock()
        sampler = callback_http.OutcomeSampler(capacity=2, now=clock)
        assert sampler.should_log("k") == (True, 0)
        assert sampler.should_log("k") == (True, 0)
        assert sampler.should_log("k") == (False, 0)
        assert sampler.should_log("k") == (False, 0)
        clock.t += 120
        assert sampler.should_log("k") == (True, 2)
        assert sampler.should_log("k") == (True, 0)

    def test_keys_are_independent(self):
        clock = _Clock()
        sampler = callback_http.OutcomeSampler(capacity=1, now=clock)
        assert sampler.should_log("a")[0] is True
        assert sampler.should_log("a")[0] is False
        assert sampler.should_log("b")[0] is True

    async def test_flood_caps_emission_but_not_responses(self, spool, caplog):
        clock = _Clock()
        sampler = callback_http.OutcomeSampler(capacity=60, now=clock)
        caplog.set_level(logging.INFO)
        app = _build_app(sampler=sampler)
        shapes = set()
        async with _client(app) as client:
            for _ in range(61):
                r = await _get(client, f"/callback/{EFFECTIVE}?state=" + "f" * 22)
                shapes.add(_shape(r))
            # One outcome line is emitted per allowed check, capped at 60.
            outcomes = [r for r in caplog.records
                        if r.name == callback_http.logger.name
                        and "outcome=" in r.getMessage()]
            assert len(outcomes) == 60
            assert len(shapes) == 1
            # The bucket reopens a minute later: the next line carries the
            # suppressed count so a flood stays diagnosable.
            clock.t += 120
            await _get(client, f"/callback/{EFFECTIVE}?state=" + "f" * 22)
        summaries = [r.getMessage() for r in caplog.records
                     if r.name == callback_http.logger.name
                     and "suppressed" in r.getMessage()]
        assert summaries and "1" in summaries[-1]

    async def test_unknown_names_share_one_bucket(self, spool, caplog):
        """Bounded cardinality: an attacker cycling names cannot mint a bucket
        per probe."""
        clock = _Clock()
        sampler = callback_http.OutcomeSampler(capacity=3, now=clock)
        caplog.set_level(logging.INFO)
        app = _build_app(sampler=sampler)
        async with _client(app) as client:
            for i in range(20):
                await _get(client, f"/callback/plg-probe--{i}")
        outcomes = [r for r in caplog.records
                    if r.name == callback_http.logger.name
                    and "outcome=unknown_name" in r.getMessage()]
        assert len(outcomes) == 3


# ---------------------------------------------------------------------------
# method handling (documented as NOT covered by INV-CB-005)
# ---------------------------------------------------------------------------


class TestNonGet:
    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
    async def test_non_get_is_the_framework_405(self, spool, method):
        """Pinned as CURRENT behaviour, not as an invariant: INV-CB-005 scopes
        itself to syntactically-accepted GETs and documents method handling as
        not covered. aiohttp's router answers 405 before the handler exists,
        so a prober can tell 'route present' from 'route absent' by method —
        the same signal the wildcard's existence already gives."""
        app = _build_app()
        async with _client(app) as client:
            r = await client.request(
                method, URL(f"/callback/{EFFECTIVE}?state={STATE}",
                            encoded=True), allow_redirects=False)
        assert r.status == 405

    async def test_head_is_handled_exactly_like_get(self, spool):
        """``add_get`` registers HEAD too (aiohttp's ``allow_head`` default),
        so a link-preview fetch takes the same path: it consumes the state
        AND publishes the result, which is the better failure mode — the
        consumer still receives the code, and the browser's own GET then gets
        the same neutral 303 as any replay. Pinned so the behaviour is a
        decision rather than a default nobody looked at."""
        callback_spool.mint(_plugin_dir(spool), STATE)
        app = _build_app()
        async with _client(app) as client:
            head = await client.request(
                "HEAD", URL(f"/callback/{EFFECTIVE}?code=abc&state={STATE}",
                            encoded=True), allow_redirects=False)
            get = await _get(client, f"/callback/{EFFECTIVE}?state={STATE}")
        assert head.status == 303
        assert head.headers["Location"] == "/callback/done"
        for header, value in callback_http.SECURITY_HEADERS.items():
            assert head.headers[header] == value
        record = json.loads(_result_path(spool, STATE).read_text())
        assert record["raw_query"] == f"code=abc&state={STATE}"
        # The browser's GET arrives second and loses like any replay.
        assert get.status == 303
        assert len(list(_results_dir(spool).iterdir())) == 1

    async def test_overlong_request_line_is_the_framework_400(self, spool):
        """Also pinned as CURRENT behaviour, not an invariant: aiohttp's
        parser rejects a request line over 8190 bytes before the application
        sees it, so that 400 is one of INV-CB-005's documented not-covered
        residuals (a rejection in FRONT of casa, like NPM's). The handler's
        own cap sits well below it, so a query casa can read is always
        answered neutrally."""
        app = _build_app()
        async with _client(app) as client:
            r = await _get(
                client, f"/callback/{EFFECTIVE}?state={STATE}&pad="
                + "p" * 9000)
        assert r.status == 400
        assert list(_results_dir(spool).iterdir()) == []

    async def test_405_does_not_touch_the_spool(self, spool):
        callback_spool.mint(_plugin_dir(spool), STATE)
        before = _pending_path(spool, STATE).read_bytes()
        app = _build_app()
        async with _client(app) as client:
            await client.post(URL(f"/callback/{EFFECTIVE}?state={STATE}",
                                  encoded=True))
        assert _pending_path(spool, STATE).read_bytes() == before
        assert list(_results_dir(spool).iterdir()) == []


# ---------------------------------------------------------------------------
# overlay contract
# ---------------------------------------------------------------------------


class TestOverlayContract:
    @pytest.mark.parametrize("entry", [
        None, {}, {"plugin": ""}, {"plugin": None}, {"plugin": 42},
        {"declared": "auth"}, "not-a-dict",
    ])
    async def test_malformed_overlay_entry_is_neutral(self, spool, entry):
        callback_spool.mint(_plugin_dir(spool), STATE)
        app = _build_app(registry=_registry({EFFECTIVE: entry}))
        async with _client(app) as client:
            r = await _get(client, f"/callback/{EFFECTIVE}?state={STATE}")
        assert r.status == 303
        assert list(_results_dir(spool).iterdir()) == []

    async def test_effective_falls_back_to_the_routed_name(self, spool):
        callback_spool.mint(_plugin_dir(spool), STATE)
        app = _build_app(registry=_registry({
            EFFECTIVE: {"plugin": PLUGIN, "declared": "auth"}}))
        async with _client(app) as client:
            await _get(client, f"/callback/{EFFECTIVE}?state={STATE}")
        record = json.loads(_result_path(spool, STATE).read_text())
        assert record["effective"] == EFFECTIVE

    async def test_casa_core_registers_done_before_the_wildcard(self):
        """Static guard on the production wiring: aiohttp matches routes in
        registration order, so a wildcard registered first would swallow
        ``/callback/done`` and answer the page request with a 303 back to
        itself — an infinite redirect for every operator who completes an
        authorization."""
        src = (Path(__file__).resolve().parent.parent / "casa" / "rootfs"
               / "opt" / "casa" / "casa_core.py").read_text(encoding="utf-8")
        done = src.index('add_get("/callback/done"')
        wildcard = src.index('add_get("/callback/{name}"')
        assert done < wildcard, (
            "casa_core.py must register /callback/done BEFORE the "
            "/callback/{name} wildcard")

    async def test_unsafe_plugin_name_is_neutral(self, spool):
        """``Claim`` raises ValueError on unsafe names — the handler treats
        (OSError, ValueError) as a neutral refusal rather than a 500."""
        app = _build_app(registry=_registry({
            EFFECTIVE: {"plugin": "../escape", "declared": "auth",
                        "effective": EFFECTIVE}}))
        async with _client(app) as client:
            r = await _get(client, f"/callback/{EFFECTIVE}?state={STATE}")
        assert r.status == 303
        assert r.headers["Location"] == "/callback/done"
