"""Pinning tests for observability/persistent-state/eval invariants (docs corpus).

Each test names the corpus invariant it pins and records, in its docstring, the
red case that was demonstrated: the code edit that made it fail. A pinning test
never shown red proves nothing.
"""
import logging
import re
import subprocess
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import casa_eval
import topic_ledger
from casa_core_middleware import cid_middleware
from casa_eval.base import Suite, Tester
from log_redact import RedactingFilter
from session_registry import SessionRegistry


async def test_pin_inv_obs_001_header_cid_validated_payload_cid_not():
    """INV-OBS-001: a correlation id arriving on the request header is
    validated to a fixed shape; one supplied inside an invocation payload is
    not.

    Red case demonstrated: making the middleware's header normalisation
    return the raw value fails the shape assertion.
    """
    from casa_core import build_invoke_message

    async def handler(request):
        return web.Response(text=request["cid"])

    app = web.Application(middlewares=[cid_middleware])
    app.router.add_get("/probe", handler)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/probe", headers={"X-Request-Cid": "not-hex!"})
        header_cid = await response.text()

    assert header_cid != "not-hex!"
    assert re.fullmatch(r"[0-9a-f]+", header_cid)

    payload_cid = "not-a-header-cid"
    message = build_invoke_message(
        "assistant", "hello", {"context": {"cid": payload_cid}},
    )
    assert message.context["cid"] == payload_cid


def test_pin_inv_obs_004_rendered_output_redacts_exceptions_and_extras():
    """INV-OBS-004 (replaces a retired predecessor, OBS 002, whose statement
    pinned the pre-#285 gap): what Casa's own formatters render is redacted end to
    end — message and args at the filter, exception text and structured
    extras at the formatter (#285). A handler that is not Casa's own is
    still uncovered.

    Red case demonstrated: dropping _RedactingRenderMixin from
    JsonFormatter (or reverting redact_extras in JsonFormatter.format)
    puts the secret back in the rendered line and fails this test.
    """
    from log_cid import JsonFormatter
    from log_redact import register_secret

    secret = "pin-obs2-exception-secret"  # gitleaks:allow - synthetic fixture; this test exists to prove redaction works
    register_secret(secret)
    try:
        raise RuntimeError(secret)
    except RuntimeError:
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="safe message", args=(), exc_info=sys.exc_info(),
        )
    record.api_key = "pin-obs2-extra"
    RedactingFilter().filter(record)
    rendered = JsonFormatter().format(record)
    assert secret not in rendered
    assert "pin-obs2-extra" not in rendered
    assert "RuntimeError" in rendered  # traceback context survives
    # The boundary that remains: a foreign handler formatting the same
    # record without Casa's formatter still sees the raw exception.
    assert secret in logging.Formatter().formatException(record.exc_info)


async def test_pin_inv_obs_003_healthz_is_fixed_and_reads_nothing():
    """INV-OBS-003: the health endpoint returns a fixed success response
    without consulting any subsystem — or even the request.

    Red case demonstrated: making healthz read a request attribute or vary
    its body fails this test.
    """
    from casa_core import healthz

    class ExplosiveRequest:
        def __getattr__(self, name):
            raise AssertionError(f"healthz read request attribute {name!r}")

    response = await healthz(ExplosiveRequest())
    assert response.status == 200
    assert b"ok" in response.body


def test_pin_inv_state_001_only_whitelist_admitted(tmp_path):
    """INV-STATE-001: only the explicit whitelist of the config root is
    admitted into version control.

    Red case demonstrated: removing the leading `*` ignore rule from the
    whitelist file tracks the unlisted path and fails this test.
    """
    from config_git import init_repo

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "a.yaml").write_text("x")
    (tmp_path / "unlisted").mkdir()
    (tmp_path / "unlisted" / "secret.txt").write_text("x")
    init_repo(str(tmp_path))

    tracked = subprocess.check_output(
        ["git", "-C", str(tmp_path), "ls-files"], text=True,
    ).splitlines()
    assert "agents/a.yaml" in tracked
    assert "unlisted/secret.txt" not in tracked


async def test_pin_inv_state_002_missing_is_empty_corrupt_is_per_loader(tmp_path):
    """INV-STATE-002: a missing state file is a valid empty state; a corrupt
    one is handled per-loader and inconsistently (ledger archives .casabak,
    session registry renames .corrupt).

    Red case demonstrated: making the topic ledger raise on a corrupt file
    instead of archiving fails this test.
    """
    assert await topic_ledger.load(path=str(tmp_path / "missing.json")) == []

    ledger = tmp_path / "ledger.json"
    ledger.write_text("{broken", encoding="utf-8")
    assert await topic_ledger.load(path=str(ledger)) == []
    assert (tmp_path / "ledger.json.casabak").exists()

    sessions = tmp_path / "sessions.json"
    sessions.write_text("{broken", encoding="utf-8")
    assert SessionRegistry(str(sessions)).all_entries() == {}
    assert (tmp_path / "sessions.json.corrupt").exists()


def test_pin_inv_eval_001_shipped_registry_is_empty():
    """INV-EVAL-001: the tester registry ships empty — checked on the
    unmodified registry, not a cleared copy.

    Red case demonstrated: registering a tester in casa_eval's import path
    fails this test.
    """
    assert casa_eval.list_testers() == []
    with pytest.raises(KeyError):
        casa_eval.get_tester("any-tester")


def test_pin_inv_eval_002_sweep_forwards_caller_selected_value():
    """INV-EVAL-002: bank/axis selection for a check comes from the caller;
    the framework forwards it and confines nothing.

    Red case demonstrated: making Tester.sweep substitute its own value for
    the caller's fails this test.
    """
    class CallerBankTester(Tester):
        id = "caller-bank"
        optimization_axes = ["bank"]
        optimization_bounds = {}

        def load_suite(self, path):
            raise AssertionError("not used")

        def run(self, suite, **opts):
            return opts["bank"]

        def recommend_from_sweep(self, reports):
            raise AssertionError("not used")

    suite = Suite(suite_id="s", description="", cases=[])
    assert CallerBankTester().sweep(suite, "bank", ["live-memory"]) == {
        "live-memory": "live-memory",
    }
