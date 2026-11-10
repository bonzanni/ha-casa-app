"""Task 3: wire types and body parsing."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac

from brokerfakes import canary

import pytest

from casa_browser.protocol import (
    ERROR_CODES,
    ERROR_STATUS,
    ActiveRun,
    FileRecord,
    Observation,
    ProtocolError,
    ProviderBinding,
    Site,
    Source,
    parse_action,
    parse_delivery,
    parse_finish,
    parse_login_code,
    parse_live,
    parse_look,
    parse_read,
    parse_run_spec,
    parse_site_registration,
)

from apihelpers import run_body

PLAN_CODES = {"unauthorized", "protocol_mismatch", "not_ready", "stale_handle",
              "blocked", "unsupported", "interrupted", "file_too_large"}


def test_error_codes_cover_plan_and_have_one_status():
    assert PLAN_CODES <= ERROR_CODES
    assert "approval_required" not in ERROR_CODES          # slice 7: nothing asks for approval
    assert ERROR_STATUS["unauthorized"] == 401
    assert ERROR_STATUS["conflict"] == 409
    assert ERROR_STATUS["not_found"] == 404
    assert ERROR_STATUS["file_too_large"] == 413
    with pytest.raises(ValueError):
        ProtocolError("made_up")


def test_parse_run_spec_round_trip():
    spec, members = parse_run_spec(run_body())
    assert spec.members() == members
    assert spec.sites[0].site_id == "site_a"
    for mutate in (lambda b: b.update(mode="admin"), lambda b: b.update(sites=[]),
                   lambda b: b.update(sites=[{"site_id": "bad id!", "access": "account"}]),
                   lambda b: b.update(sites=[{"site_id": "a", "access": "account"}] * 2),
                   lambda b: b.update(task_text=""), lambda b: b.update(task_text=None),
                   lambda b: b["admission"].update(admitted_spec_digest="sha256:xyz"),
                   lambda b: b["admission"].update(extra=1), lambda b: b.pop("task_kind"),
                   lambda b: b.update(task_kind=5)):
        body = run_body()
        mutate(body)
        with pytest.raises(ProtocolError):
            parse_run_spec(body)


def test_start_url_only_in_an_open_web_spec_and_a_digest_member_only_when_given():
    """Slice 5b (F-a): an open-web spec may name its first page; an account spec never."""
    url = "https://shop.example/forms/post"
    body = run_body(mode="open_web", sites=[], start_url=url)
    spec, members = parse_run_spec(body)
    assert spec.start_url == url and spec.members() == members and members["start_url"] == url
    assert "start_url" not in parse_run_spec(run_body())[0].members()        # unchanged digests
    for bad in ("http://shop.example/", "https:///x", "https://a b/", "https://a.example/\n",
                "https://a.example/" + "x" * 2048, 7, None):
        with pytest.raises(ProtocolError):
            parse_run_spec(run_body(mode="open_web", sites=[], start_url=bad))
    with pytest.raises(ProtocolError):
        parse_run_spec(run_body(start_url=url))                               # an account spec


def test_parse_live_is_a_login_or_an_assist_view():
    """Slice 7: the demo live view is gone."""
    assert parse_live({"purpose": "login"}) == "login"
    assert parse_live({"purpose": "assist"}) == "assist"
    for body in ({"purpose": "demo"}, {"purpose": "login", "extra": 1}, {}):
        with pytest.raises(ProtocolError):
            parse_live(body)


def test_parse_finish_and_delivery():
    m = parse_finish({"summary_masked": "ok", "file_ids": ["f1"], "source_ids": ["s1"],
                      "expectation_status": "mismatch"})
    assert m.source_ids == ("s1",)
    with pytest.raises(ProtocolError):
        parse_finish({"summary_masked": "ok", "file_ids": [], "source_ids": [],
                      "expectation_status": "great"})
    assert parse_delivery({"requester_id": "finance", "delivery_id": "d1",
                           "outcome": "handed_off"})["outcome"] == "handed_off"
    with pytest.raises(ProtocolError):
        parse_delivery({"requester_id": "finance", "delivery_id": "d1", "outcome": "lost"})


def test_parse_action_takes_ask_first_as_a_boolean_only():
    base = {"kind": "click", "handle": "el_1", "generation": 3}
    assert parse_action(base).ask_first is False
    assert parse_action({**base, "ask_first": True}).ask_first is True
    for bad in ("yes", 1, None):
        with pytest.raises(ProtocolError):
            parse_action({**base, "ask_first": bad})


def test_parse_action_shapes():
    a = parse_action({"kind": "type", "handle": "el_1", "generation": 3, "text": "hello"})
    assert a.text == "hello" and "hello" not in repr(a)
    with pytest.raises(ProtocolError):
        parse_action({"kind": "navigate", "handle": "el_1", "generation": -1})
    with pytest.raises(ProtocolError):
        parse_action({"kind": "click", "handle": "el_1", "generation": True})


@pytest.mark.parametrize("text", [
    "a\tb", "a\x1bb", "a\x08b", "a\x00b", "a\x7fb", "a\x0bb", "a\x0cb",
    "a\x85b", "a\x9fb", "a\u2028b", "a\u2029b", "a\n\tb"],
    ids=["Tab", "Escape", "Backspace", "NUL", "DEL", "VT", "FF", "NEL",
         "C1-end", "LINE-SEP", "PARA-SEP", "LF-then-Tab"])
def test_parse_action_refuses_control_characters(text):
    """S2-4 rule 1: typed text holds printable characters only, refused before any packet, so
    no typed character can submit or navigate; only the guarded Enter can. Slice 6b: a line
    break is the one exception (below); every other control character is still refused."""
    with pytest.raises(ProtocolError, match="unsupported"):
        parse_action({"kind": "type", "handle": "el_1", "generation": 3, "text": text})


@pytest.mark.parametrize("text,want", [
    ("a\nb", "a\nb"), ("ab\n", "ab\n"), ("a\r\nb", "a\nb"), ("a\rb", "a\nb"),
    ("Hallo,\n\nDank je.\r\nJan", "Hallo,\n\nDank je.\nJan")],
    ids=["LF", "trailing-LF", "CRLF", "CR", "mixed"])
def test_parse_action_takes_line_breaks_as_lf(text, want):
    """Slice 6b: a line break is parsed (CR and CRLF arrive as LF, what a textarea holds); where
    it may go — only a real <textarea>, only on a judged step — the controller decides."""
    a = parse_action({"kind": "type", "handle": "el_1", "generation": 3, "text": text})
    assert a.text == want


def test_parse_action_enter_only_for_type():
    a = parse_action({"kind": "type", "handle": "el_1", "generation": 3, "text": "factuur oktober",
                      "enter": True})
    assert a.enter is True
    assert parse_action({"kind": "type", "handle": "el_1", "generation": 3, "text": "é ü 😀 x"}).enter is False
    for bad in ({"kind": "click", "handle": "el_1", "generation": 3, "enter": True},
                {"kind": "type", "handle": "el_1", "generation": 3, "text": "x", "enter": "yes"},
                {"kind": "click", "handle": "el_1", "generation": 3, "verdict": {"verdict": "go"}},
                {"kind": "click", "handle": "el_1", "generation": 3, "packet_sha256": "0" * 64}):
        with pytest.raises(ProtocolError, match="unsupported"):
            parse_action(bad)


def test_parse_judgement_verdict_and_answer():
    from casa_browser.protocol import parse_answer, parse_judgement_verdict
    v = parse_judgement_verdict({"packet_sha256": "a" * 64, "verdict": {"verdict": "go"}})
    assert v["verdict"] == {"verdict": "go"} and v["latency_ms"] is None
    assert parse_judgement_verdict({"no_verdict": "check_unavailable"}) == {
        "no_verdict": "check_unavailable"}
    for bad in ({"packet_sha256": "A" * 64, "verdict": {}}, {"packet_sha256": "a" * 63, "verdict": {}},
                {"packet_sha256": "a" * 64, "verdict": "go"},
                {"no_verdict": "timeout"}, {"no_verdict": "check_unavailable", "verdict": {}},
                {"packet_sha256": "a" * 64, "verdict": {}, "extra": 1}):
        with pytest.raises(ProtocolError):
            parse_judgement_verdict(bad)
    # A verdict outside the check's bounds is parsed (it becomes no verdict, F1), not refused.
    assert parse_judgement_verdict({"packet_sha256": "a" * 64,
                                    "verdict": {"verdict": "go", "reason": "x" * 5000}})
    a = parse_answer({"revision": 1, "digest": "b" * 64, "choice": {"extra": 2},
                      "operator_ref": "tg-1"})
    assert a["choice"] == {"extra": 2}
    for bad in ({"revision": 0, "digest": "b" * 64, "choice": "confirm", "operator_ref": "tg"},
                {"revision": 1, "digest": "b" * 64, "choice": {"extra": 3}, "operator_ref": "tg"},
                {"revision": 1, "digest": "b" * 64, "choice": "yes", "operator_ref": "tg"},
                {"revision": 1, "digest": "sha256:" + "b" * 64, "choice": "confirm",
                 "operator_ref": "tg"}):
        with pytest.raises(ProtocolError):
            parse_answer(bad)
    assert ERROR_STATUS["judgement_stale"] == 409 and ERROR_STATUS["digest_mismatch"] == 409


def test_site_registration_only_https_origins():
    ok = {"display_name": "RunPod", "origins": ["https://console.runpod.io"],
          "login_origins": ["https://clerk.runpod.io"], "operator_ref": "tg-1",
          "vault_item_id": "abc123", "start_page": "https://console.runpod.io/user/billing"}
    assert parse_site_registration(ok)["origins"] == ("https://console.runpod.io",)
    for bad in ({**ok, "origins": ["http://console.runpod.io"]},
                {**ok, "origins": ["https://user@example.com"]},
                {**ok, "origins": ["https://x.example/path"]},
                {**ok, "start_page": "javascript:alert(1)"},
                {**ok, "vault_item_id": "op://vault/item"}):
        with pytest.raises(ProtocolError):
            parse_site_registration(bad)


def test_private_members_never_in_repr_or_wire():
    """Tagged bindings and other private site members remain absent from public reprs."""
    key = canary().encode()
    tags = {label + '_tag': hmac.new(key, label.encode() + b'\0' + raw, hashlib.sha256).hexdigest()
            for label, raw in [('path', b'/auth'), ('client_id', b'client-123'),
                               ('redirect_uri', b'https://cb.example/cb')]}
    site = Site("s", "Name", ("https://secret-origin.example",), ("https://login.example",),
                "vaultitem", (ProviderBinding("https://p.example", **tags),),
                "https://secret-origin.example/start?token=abc", 1)
    text = repr(site) + repr(site.provider_bindings)
    for tag in tags.values():
        assert tag not in text
    for private in ("secret-origin", "vaultitem", "client-123", "token=abc"):
        assert private not in text
    f = FileRecord("f1", "r1", "finance", "a.pdf", "application/pdf", 1, "0" * 64,
                   "/data/artifacts/r1/abc", None, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "/data/artifacts" not in repr(f) and "private_path" not in f.to_wire()
    s = Source("src_1", "https://example.org/a?x=1", "Title")
    assert "example.org" not in repr(s)


@pytest.mark.parametrize("code", ["1234", "ABCDEF", "abc-123", "0123456789"])
def test_login_code_bounds_and_no_repr(code):
    """Slice 6 (S6-3): a sign-in code is 4–10 of [A-Za-z0-9-]; the parsed value never shows it."""
    parsed = parse_login_code({"code": code, "site_id": "sa"})
    assert (parsed.value, parsed.site_id) == (code, "sa")
    assert code not in repr(parsed) and code not in str(parsed)


@pytest.mark.parametrize("body", [
    {"code": "123"}, {"code": "12345678901"}, {"code": "12 34"}, {"code": "12_34"},
    {"code": "1234\n"}, {"code": "１２３４"}, {"code": 1234}, {"code": None}, {"site_id": "sa"},
    {"code": "1234", "site_id": "sa", "run_id": "r1"}, {"code": "1234"},
    {"code": "1234", "site_id": "s a"}, "1234", ["1234"]],
    ids=["short", "long", "space", "underscore", "newline", "fullwidth", "int", "null", "missing",
         "extra", "no-site", "bad-site", "string", "list"])
def test_login_code_refusals_never_echo(body):
    if isinstance(body, dict) and "code" in body and "site_id" not in body and body != {"code": "1234"}:
        body = {**body, "site_id": "sa"}
    with pytest.raises(ProtocolError, match="unsupported") as caught:
        parse_login_code(body)
    if isinstance(body, dict) and isinstance(body.get("code"), str):
        assert body["code"] not in str(caught.value) and body["code"] not in repr(caught.value)


def test_text_bounds():
    """One line stays within what typing takes; a text with line breaks may be a whole list."""
    def body(text):
        return {"kind": "type", "handle": "h", "generation": 1, "text": text}
    assert parse_action(body("x" * 2000)).text == "x" * 2000
    with pytest.raises(ProtocolError):
        parse_action(body("x" * 2001))
    long = ("x" * 99 + "\n") * 320
    assert parse_action(body(long)).text == long and len(long) == 32_000
    with pytest.raises(ProtocolError):
        parse_action(body(long + "x"))
    assert parse_action(body(("x" * 2500) + "\r\ny")).text == "x" * 2500 + "\ny"


def test_parse_action_bounds_the_plan():
    def body(plan):
        return {"kind": "click", "handle": "h", "generation": 1, "plan": plan}
    action = parse_action(body("Add:\r\n1 Sol Ring"))
    assert action.plan == "Add:\n1 Sol Ring" and "Sol Ring" not in repr(action)
    assert len(parse_action(body("x" * 12_000)).plan) == 12_000
    for bad in ("x" * 12_001, "", "  \n", "a\x07b", 5):
        with pytest.raises(ProtocolError):
            parse_action(body(bad))
    assert parse_action({"kind": "click", "handle": "h", "generation": 1}).plan is None


def test_active_run_carries_approved_on_the_wire():
    at = datetime(2026, 11, 6, tzinfo=timezone.utc)
    assert ActiveRun("r", "q", "running", at).to_wire()["approved"] is None
    assert ActiveRun("r", "q", "running", at, approved="the plan").to_wire()["approved"] == "the plan"


def test_an_observation_says_it_was_cut_only_when_it_was():
    plain = Observation("p", 1, "text", ())
    assert "text_cut" not in plain.to_wire()
    assert Observation("p", 1, "text", (), text_cut=True).to_wire()["text_cut"] is True


def test_parse_look_and_read():
    assert parse_look({}) is None and parse_look({"handle": "h1"}) == "h1"
    assert parse_read({}) == (None, None, 0, False)
    assert parse_read({"handle": "h1", "offset": 30}) == ("h1", None, 30, False)
    assert parse_read({"file_id": "f1"}) == (None, "f1", 0, False)
    assert parse_read({"rows": True, "offset": 5}) == (None, None, 5, True)
    assert parse_read({"rows": False, "handle": "h1"}) == ("h1", None, 0, False)
    for bad in ({"handle": "h", "file_id": "f"}, {"offset": -1}, {"offset": "1"}, {"other": 1},
                {"handle": "a b"}, {"rows": 1}, {"rows": True, "handle": "h"},
                {"rows": True, "file_id": "f"}):
        with pytest.raises(ProtocolError):
            parse_read(bad)
    with pytest.raises(ProtocolError):
        parse_look({"offset": 1})
