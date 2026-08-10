"""send_media tool — origin targeting, guards, classification, cleanup (v0.73.0)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent as agent_mod
import plugin_outbox
import tools

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

PDF = b"%PDF-1.7\n" + b"x" * 100
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 100


def _payload(res):
    return json.loads(res["content"][0]["text"])


@pytest.fixture
def wired(tmp_path):
    """Init a real outbox + a mocked channel manager whose telegram channel has
    an AsyncMock send_media; set a telegram origin. Tear everything down."""
    ob = plugin_outbox.init_outbox(str(tmp_path / "plugin-outbox"))
    ch = MagicMock()
    ch.send_media = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = ch
    tools.init_tools(
        channel_manager=cm, bus=MagicMock(), specialist_registry=MagicMock(),
        mcp_registry=MagicMock(), trigger_registry=MagicMock(),
        engagement_registry=MagicMock(),
    )
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram",
                                      "chat_id": 1197017861})
    try:
        yield ob, ch
    finally:
        agent_mod.origin_var.reset(token)
        ob.close()
        plugin_outbox._OUTBOX = None


def _drop(outbox, name, data):
    p = os.path.join(outbox._root_realpath, name)
    with open(p, "wb") as fh:
        fh.write(data)
    return p


async def test_happy_path_document(wired):
    ob, ch = wired
    path = _drop(ob, "invoice-2026-07.pdf", PDF)
    res = await tools.send_media.handler({"path": path, "kind": "document",
                                          "caption": "Here it is"})
    body = _payload(res)
    assert body["status"] == "ok"
    ch.send_media.assert_awaited_once()
    args, kwargs = ch.send_media.await_args
    assert args[0] == PDF                       # bytes captured & handed to channel
    assert args[1] == "document"
    assert kwargs["context"]["chat_id"] == 1197017861
    assert kwargs["caption"] == "Here it is"
    assert os.listdir(ob._claims_realpath) == []
    # producer-contract: NO file bytes leak into the result (only bounded fields)
    text = res["content"][0]["text"]
    assert "%PDF" not in text
    assert set(body) <= {"status", "kind_error", "kind", "filename", "summary",
                         "message", "cleanup_warning"}
    assert body["filename"] == "invoice-2026-07.pdf"


async def test_magic_mismatch_pdf_as_photo_no_send(wired):
    ob, ch = wired
    path = _drop(ob, "x.jpg", PDF)              # PDF bytes, declared photo, .jpg name
    res = await tools.send_media.handler({"path": path, "kind": "photo"})
    body = _payload(res)
    assert body["status"] == "error"
    assert body["kind_error"] == "magic_mismatch"
    ch.send_media.assert_not_awaited()
    assert os.listdir(ob._claims_realpath) == []   # claim cleaned even on guard fail


async def test_outside_outbox_no_send(wired, tmp_path):
    ob, ch = wired
    secret = tmp_path / "webhook_secret"
    secret.write_bytes(b"%PDF-secret")
    res = await tools.send_media.handler({"path": str(secret), "kind": "document"})
    body = _payload(res)
    assert body["kind_error"] == "outside_outbox"
    ch.send_media.assert_not_awaited()


@pytest.mark.parametrize("origin,expected", [
    (None, "no_origin"),                                  # empty snapshot -> no_origin
    ({"role": "x"}, "invalid_origin"),                    # has content, no telegram channel
    ({"channel": "voice", "chat_id": 5}, "invalid_origin"),
    ({"channel": "telegram", "chat_id": 0}, "invalid_origin"),
    ({"channel": "telegram", "chat_id": "not-num"}, "invalid_origin"),
])
async def test_origin_refusals(wired, origin, expected):
    ob, ch = wired
    path = _drop(ob, "a.pdf", PDF)
    token = agent_mod.origin_var.set(origin)
    try:
        res = await tools.send_media.handler({"path": path, "kind": "document"})
    finally:
        agent_mod.origin_var.reset(token)
    assert _payload(res)["kind_error"] == expected
    ch.send_media.assert_not_awaited()


async def test_engagement_origin_preferred(wired):
    ob, ch = wired
    path = _drop(ob, "b.pdf", PDF)
    # origin_var says chat 1197017861, but the active engagement says chat 42.
    eng = MagicMock()
    eng.origin = {"channel": "telegram", "chat_id": 42}
    eng.status = "active"
    eng.context_rebuild_pending = False
    etok = tools.engagement_var.set(eng)
    try:
        await tools.send_media.handler({"path": path, "kind": "document"})
    finally:
        tools.engagement_var.reset(etok)
    assert ch.send_media.await_args.kwargs["context"]["chat_id"] == 42


# ---------------------------------------------------------------------------
# Per-engagement private outbox (containment stage 2, Task 11).
# ---------------------------------------------------------------------------


def _eng_with_uid(uid, chat_id=42):
    eng = MagicMock()
    eng.origin = {"channel": "telegram", "chat_id": chat_id}
    eng.allocated_uid = uid
    eng.status = "active"
    eng.context_rebuild_pending = False
    return eng


async def test_send_media_uses_engagement_uid_private_outbox(wired, tmp_path):
    """The claim comes from the AUTHENTICATED record's own private outbox,
    not the shared one — even though the `path` argument's parent dir is
    literally the private dir (a producer would return exactly this)."""
    _ob, ch = wired
    priv = plugin_outbox.PluginOutbox(str(tmp_path / "eng-42"))
    try:
        path = _drop(priv, "report.pdf", PDF)
        with patch.object(
            plugin_outbox, "get_engagement_outbox", return_value=priv,
        ) as fake_get:
            etok = tools.engagement_var.set(_eng_with_uid(200042))
            try:
                res = await tools.send_media.handler(
                    {"path": path, "kind": "document"})
            finally:
                tools.engagement_var.reset(etok)
        fake_get.assert_called_once_with(200042)
        body = _payload(res)
        assert body["status"] == "ok"
        ch.send_media.assert_awaited_once()
        assert os.listdir(priv._claims_realpath) == []   # claim cleaned up
    finally:
        priv.close()


async def test_send_media_ignores_path_pointing_at_another_engagements_outbox(
    wired, tmp_path,
):
    """A submitted `path` naming a DIFFERENT engagement's private outbox
    directory must be refused, not honored — the dir to claim from is
    whichever `get_engagement_outbox(this_record's_uid)` returns, never
    derived from the argument."""
    _ob, ch = wired
    mine = plugin_outbox.PluginOutbox(str(tmp_path / "eng-mine"))
    theirs = plugin_outbox.PluginOutbox(str(tmp_path / "eng-theirs"))
    try:
        secret_path = _drop(theirs, "b-secret.pdf", PDF)
        with patch.object(
            plugin_outbox, "get_engagement_outbox", return_value=mine,
        ):
            etok = tools.engagement_var.set(_eng_with_uid(200043))
            try:
                res = await tools.send_media.handler(
                    {"path": secret_path, "kind": "document"})
            finally:
                tools.engagement_var.reset(etok)
        body = _payload(res)
        assert body["status"] == "error"
        assert body["kind_error"] == "outside_outbox"
        ch.send_media.assert_not_awaited()
        # B's file was never touched — still sitting where B's producer left it.
        assert os.path.exists(secret_path)
    finally:
        mine.close()
        theirs.close()


async def test_send_media_refuses_symlink_planted_in_own_private_outbox(
    wired, tmp_path,
):
    """A symlink sitting in the caller's OWN private outbox (e.g. pointing at
    a sibling engagement's `.mcp.json`) is refused by the reused hardened
    capture — single-hard-link + `O_NOFOLLOW` — the same guard the shared
    outbox has always had, now covering the private one too."""
    _ob, ch = wired
    mine = plugin_outbox.PluginOutbox(str(tmp_path / "eng-mine2"))
    target = tmp_path / "victim-mcp.json"
    target.write_bytes(b"super-secret-token")
    link = Path(mine._root_realpath) / "innocuous.pdf"
    link.symlink_to(target)
    try:
        with patch.object(
            plugin_outbox, "get_engagement_outbox", return_value=mine,
        ):
            etok = tools.engagement_var.set(_eng_with_uid(200044))
            try:
                res = await tools.send_media.handler(
                    {"path": str(link), "kind": "document"})
            finally:
                tools.engagement_var.reset(etok)
        body = _payload(res)
        assert body["status"] == "error"
        assert body["kind_error"] == "not_regular"
        ch.send_media.assert_not_awaited()
        assert target.read_bytes() == b"super-secret-token"  # never exfiltrated
    finally:
        mine.close()


@pytest.mark.parametrize("args,expected", [
    ({"kind": "document"}, "invalid_arguments"),            # missing path
    ({"path": "/x", "kind": "movie"}, "invalid_arguments"),  # unknown kind
    ({"path": "/x", "kind": []}, "invalid_arguments"),       # non-str kind (no TypeError)
    ({"path": "", "kind": "document"}, "invalid_arguments"),  # empty path
    ({"path": "/x", "kind": "document", "caption": 5}, "invalid_arguments"),
    ({"path": "/x", "kind": "document", "filename": 5}, "invalid_arguments"),
])
async def test_invalid_arguments(wired, args, expected):
    ob, ch = wired
    res = await tools.send_media.handler(args)
    assert _payload(res)["kind_error"] == expected
    ch.send_media.assert_not_awaited()


async def test_bad_name_wrong_extension(wired):
    ob, ch = wired
    path = _drop(ob, "invoice.txt", PDF)        # .txt not in document allowlist
    res = await tools.send_media.handler({"path": path, "kind": "document"})
    assert _payload(res)["kind_error"] == "bad_name"
    ch.send_media.assert_not_awaited()
    assert os.listdir(ob._claims_realpath) == []   # claimed (valid basename) then cleaned


async def test_caption_truncated_to_1024(wired):
    ob, ch = wired
    path = _drop(ob, "c.pdf", PDF)
    await tools.send_media.handler({"path": path, "kind": "document",
                                    "caption": "z" * 5000})
    assert len(ch.send_media.await_args.kwargs["caption"]) == 1024


@pytest.mark.parametrize("exc,expected", [
    ("NotImplementedError", "unsupported_channel"),
    ("BadRequest", "rejected"),
    ("Forbidden", "rejected"),
    ("TimedOut", "delivery_uncertain"),
    ("NetworkError", "delivery_uncertain"),
    ("RetryAfter", "delivery_uncertain"),
    ("TelegramError", "delivery_uncertain"),   # generic base -> delivery_uncertain
    ("RuntimeError", "channel_unavailable"),
])
async def test_send_error_classification_and_cleanup(wired, exc, expected):
    ob, ch = wired
    from telegram.error import (
        BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut,
    )
    exc_map = {
        "NotImplementedError": NotImplementedError("x"),
        "BadRequest": BadRequest("x"), "Forbidden": Forbidden("x"),
        "TimedOut": TimedOut(), "NetworkError": NetworkError("x"),
        "RetryAfter": RetryAfter("x"), "TelegramError": TelegramError("x"),
        "RuntimeError": RuntimeError("x"),
    }
    ch.send_media.side_effect = exc_map[exc]
    path = _drop(ob, "e.pdf", PDF)
    res = await tools.send_media.handler({"path": path, "kind": "document"})
    assert _payload(res)["kind_error"] == expected
    assert os.listdir(ob._claims_realpath) == []   # claim removed on EVERY outcome


async def test_channel_unavailable_when_manager_missing(tmp_path):
    ob = plugin_outbox.init_outbox(str(tmp_path / "ob2"))
    tools.init_tools(channel_manager=None, bus=MagicMock(),
                     specialist_registry=MagicMock(), mcp_registry=MagicMock(),
                     trigger_registry=MagicMock(), engagement_registry=MagicMock())
    token = agent_mod.origin_var.set({"channel": "telegram", "chat_id": 5})
    try:
        res = await tools.send_media.handler({"path": "/x", "kind": "document"})
        assert _payload(res)["kind_error"] == "channel_unavailable"
    finally:
        agent_mod.origin_var.reset(token)
        ob.close()
        plugin_outbox._OUTBOX = None


async def test_internal_error_wraps_unexpected(wired, monkeypatch):
    ob, ch = wired
    # Force an unexpected exception BEFORE the claim (get_outbox) -> internal_error,
    # nothing claimed.
    monkeypatch.setattr(plugin_outbox, "get_outbox",
                        MagicMock(side_effect=ValueError("boom")))
    path = _drop(ob, "i.pdf", PDF)
    res = await tools.send_media.handler({"path": path, "kind": "document"})
    assert _payload(res)["kind_error"] == "internal_error"


async def test_uppercase_extension_accepted(wired):
    ob, ch = wired
    path = _drop(ob, "IMG.JPG", JPEG)                  # upper-case .JPG, photo bytes
    res = await tools.send_media.handler({"path": path, "kind": "photo"})
    assert _payload(res)["status"] == "ok"
    assert ch.send_media.await_args.args[1] == "photo"
    assert ch.send_media.await_args.args[0] == JPEG


async def test_explicit_empty_filename_is_bad_name(wired):
    ob, ch = wired
    path = _drop(ob, "real.pdf", PDF)
    res = await tools.send_media.handler(
        {"path": path, "kind": "document", "filename": ""})
    assert _payload(res)["kind_error"] == "bad_name"
    ch.send_media.assert_not_awaited()
    assert os.listdir(ob._claims_realpath) == []       # claimed, then cleaned


async def test_directory_claim_via_tool_not_regular_and_cleaned(wired):
    ob, ch = wired
    d = os.path.join(ob._root_realpath, "d.pdf")
    os.mkdir(d)
    with open(os.path.join(d, "inner"), "wb") as fh:
        fh.write(b"z")
    res = await tools.send_media.handler({"path": d, "kind": "document"})
    assert _payload(res)["kind_error"] == "not_regular"
    ch.send_media.assert_not_awaited()
    assert os.listdir(ob._claims_realpath) == []       # dir-claim rmtree'd


async def test_cleanup_failure_surfaces_warning_on_ok(wired, monkeypatch):
    ob, ch = wired
    path = _drop(ob, "cw.pdf", PDF)
    monkeypatch.setattr(ob, "remove_claim", MagicMock(side_effect=OSError("boom")))
    body = _payload(await tools.send_media.handler({"path": path, "kind": "document"}))
    assert body["status"] == "ok"
    assert "cleanup_warning" in body
    ch.send_media.assert_awaited_once()


async def test_unexpected_post_claim_exception_cleans_and_internal_errors(wired, monkeypatch):
    ob, ch = wired
    path = _drop(ob, "u.pdf", PDF)
    # A non-OutboxError from capture propagates past the inner except, the finally
    # still removes the claim, and the outer wrap yields internal_error.
    monkeypatch.setattr(ob, "capture", MagicMock(side_effect=ValueError("boom")))
    res = await tools.send_media.handler({"path": path, "kind": "document"})
    assert _payload(res)["kind_error"] == "internal_error"
    ch.send_media.assert_not_awaited()
    assert os.listdir(ob._claims_realpath) == []       # claim removed despite the crash


async def test_capture_runs_off_loop(wired, monkeypatch):
    import threading
    ob, ch = wired
    path = _drop(ob, "off.pdf", PDF)
    main_ident = threading.get_ident()
    seen: dict = {}
    real_capture = ob.capture

    def spy(name, kind):
        seen["ident"] = threading.get_ident()
        return real_capture(name, kind)

    monkeypatch.setattr(ob, "capture", spy)
    await tools.send_media.handler({"path": path, "kind": "document"})
    assert seen["ident"] != main_ident                 # capture read ran off the loop


async def test_delegated_finance_origin_numeric_string_chat(wired):
    ob, ch = wired
    # A delegated finance turn carries its origin via origin_var (NOT engagement_var)
    # with a numeric-STRING chat_id. The tool must coerce to int and target it.
    token = agent_mod.origin_var.set(
        {"role": "finance", "channel": "telegram", "chat_id": "1197017861"})
    try:
        assert tools.engagement_var.get(None) is None
        path = _drop(ob, "fin.pdf", PDF)
        res = await tools.send_media.handler({"path": path, "kind": "document"})
    finally:
        agent_mod.origin_var.reset(token)
    assert _payload(res)["status"] == "ok"
    assert ch.send_media.await_args.kwargs["context"]["chat_id"] == 1197017861  # coerced


async def test_concurrent_tool_calls_one_sends_one_missing(wired):
    import asyncio as _asyncio
    ob, ch = wired
    path = _drop(ob, "conc.pdf", PDF)
    # Two concurrent tool calls on ONE source: the claim-by-rename lets exactly one
    # win (sends); the other gets `missing`. (Spec §6 / watchlist.)
    r1, r2 = await _asyncio.gather(
        tools.send_media.handler({"path": path, "kind": "document"}),
        tools.send_media.handler({"path": path, "kind": "document"}),
    )
    payloads = [_payload(r1), _payload(r2)]
    oks = [p for p in payloads if p["status"] == "ok"]
    errs = [p for p in payloads if p["status"] == "error"]
    assert len(oks) == 1 and len(errs) == 1
    assert errs[0]["kind_error"] == "missing"
    assert ch.send_media.await_count == 1
    assert os.listdir(ob._claims_realpath) == []       # winner's claim cleaned; loser none
