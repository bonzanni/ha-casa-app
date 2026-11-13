"""Casa Browser action model (slice 1): the check of one browsing action —
``POST /internal/broker/action-judgement``, the measured rulebooks, the digest
of the bytes received, and the operator's words kept per spec digest."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import sys
import types
from pathlib import Path

import pytest

from sent_prompt import sent_prompt
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

import action_judgement as aj
import operator_calls
import tools as tools_mod
from authz_grants import GrantIdentity
from two_endpoints import CASA_BOT, build_two_endpoints

ARTIFACT = "1" * 64
DIGEST = "sha256:spec-aa"
WORDS = "haal mijn laatste factuur van Voorbeeld Energie op"
PAGE_TEXT = "Factuur oktober 2026 totaal 84,20 voor Jan Voorbeeld"
REASON_TEXT = "the confidential reason line"
ORIGIN = {"role": "assistant", "execution_role": "browser", "channel": "telegram",
          "chat_id": "42", "user_id": 100, "cid": "c", "message_type": "channel_in",
          "source": "telegram", "_operator_turn": True, "_delegation_id": "d-1",
          "delegation_depth": 1, "_handoff_chain": ("assistant", "browser"),
          "_tg_bot": CASA_BOT}

GO = {"verdict": "go", "category": "reading", "reason": "opens the invoice list",
      "look_first": "", "question": None}
QUESTION = {"text": "Pay the October invoice of €84.20?", "confirm_label": "Pay €84.20",
            "extra_options": [], "screenshot": "required", "highlight": ["€84.20"],
            "text_incomplete": False}
ASK = {"verdict": "ask", "category": "payment", "reason": REASON_TEXT,
       "look_first": "", "question": QUESTION}


PNG = b"\x89PNG\r\n\x1a\n"
PICTURES: list = []     # the picture each check of the ``wired`` double was handed


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _packet(**over) -> dict:
    p = {"task": "download the latest invoice",
         "where": {"session": "signed_in", "site": "Voorbeeld Energie"},
         "action": {"kind": "click"}, "target": {"role": "link", "name": "Facturen"},
         "page": {"blocks": [PAGE_TEXT], "controls": []}}
    p.update(over)
    return p


def _canonical(p: dict) -> str:
    return json.dumps(p, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(tools_mod, "_channel_manager", build_two_endpoints().manager,
                        raising=False)
    monkeypatch.setattr(operator_calls, "CALLS", operator_calls.OperatorCalls())
    monkeypatch.setattr(aj, "WORDS", aj.OperatorWords())
    PICTURES.clear()
    seen: list[tuple[str, str]] = []
    replies: list = []

    async def _ask(rulebook, body, picture=None):
        seen.append((rulebook, body))
        PICTURES.append(picture)
        reply = replies.pop(0) if replies else json.dumps(GO)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return await reply()
        return reply

    monkeypatch.setattr(aj, "_ask_check", _ask)
    return seen, replies


def _call(plugin="browser", operator_id=100, client="c1"):
    ident = GrantIdentity(operator_id=operator_id, chat_id=42, enforcement_role="browser",
                          artifact_id=ARTIFACT, engagement_id="", bot_id=CASA_BOT)
    return operator_calls.CALLS.open(
        client_id=client, tool_use_id="tu-" + client, tool_name="x", role="browser",
        plugin=plugin, identity=ident, origin={**ORIGIN, "_operator_words": WORDS})


async def _post(body):
    handler = operator_calls.build_action_judgement_handler()
    req = make_mocked_request("POST", "/internal/broker/action-judgement")

    async def _json():
        return body
    req.json = _json
    return json.loads((await handler(req)).body)


# -- the digest binds the bytes received (INV-PLUG-055) ------------------------------

async def test_digest_is_of_the_bytes_received(wired):
    seen, _ = wired
    call = _call()
    a = '{"action":{"kind":"click"},"page":{"blocks":["Totaal € 84,20"]}}'
    b = '{"page":{"blocks":["Totaal € 84,20"]},"action":{"kind":"click"}}'
    out_a = await aj.judge(call, "action", a, DIGEST)
    out_b = await aj.judge(call, "action", b, DIGEST)
    assert out_a["packet_sha256"] == hashlib.sha256(a.encode("utf-8")).hexdigest()
    assert out_b["packet_sha256"] == hashlib.sha256(b.encode("utf-8")).hexdigest()
    assert out_a["packet_sha256"] != out_b["packet_sha256"]       # never re-serialised
    assert out_a["verdict"] == GO and isinstance(out_a["latency_ms"], int)
    # what the model judged is exactly what was hashed
    assert json.loads(seen[0][1])["packet"] == json.loads(a)
    # the route answers the same digest for the same string
    assert (await _post({"client": "c1", "kind": "action", "packet": a,
                         "spec_digest": DIGEST}))["packet_sha256"] == out_a["packet_sha256"]


async def test_a_repeated_key_is_not_a_packet(wired):
    """A key given twice would let the model read one value while the digest
    covers both: no such string is judged."""
    seen, _ = wired
    out = await aj.judge(_call(), "action", '{"a":{"x":1},"a":{"x":2}}', DIGEST)
    assert out == {"error": "bad_packet"} and seen == []


async def test_model_body_is_the_measured_shape(wired):
    seen, replies = wired
    call = _call()
    aj.WORDS.record(plugin="browser", bot_id=CASA_BOT, operator_id=100, spec_digest=DIGEST, words=WORDS)
    p = json.loads(_canonical(_packet()))           # the order the app sends
    await aj.judge(call, "action", _canonical(p), DIGEST)
    rulebook, body = seen[-1]
    assert rulebook == aj.RULEBOOKS["action"]
    assert body == json.dumps({"context": {"operator_words": WORDS}, "packet": p},
                              ensure_ascii=False)
    replies.append('{"verdict": "same", "reason": "", "text_incomplete": false}')
    rj = json.loads(_canonical({"approved_packet": p, "current_packet": p, "difference": [],
                                "destination_changed": False,
                                "approved_question": QUESTION}))
    out = await aj.judge(call, "rejudge", _canonical(rj), DIGEST)
    rulebook, body = seen[-1]
    assert rulebook == aj.RULEBOOKS["rejudge"]
    assert body == json.dumps(rj, ensure_ascii=False)              # the bare packet, no words
    assert out["verdict"] == {"verdict": "same", "reason": "", "text_incomplete": False}


async def test_words_attach_only_by_digest_plugin_and_operator(wired):
    seen, _ = wired
    aj.WORDS.record(plugin="browser", bot_id=CASA_BOT, operator_id=100, spec_digest=DIGEST, words=WORDS)
    text = _canonical(_packet())

    async def words_for(call, digest):
        await aj.judge(call, "action", text, digest)
        return json.loads(seen[-1][1])["context"]["operator_words"]

    assert await words_for(_call(), DIGEST) == WORDS
    assert await words_for(_call(), "sha256:other") is None
    assert await words_for(_call(), None) is None
    assert await words_for(_call(plugin="bank", client="c2"), DIGEST) is None
    assert await words_for(_call(operator_id=999, client="c3"), DIGEST) is None


def test_words_are_bounded_clipped_and_expire():
    now = [0.0]
    store = aj.OperatorWords(now=lambda: now[0])
    store.record(plugin="p", bot_id=CASA_BOT, operator_id=1, spec_digest="d", words="x" * 9000)
    assert store.get(plugin="p", bot_id=CASA_BOT, operator_id=1, spec_digest="d") == "x" * aj.MAX_WORDS_CHARS
    now[0] = aj.WORDS_KEEP_S
    assert store.get(plugin="p", bot_id=CASA_BOT, operator_id=1, spec_digest="d") is None
    for i in range(aj.WORDS_MAX_ENTRIES + 1):
        store.record(plugin="p", bot_id=CASA_BOT, operator_id=1, spec_digest=f"d{i}", words="w")
    assert store.get(plugin="p", bot_id=CASA_BOT, operator_id=1, spec_digest="d0") is None   # oldest out
    assert store.get(plugin="p", bot_id=CASA_BOT, operator_id=1,
                     spec_digest=f"d{aj.WORDS_MAX_ENTRIES}") == "w"


# -- no verdict is never go (F1, INV-PLUG-055) ----------------------------------------

def _ask_with(**q) -> str:
    return json.dumps({**ASK, "question": {**QUESTION, **q}})


async def _hang():
    await asyncio.Event().wait()


NO_VERDICT = [
    pytest.param("timeout", id="timeout"),
    pytest.param(RuntimeError("cli gone"), id="exception"),
    pytest.param(aj.CheckFailed("assistant_error"), id="refusal-or-api-error"),
    pytest.param("I can't help with deciding this.", id="refusal-text"),
    pytest.param("{}", id="empty-object"),
    pytest.param('{"verdict": "allow", "category": "reading"}', id="wrong-verdict"),
    pytest.param('{"verdict": "same", "text_incomplete": false}', id="rejudge-verdict"),
    pytest.param('{"verdict": "go", "category": "browsing"}', id="wrong-category"),
    pytest.param(json.dumps({**ASK, "question": None}), id="ask-without-question"),
    pytest.param(_ask_with(text_incomplete="no"), id="ask-without-bool"),
    pytest.param(_ask_with(text="x" * 601), id="text-too-long"),
    pytest.param(_ask_with(confirm_label="x" * 33), id="label-too-long"),
    pytest.param(_ask_with(extra_options=["a", "b", "c", "d"]), id="four-extra-options"),
    pytest.param(_ask_with(extra_options=["x" * 61]), id="extra-option-too-long"),
    pytest.param(_ask_with(highlight=["h"] * 7), id="seven-highlights"),
    pytest.param(_ask_with(highlight=["x" * 201]), id="highlight-too-long"),
    pytest.param(_ask_with(screenshot="maybe"), id="bad-screenshot"),
    pytest.param(json.dumps({**GO, "reason": "x" * 601}), id="reason-too-long"),
    pytest.param(json.dumps({**GO, "look_first": 7}), id="look-first-not-text"),
]


@pytest.mark.parametrize("reply", NO_VERDICT)
async def test_no_verdict_is_never_go(wired, monkeypatch, reply):
    _, replies = wired
    if reply == "timeout":
        monkeypatch.setattr(aj, "CHECK_TIMEOUT_S", 0.01)
        reply = _hang
    replies.append(reply)
    out = await aj.judge(_call(), "action", _canonical(_packet()), DIGEST)
    assert out == {"error": "check_unavailable"}


@pytest.mark.parametrize("reply", [
    '{"verdict": "go", "category": "reading"}',
    '{"verdict": "changed", "reason": "new fee"}',
    '{"verdict": "same", "text_incomplete": "false"}',
    '{"verdict": "same", "text_incomplete": false, "reason": "' + "x" * 601 + '"}',
])
async def test_no_rejudgement_verdict_is_never_same(wired, reply):
    _, replies = wired
    replies.append(reply)
    out = await aj.judge(_call(), "rejudge", _canonical(_packet()), None)
    assert out == {"error": "check_unavailable"}


async def test_an_error_or_refusal_message_is_no_verdict(monkeypatch):
    """The CLI reports a fault or a refusal as an assistant message with its
    own text; a verdict-shaped text in it is still no verdict."""
    import claude_agent_sdk as real

    def _fake(messages):
        fake = types.ModuleType("claude_agent_sdk")
        fake.ClaudeAgentOptions = real.ClaudeAgentOptions
        fake.AssistantMessage = real.AssistantMessage
        fake.ResultMessage = real.ResultMessage

        async def query(*, prompt, options):
            for m in messages:
                yield m
        fake.query = query
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)

    text = [real.TextBlock(text=json.dumps(GO))]

    def result(**over):
        facts = {"subtype": "success", "duration_ms": 1, "duration_api_ms": 1,
                 "is_error": False, "num_turns": 1, "session_id": "s",
                 "stop_reason": "end_turn", "terminal_reason": "completed", **over}
        return real.ResultMessage(**facts)

    # each followed by a completed terminal result, so only the message's own
    # error or refusal can make it no verdict
    for faulted in ({"error": "unknown"}, {"stop_reason": "refusal"}):
        _fake([real.AssistantMessage(content=text, model="m", **faulted), result()])
        with pytest.raises(aj.CheckFailed, match="assistant_error"):
            await aj._ask_check("rb", "{}")

    for unfinished in ({"is_error": True}, {"stop_reason": "refusal"},
                       {"stop_reason": "max_tokens"}, {"subtype": "error_max_turns"},
                       {"terminal_reason": "aborted_streaming"},
                       {"terminal_reason": "aborted_tools"},
                       {"terminal_reason": "a_reason_nobody_listed"}):
        _fake([real.AssistantMessage(content=text, model="m"), result(**unfinished)])
        with pytest.raises(aj.CheckFailed):
            await aj._ask_check("rb", "{}")
    _fake([real.AssistantMessage(content=text, model="m")])        # no terminal result
    with pytest.raises(aj.CheckFailed):
        await aj._ask_check("rb", "{}")
    _fake([real.AssistantMessage(content=text, model="m"), result()])
    assert aj.parse_reply("action", await aj._ask_check("rb", "{}")) == GO
    _fake([real.AssistantMessage(content=text, model="m"),
           result(stop_reason=None, terminal_reason=None)])          # an older CLI
    assert aj.parse_reply("action", await aj._ask_check("rb", "{}")) == GO


async def test_an_unfinished_check_answers_check_unavailable_on_the_route(monkeypatch):
    """The real route over the real ``_ask_check``: a cancelled turn whose
    text is a valid verdict is still no verdict."""
    import claude_agent_sdk as real
    monkeypatch.setattr(operator_calls, "CALLS", operator_calls.OperatorCalls())
    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = real.ClaudeAgentOptions
    fake.AssistantMessage = real.AssistantMessage
    fake.ResultMessage = real.ResultMessage

    async def query(*, prompt, options):
        yield real.AssistantMessage(content=[real.TextBlock(text=json.dumps(GO))], model="m")
        yield real.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                 is_error=False, num_turns=1, session_id="s",
                                 stop_reason="end_turn", terminal_reason="aborted_streaming")
    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    _call()
    assert await _post({"client": "c1", "kind": "action", "packet": _canonical(_packet()),
                        "spec_digest": DIGEST}) == {"error": "check_unavailable"}


def test_a_well_formed_reply_parses_whole():
    assert aj.parse_reply("action", "```json\n" + json.dumps(ASK) + "\n```") == ASK
    lf = {**GO, "verdict": "look_first", "category": "unclear",
          "look_first": "what the button leads to"}
    assert aj.parse_reply("action", json.dumps(lf)) == lf
    edge = _ask_with(text="x" * 600, confirm_label="x" * 32, extra_options=["x" * 60] * 3,
                     highlight=["x" * 200] * 6)
    assert aj.parse_reply("action", edge)["question"]["text"] == "x" * 600
    assert aj.parse_reply("action", json.dumps({**GO, "reason": "x" * 600}))["reason"] == "x" * 600


def test_every_measured_reply_is_within_the_bounds():
    """The measured maxima (767 parsed replies: reason 326, extra option 33, confirm label 32,
    text 548, highlight 99) all pass: a well-formed check never costs a retry."""
    q = {"text": "x" * 548, "confirm_label": "x" * 32, "extra_options": ["x" * 33] * 2,
         "screenshot": "useful", "highlight": ["x" * 99] * 6, "text_incomplete": False}
    reply = {"verdict": "ask", "category": "account_change", "reason": "x" * 326,
             "question": q}
    parsed = aj.parse_reply("action", json.dumps(reply))
    assert parsed is not None and parsed["reason"] == "x" * 326
    assert parsed["question"]["extra_options"] == ["x" * 33] * 2


# -- the route -------------------------------------------------------------------------

async def test_route_serves_only_the_in_flight_call(wired):
    seen, _ = wired
    body = {"kind": "action", "packet": _canonical(_packet()), "spec_digest": DIGEST}
    assert await _post({"client": "c1", **body}) == {"error": "no_call_in_flight"}
    _call()
    operator_calls.CALLS.open(
        client_id="c1", tool_use_id="tu-other", tool_name="x", role="browser",
        plugin="browser", identity=GrantIdentity(
            operator_id=100, chat_id=42, enforcement_role="browser",
            artifact_id=ARTIFACT, engagement_id="", bot_id=CASA_BOT), origin=ORIGIN)
    assert await _post({"client": "c1", **body}) == {"error": "ambiguous_call"}
    assert await _post({**body}) == {"error": "bad_client"}
    assert await _post(["not", "an", "object"]) == {"error": "bad_json"}
    assert seen == []


async def test_route_validates_the_request_before_any_model_call(wired):
    seen, _ = wired
    _call()
    text = _canonical(_packet())
    for body, err in [
        ({"kind": "login", "packet": text}, "bad_kind"),
        ({"packet": text}, "bad_kind"),
        ({"kind": "action", "packet": json.loads(text)}, "bad_packet"),   # not a string
        ({"kind": "action", "packet": "[1, 2]"}, "bad_packet"),
        ({"kind": "action", "packet": "{not json"}, "bad_packet"),
        ({"kind": "action", "packet": '{"a": "\ud800"}'}, "bad_packet"),
        ({"kind": "action", "packet": text, "spec_digest": "x" * 129}, "bad_digest"),
        ({"kind": "action", "packet": text, "spec_digest": "a\nb"}, "bad_digest"),
        ({"kind": "action", "packet": text, "spec_digest": 7}, "bad_digest"),
        ({"kind": "action", "packet": text, "spec_digest": ""}, "bad_digest"),
    ]:
        assert await _post({"client": "c1", **body}) == {"error": err}, body
    assert seen == []
    out = await _post({"client": "c1", "kind": "action", "packet": text, "spec_digest": None})
    assert out["verdict"] == GO


async def test_packet_bounds(wired):
    seen, _ = wired
    _call()
    pad = '{"a":"' + "€" * 10
    over = pad + "x" * (aj.MAX_PACKET_BYTES + 1 - len(pad.encode("utf-8")) - 2) + '"}'
    assert len(over.encode("utf-8")) == aj.MAX_PACKET_BYTES + 1
    assert await _post({"client": "c1", "kind": "action", "packet": over,
                        "spec_digest": DIGEST}) == {"error": "bad_packet"}
    assert seen == []
    at = over[:-3] + '"}'
    assert len(at.encode("utf-8")) == aj.MAX_PACKET_BYTES
    assert "verdict" in await _post({"client": "c1", "kind": "action", "packet": at,
                                     "spec_digest": DIGEST})
    assert len(seen) == 1


# -- the rulebooks are the measured files ----------------------------------------------

def test_rulebook_files_are_the_measured_bytes():
    measured = {
        # a cart, basket or wish list is local (go) without a plan, measured 2026-11-13
        "action_v1.txt": (8752, "01c8207a7c40e32134e4a9ff72156877"
                                "de5627c62abf05720575e07968e06dd7"),
        "rejudge_v1.txt": (2417, "7584e517b40bf7ec9c4c0e6e11c60820"
                                 "f68d8c9bd074b474d9e1f3397aca8eb0"),
        # slice 5, measured 2026-10-29
        "login_item_v1.txt": (1513, "6c31da11c895970fab24d82fc88e4c34"
                                    "b6576753235325ba706f8913670f6bc6"),
        "signin_v1.txt": (2037, "551b5d6477790a54b82c5121709115683"
                                "ae42a5f288d65d3998bf5fad4a0ebdf"),
        # signing in off the start page (M9, with R9's field mapping), measured 2026-11-03
        "sign_in_v1.txt": (3021, "a72031d6f76eb06038044531f26eb6dd"
                                 "fae484461d30fad4d3b6ac4996e9fe4a"),
    }
    for kind, name in aj.RULEBOOK_FILES.items():
        raw = (aj.RULEBOOK_DIR / name).read_bytes()
        assert (len(raw), hashlib.sha256(raw).hexdigest()) == measured[name]
        assert aj.RULEBOOKS[kind].encode("utf-8") == raw     # loaded without translation


# -- the log line ---------------------------------------------------------------------

async def test_log_line_carries_no_packet_text(wired, caplog):
    _, replies = wired
    aj.WORDS.record(plugin="browser", bot_id=CASA_BOT, operator_id=100, spec_digest=DIGEST, words=WORDS)
    replies.extend([json.dumps(ASK), RuntimeError(PAGE_TEXT), json.dumps(ASK) + REASON_TEXT])
    caplog.set_level(logging.DEBUG, logger="action_judgement")
    text = _canonical(_packet())
    out = await aj.judge(_call(), "action", text, DIGEST)
    for _ in range(2):
        await aj.judge(_call(client="c9"), "action", text, DIGEST)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert len(caplog.records) == 3
    assert "verdict=ask" in logged and "category=payment" in logged
    assert out["packet_sha256"][:12] in logged and out["packet_sha256"] not in logged
    for secret in (PAGE_TEXT, WORDS, REASON_TEXT, QUESTION["text"], "Voorbeeld"):
        assert secret not in logged


# -- slice 5: the login kinds (plan S5-2, S5-5) -----------------------------------------

LOGIN_ITEM = {"kind": "login_item",
              "site": {"name": "Voorbeeld Energie", "sign_in_host": "login.example.com",
                       "registered_origins": ["https://example.com"]},
              "items": [{"index": 0, "title": "Voorbeeld", "website_hosts": ["example.com"]},
                        {"index": 1, "title": "Other", "website_hosts": []}],
              "items_truncated": False}
SIGNIN = {"kind": "signin", "site": {"name": "Voorbeeld Energie",
                                     "origins": ["https://example.com"]},
          "after_attempt": [{"title": "Account", "host": "example.com", "path": "/account",
                             "blocks": [{"n": 1, "text": "Log out"}], "controls": [],
                             "password_field": False}],
          "at_done": {"title": "Account", "host": "example.com", "path": "/account",
                      "blocks": [{"n": 2, "text": "Your invoices"}], "controls": [],
                      "password_field": False}}


async def test_login_kinds_send_the_bare_packet_with_their_rulebook(wired):
    """No operator words, no wrapper: the model reads exactly the packet hashed, under the
    kind's own rulebook."""
    seen, replies = wired
    aj.WORDS.record(plugin="browser", bot_id=CASA_BOT, operator_id=100, spec_digest=DIGEST, words=WORDS)
    replies.append('{"items": [0], "reason": "the title names the site"}')
    out = await aj.judge(_call(), "login_item", _canonical(LOGIN_ITEM), DIGEST)
    sent = json.loads(_canonical(LOGIN_ITEM))                 # the order the app sends
    assert seen[-1] == (aj.RULEBOOKS["login_item"], json.dumps(sent, ensure_ascii=False))
    assert out["verdict"] == {"items": [0], "reason": "the title names the site"}
    assert out["packet_sha256"] == hashlib.sha256(_canonical(LOGIN_ITEM).encode()).hexdigest()
    replies.append('{"verdict": "signed_in", "evidence_blocks": [1, 2], "reason": "Log out"}')
    out = await aj.judge(_call(client="c2"), "signin", _canonical(SIGNIN), DIGEST)
    sent = json.loads(_canonical(SIGNIN))
    assert seen[-1] == (aj.RULEBOOKS["signin"], json.dumps(sent, ensure_ascii=False))
    assert out["verdict"] == {"verdict": "signed_in", "evidence_blocks": [1, 2],
                              "reason": "Log out"}
    assert WORDS not in seen[-2][1] + seen[-1][1]


@pytest.mark.parametrize("reply", [
    '{"items": [0, 1, 2, 3, 4, 5], "reason": ""}',       # more than five
    '{"items": [1, 1], "reason": ""}',                   # repeated
    '{"items": [-1], "reason": ""}',
    '{"items": [true], "reason": ""}',                   # a bool is not an index
    '{"items": [0.0], "reason": ""}',
    '{"items": "0", "reason": ""}',
    '{"items": [0]}',                                    # no reason
    '{"items": [0], "reason": "' + "x" * 601 + '"}',
    '{"verdict": "go", "category": "reading", "reason": ""}',
    'none fit',
])
async def test_a_login_item_reply_outside_its_shape_is_no_verdict(wired, reply):
    _, replies = wired
    replies.append(reply)
    assert await aj.judge(_call(), "login_item", _canonical(LOGIN_ITEM), None) == \
        {"error": "check_unavailable"}


@pytest.mark.parametrize("reply", [
    '{"verdict": "logged_in", "evidence_blocks": [], "reason": ""}',
    '{"verdict": "signed_in", "reason": ""}',            # no evidence list
    '{"verdict": "signed_in", "evidence_blocks": [0], "reason": ""}',   # blocks count from 1
    '{"verdict": "signed_in", "evidence_blocks": ["1"], "reason": ""}',
    '{"verdict": "signed_in", "evidence_blocks": [true], "reason": ""}',
    '{"verdict": "signed_in", "evidence_blocks": [1], "reason": "' + "x" * 601 + '"}',
    '{"verdict": "signed_in", "evidence_blocks": ' + json.dumps(list(range(1, 52))) + ', "reason": ""}',
    '{"verdict": "same", "reason": "", "text_incomplete": false}',
])
async def test_a_signin_reply_outside_its_shape_is_no_verdict(wired, reply):
    _, replies = wired
    replies.append(reply)
    assert await aj.judge(_call(), "signin", _canonical(SIGNIN), None) == \
        {"error": "check_unavailable"}


def test_login_replies_within_their_bounds_parse_whole():
    assert aj.parse_reply("login_item", '{"items": [], "reason": "none fits"}') == \
        {"items": [], "reason": "none fits"}
    assert aj.parse_reply("login_item", '{"items": [4, 0, 2, 1, 3], "reason": "x"}')["items"] \
        == [4, 0, 2, 1, 3]
    for verdict in ("signed_in", "not_signed_in", "unsure"):
        assert aj.parse_reply("signin", json.dumps(
            {"verdict": verdict, "evidence_blocks": [], "reason": "r"})) == \
            {"verdict": verdict, "evidence_blocks": [], "reason": "r"}


async def test_the_signin_check_logs_nothing_it_read(wired, caplog):
    """Operator ruling (slice 5): Casa's sign-in check writes no packet text, page text or reply
    to any log at any level (the CLI runs with no session persistence, so no transcript is
    written either: ``claude_runtime.ephemeral_extra_args``)."""
    _, replies = wired
    marker = "PageEchoWinterFrost77"
    packet = {**SIGNIN, "at_done": {**SIGNIN["at_done"],
                                    "blocks": [{"n": 2, "text": "Echo " + marker}]}}
    replies.extend([json.dumps({"verdict": "signed_in", "evidence_blocks": [2],
                                "reason": "shows " + marker}), "not json " + marker])
    caplog.set_level(logging.DEBUG)
    out = await aj.judge(_call(), "signin", _canonical(packet), None)
    assert out["verdict"]["verdict"] == "signed_in"
    assert await aj.judge(_call(client="c9"), "signin", _canonical(packet), None) == \
        {"error": "check_unavailable"}
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert len(caplog.records) == 2 and "kind=signin" in logged
    assert marker not in logged and "Echo" not in logged
    import claude_runtime
    assert claude_runtime.ephemeral_extra_args() == {"no-session-persistence": None}


async def test_the_sdks_own_payload_records_are_silenced_during_a_check(monkeypatch, caplog):
    """r6 Astra A5: on a truncated CLI reply the SDK logs the raw stdout (reply text that may
    quote the page) from its own loggers; inside a check those records are dropped."""
    import claude_agent_sdk as real
    marker = "PageEchoWinterFrost77"
    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = real.ClaudeAgentOptions
    fake.AssistantMessage = real.AssistantMessage
    fake.ResultMessage = real.ResultMessage

    async def query(*, prompt, options):
        logging.getLogger("claude_agent_sdk._internal.transport.subprocess_cli").debug(
            "Dropping truncated JSON at end of CLI stdout: %s", marker)
        if False:
            yield None
    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(aj.CheckFailed):
        await aj._ask_check("rb", "{}")
    assert marker not in "\n".join(r.getMessage() for r in caplog.records)
    logging.getLogger("claude_agent_sdk._internal.transport.subprocess_cli").debug(marker)
    assert marker in "\n".join(r.getMessage() for r in caplog.records)   # control: outside a check


# -- signing in off the start page: the sign_in kind ----------------------------------------

SIGN_IN = {"kind": "sign_in",
           "site": {"name": "Voorbeeld Energie", "registered_origins": ["https://example.com"],
                    "host": "example.com"},
           "page": {"title": "Log in", "path": "/login", "blocks": [{"n": 1, "text": "Log in"}],
                    "page_truncated": False, "controls": ["textbox: Email", "button: Log in"]},
           "planned": {"fields": [{"role": "username", "label": "Email"}], "button": "Log in",
                       "passkey": False}}


async def test_the_sign_in_check_sends_the_bare_packet_with_its_rulebook(wired):
    seen, replies = wired
    aj.WORDS.record(plugin="browser", bot_id=CASA_BOT, operator_id=100, spec_digest=DIGEST, words=WORDS)
    for verdict in ("sign_in", "not_sign_in"):
        replies.append(json.dumps({"verdict": verdict, "reason": "the page says so",
                                   "fields": {"username": "f1"}}))
        out = await aj.judge(_call(client="c-" + verdict), "sign_in", _canonical(SIGN_IN), DIGEST)
        sent = json.loads(_canonical(SIGN_IN))
        assert seen[-1] == (aj.RULEBOOKS["sign_in"], json.dumps(sent, ensure_ascii=False))
        assert out["verdict"] == {"verdict": verdict, "reason": "the page says so",
                                  "fields": {"username": "f1"}}
        assert out["packet_sha256"] == hashlib.sha256(_canonical(SIGN_IN).encode()).hexdigest()
        assert WORDS not in seen[-1][1]


@pytest.mark.parametrize("reply", [
    '{"verdict": "signed_in", "reason": ""}',            # another kind's verdict
    '{"verdict": "go", "category": "login", "reason": ""}',
    '{"verdict": "yes", "reason": ""}',
    '{"verdict": "sign_in", "reason": "' + "x" * 601 + '"}',
    '{"verdict": "sign_in", "reason": 3}',
    'sign_in',
    '{"verdict": "sign_in", "reason": "", "fields": ["f1"]}',
    '{"verdict": "sign_in", "reason": "", "fields": {"login": "f1"}}',
    '{"verdict": "sign_in", "reason": "", "fields": {"username": 1}}',
    '{"verdict": "sign_in", "reason": "", "fields": {"username": "' + "f" * 17 + '"}}',
])
async def test_a_sign_in_reply_outside_its_shape_is_no_verdict(wired, reply):
    _, replies = wired
    replies.append(reply)
    assert await aj.judge(_call(), "sign_in", _canonical(SIGN_IN), None) == \
        {"error": "check_unavailable"}


def test_a_sign_in_reply_parses_to_its_verdict_and_reason_only():
    assert aj.parse_reply("sign_in", '{"verdict": "not_sign_in", "reason": "a newsletter", '
                                     '"evidence_blocks": [1]}') == \
        {"verdict": "not_sign_in", "reason": "a newsletter", "fields": {}}
    assert aj.parse_reply("sign_in", '{"verdict": "sign_in"}') == {"verdict": "sign_in",
                                                                     "reason": "", "fields": {}}
    # R9: the planned field ids the username and the password go into; a null is no field
    assert aj.parse_reply("sign_in", '{"verdict": "sign_in", "reason": "r", "fields": '
                                     '{"username": "f2", "password": null}}') == \
        {"verdict": "sign_in", "reason": "r", "fields": {"username": "f2"}}


# -- the picture and the two addenda (INV-PLUG-057) ------------------------------------

def test_addenda_are_the_measured_bytes():
    measured = {"picture_v1.txt": (1263, "26d45536f8ed4a879d40fecf60b711ec"
                                         "edf2324ebc17e6e9f2fe6e0fefcbc5fa"),
                "scope_v1.txt": (3366, "4eba6e3147aa5323a477ec696bf6141c"
                                       "8519eef5c436169dab7a0fed11155ba0")}
    for name, want in measured.items():
        raw = (aj.RULEBOOK_DIR / name).read_bytes()
        assert (len(raw), hashlib.sha256(raw).hexdigest()) == want
    assert aj.PICTURE_ADDENDUM.encode("utf-8") == (aj.RULEBOOK_DIR / "picture_v1.txt").read_bytes()
    assert aj.SCOPE_ADDENDUM.encode("utf-8") == (aj.RULEBOOK_DIR / "scope_v1.txt").read_bytes()
    assert "picture_v1.txt" not in aj.RULEBOOK_FILES.values()
    assert "scope_v1.txt" not in aj.RULEBOOK_FILES.values()


def test_system_prompt_appends_only_what_the_call_carries():
    A, P, S = aj.RULEBOOKS["action"], aj.PICTURE_ADDENDUM, aj.SCOPE_ADDENDUM
    assert aj.system_prompt("action", {}, None) == A
    assert aj.system_prompt("action", {}, "x") == A + P
    assert aj.system_prompt("action", {"plan": "p"}, None) == A + S
    assert aj.system_prompt("action", {"approved": "p"}, None) == A + S
    assert aj.system_prompt("action", {"approved": "p"}, "x") == A + P + S
    assert aj.system_prompt("action", {"plan": "p", "approved": "q"}, "x") == A + P + S
    assert aj.system_prompt("rejudge", {"plan": "p"}, "x") == aj.RULEBOOKS["rejudge"]


def _recording_sdk(monkeypatch, launches: list, *, reply: str = json.dumps(GO)):
    """A ``claude_agent_sdk`` whose ``query`` records its prompt, read as the CLI would be
    sent it, and its options, and answers one completed verdict."""
    import claude_agent_sdk as real
    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = real.ClaudeAgentOptions
    fake.AssistantMessage = real.AssistantMessage
    fake.ResultMessage = real.ResultMessage

    async def query(*, prompt, options):
        sent = prompt if isinstance(prompt, str) else [m async for m in prompt]
        launches.append((sent, options))
        yield real.AssistantMessage(content=[real.TextBlock(text=reply)], model="m")
        yield real.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                 is_error=False, num_turns=1, session_id="s",
                                 stop_reason="end_turn", terminal_reason="completed")
    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


async def test_picture_and_body_go_as_one_user_message(monkeypatch):
    launches: list = []
    _recording_sdk(monkeypatch, launches)
    pic = _b64(PNG + b"rest")
    assert await aj._ask_check("rb", '{"packet": 1}', pic) == json.dumps(GO)
    sent, options = launches[0]
    assert sent == [{"type": "user", "session_id": "", "parent_tool_use_id": None,
                     "message": {"role": "user", "content": [
                         {"type": "image", "source": {"type": "base64",
                                                      "media_type": "image/png", "data": pic}},
                         {"type": "text", "text": '{"packet": 1}'}]}}]
    assert options.tools == [] and sent_prompt(options) == "rb"
    # no picture: the prompt is the body string, as it was
    await aj._ask_check("rb", '{"packet": 1}')
    assert launches[1][0] == '{"packet": 1}'


async def test_the_check_runs_with_the_measured_route_switches(monkeypatch):
    launches: list = []
    _recording_sdk(monkeypatch, launches)
    await aj._ask_check("rb", "{}")
    await aj._ask_check("rb", "{}", _b64(PNG))
    for _sent, options in launches:
        assert options.extra_args == {"no-session-persistence": None, "strict-mcp-config": None}
        assert options.env == {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}


@pytest.mark.parametrize("bad", [
    5, b"bytes", ["x"], "", "not base64!", "é", _b64(b"GIF89a"), _b64(PNG)[:-1],
    _b64(PNG + b"x" * (2 * 1024 * 1024 - len(PNG) + 1))], ids=[
    "int", "bytes", "list", "empty", "not_base64", "not_ascii", "gif", "cut", "one_over_the_bound"])
async def test_a_bad_picture_is_no_picture(wired, bad):
    seen, _ = wired
    out = await aj.judge(_call(), "action", _canonical(_packet()), DIGEST, picture=bad)
    assert out["verdict"] == GO
    assert seen[0][0] == aj.RULEBOOKS["action"] and PICTURES == [None]


async def test_a_picture_at_the_bound_is_a_picture(wired):
    seen, _ = wired
    pic = _b64(PNG + b"x" * (aj.MAX_PICTURE_BYTES - len(PNG)))
    await aj.judge(_call(), "action", _canonical(_packet()), DIGEST, picture=pic)
    assert seen[0][0] == aj.RULEBOOKS["action"] + aj.PICTURE_ADDENDUM and PICTURES == [pic]


@pytest.mark.parametrize("packet_extra, addenda", [
    ({}, ""), ({"plan": "add 96 cards"}, "S"), ({"approved": "add 96 cards"}, "S")])
async def test_the_check_is_sent_what_its_call_carries(wired, packet_extra, addenda):
    seen, _ = wired
    pic = _b64(PNG)
    raw = _canonical(_packet(**packet_extra))
    await aj.judge(_call(), "action", raw, DIGEST, picture=pic)
    await aj.judge(_call(), "action", raw, DIGEST)
    scope = aj.SCOPE_ADDENDUM if addenda else ""
    assert seen[0][0] == aj.RULEBOOKS["action"] + aj.PICTURE_ADDENDUM + scope
    assert seen[1][0] == aj.RULEBOOKS["action"] + scope
    assert PICTURES == [pic, None]
    # the digest binds the packet alone, picture or none
    assert json.loads(seen[0][1]) == json.loads(seen[1][1])


async def test_the_digest_does_not_move_with_the_picture(wired):
    raw = _canonical(_packet())
    with_pic = await aj.judge(_call(), "action", raw, DIGEST, picture=_b64(PNG + b"a"))
    other = await aj.judge(_call(), "action", raw, DIGEST, picture=_b64(PNG + b"b"))
    without = await aj.judge(_call(), "action", raw, DIGEST)
    assert (with_pic["packet_sha256"] == other["packet_sha256"]
            == without["packet_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest())


@pytest.mark.parametrize("kind", ["rejudge", "login_item", "signin", "sign_in"])
async def test_a_picture_on_another_kind_is_ignored(wired, kind):
    seen, replies = wired
    replies.append(json.dumps({"verdict": "same", "reason": "", "text_incomplete": False,
                               "items": [], "evidence_blocks": []}))
    await aj.judge(_call(), kind, _canonical({"plan": "p", "approved": "q"}), DIGEST,
                   picture=_b64(PNG))
    assert seen[0][0] == aj.RULEBOOKS[kind] and PICTURES == [None]


async def test_log_line_carries_no_picture(wired, caplog):
    _seen, replies = wired
    caplog.set_level(logging.DEBUG)
    pic = _b64(PNG + b"PictureBytesMarker")
    await aj.judge(_call(), "action", _canonical(_packet()), DIGEST, picture=pic)
    replies.append(RuntimeError("no"))
    await aj.judge(_call(), "action", _canonical(_packet()), DIGEST, picture=pic)
    await aj.judge(_call(), "action", _canonical(_packet()), DIGEST)
    lines = [r.getMessage() for r in caplog.records]
    assert pic not in "\n".join(lines)
    judged = [ln for ln in lines if ln.startswith("action judgement:")]
    assert len(judged) == 3
    assert "picture=yes" in judged[0] and "picture=yes" in judged[1]
    assert "picture=no" in judged[2]


async def test_the_route_passes_the_picture_to_the_judge(wired):
    seen, _ = wired
    _call()
    pic = _b64(PNG)
    out = await _post({"client": "c1", "kind": "action", "packet": _canonical(_packet()),
                       "spec_digest": DIGEST, "picture": pic})
    assert out["verdict"] == GO and PICTURES == [pic]
    assert seen[0][0] == aj.RULEBOOKS["action"] + aj.PICTURE_ADDENDUM


async def test_a_large_picture_reaches_the_judge_route(wired):
    """A rich signed-in page: a real 1.41 MB picture is over aiohttp's default 1 MiB body."""
    _call()
    pic = _b64(PNG + b"x" * 1_410_000)
    body = {"client": "c1", "kind": "action", "packet": _canonical(_packet()),
            "spec_digest": DIGEST, "picture": pic}

    async def _posted(app):
        app.router.add_post("/j", operator_calls.build_action_judgement_handler())
        async with TestClient(TestServer(app)) as client:
            return await (await client.post("/j", json=body)).json()

    assert await _posted(web.Application()) == {"error": "bad_json"}
    out = await _posted(web.Application(client_max_size=operator_calls.INTERNAL_MAX_BODY_BYTES))
    assert out["verdict"] == GO and PICTURES == [pic]


async def test_the_largest_picture_beside_the_largest_escaped_packet_gets_its_verdict(wired):
    """Both members at their bounds, sent as the plugin's client sends them: ``json.dumps``
    escapes an accented letter's two UTF-8 bytes to six."""
    _call()
    pic = _b64(PNG + b"x" * (aj.MAX_PICTURE_BYTES - len(PNG)))
    pad = (aj.MAX_PACKET_BYTES - len(_canonical({"p": ""}).encode("utf-8"))) // 2
    packet = _canonical({"p": "é" * pad})
    assert len(packet.encode("utf-8")) <= aj.MAX_PACKET_BYTES
    body = json.dumps({"client": "c1", "kind": "action", "packet": packet,
                       "spec_digest": DIGEST, "picture": pic}).encode("utf-8")
    assert len(body) > 4 * 1024 * 1024
    app = web.Application(client_max_size=operator_calls.INTERNAL_MAX_BODY_BYTES)
    app.router.add_post("/j", operator_calls.build_action_judgement_handler())
    async with TestClient(TestServer(app)) as client:
        out = await (await client.post("/j", data=body,
                                       headers={"Content-Type": "application/json"})).json()
    assert out["verdict"] == GO and PICTURES == [pic]


def test_casa_core_internal_app_takes_the_bound():
    import casa_core
    source = Path(casa_core.__file__).read_text(encoding="utf-8")
    assert "client_max_size=INTERNAL_MAX_BODY_BYTES" in source
