"""Action model slice 2 in the image: the gate with the production run controller, real Chrome
(the browser user, through the launcher), the real pacer and the gate's own page reading,
and a SCRIPTED judge (the test plays the plugin's relay and chooses each verdict). Design §7's
floor and tap-time fixtures and the plan's integration cases 1–28, as local synthetic pages.

Most pages are a public grant's pages, opened in the public session: a click on a control
that is not a link, a select and typing into a field that is not a search field are checked
there too (design §3), and the session sends freely, so a press that should not happen would
reach the fixture and be counted. Signed-in cases use an account site, whose own pages its
session reads freely (design §4).

Every case asserts what the app handed the check (the difference, ``destination_changed``)
and that nothing ran that should not: zero wrong-target and zero duplicate executions.
"""
from __future__ import annotations

import json

import pytest

from lifecycleprobe import controller_home, run_lifecycle
from sitefixture import ChromeSites
from test_run_lifecycle import fixture_config

pytestmark = pytest.mark.docker

A, B, C = "site-a.example", "pub-b.example", "pub-c.example"   # C: another public site
GO = {"verdict": "go", "category": "reading", "reason": "reads"}
ASK = {"verdict": "ask", "category": "payment", "reason": "pays",
       "question": {"text": "Pay € 84,20 to Public B?", "confirm_label": "Pay € 84,20",
                    "extra_options": [], "screenshot": "required", "highlight": ["€ 84,20"],
                    "text_incomplete": False}}
ASK_CHANGE = {"verdict": "ask", "category": "personal_data", "reason": "sends a message",
              "question": {"text": "Send this message to Public B?", "confirm_label": "Send",
                           "extra_options": [], "screenshot": "useful", "highlight": [],
                           "text_incomplete": False}}
SAME = {"verdict": "same", "reason": "unchanged", "text_incomplete": False}
CHANGED = {"verdict": "changed", "reason": "changed", "text_incomplete": False}


def page(body: str, script: str = "") -> bytes:
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>Public B</title></head>'
            f'<body>{body}<script>{script}</script></body></html>').encode("utf-8")


SUBMIT = "fetch('/submit?' + this.dataset.k, {method: 'POST'})"


def gate_run(tmp_path, html: bytes, steps: list, *, pages: dict | None = None,
             account_html: bytes | None = None, timeout: int = 300):
    """An account run with Site A (signed in) and Public B (public), opened on Public B's page
    (``html``), then ``steps``. Returns (the steps' results, the fixture's received requests)."""
    config = fixture_config(tmp_path)
    with ChromeSites([A, B, C]) as sites:
        sites.call(setattr, sites.harness.proxy, "controller_uid", 1103)
        sites.pages["/start"] = ("text/html", account_html or b"<title>A</title><p>Account</p>", {})
        sites.pages["/pub"] = ("text/html", html, {})
        for path in ("/submit", "/save", "/msg", "/pay", "/delete", "/frame1", "/frame2"):
            sites.pages[path] = ("text/html", b"<p>ok</p>", {})
        for path, body in (pages or {}).items():
            sites.pages[path] = ("text/html", body, {})
        base = [
            {"site": {"site_id": "sa", "name": "Site A", "origins": [f"https://{A}"],
                      "start": f"https://{A}/start"}},
            {"site": {"site_id": "sp", "name": "Public B", "origins": [f"https://{B}"],
                      "start": f"https://{B}/pub"}},
            {"site": {"site_id": "sq", "name": "Public C", "origins": [f"https://{C}"],
                      "start": f"https://{C}/frame1"}},
            {"run": {"name": "r", "request_id": "it-gate",
                     "sites": [["sa", "account"], ["sp", "public"], ["sq", "public"]]}},
            {"wait_state": {"name": "r", "state": "running"}},
            {"observe": {"name": "r"}},
        ]
        if account_html is None:
            base += [{"act": {"name": "r", "kind": "navigate", "label": "Open site: Public B"}},
                     {"sleep": 0.5}, {"observe": {"name": "r"}}]
        out = run_lifecycle(base + steps, home=controller_home(tmp_path), broker_fixture=config,
                            timeout=timeout)
        requests = [(m, host, path) for m, host, path, _b in sites.receipts.requests]
    return out[len(base):], requests


def hits(requests, prefix):
    return [r for r in requests if r[2].startswith(prefix)]


def packet(reply):
    return json.loads(reply["judgement"]["packet"])


R = "r"


def act(kind, label, **kw):
    return {"act": {"name": R, "kind": kind, "label": label, "hold": True, **kw}}


def verdict(v, **kw):
    return {"verdict": {"name": R, "verdict": v, **kw}}


def js(code):
    return {"js": {"name": R, "code": code}}


def tap(choice="confirm"):
    return {"tap": {"name": R, "choice": choice}}


def landed(path, timeout=10):
    """Wait, bounded, until a request to ``path`` (path and query) has completed in any frame of
    the page: a fetch's resource timing entry, in the main frame or a child frame (cross-site
    ones included), or a frame now at that address (a form's navigation). A press's request
    leaves the page after the action reports ``done``; the scenario's end withdraws the
    browser's connections, so a request still in transit then never reaches the fixture. It
    returns on the first such request, so a caller that pins exactly one keeps the file's
    observation margin (``MARGIN``) after it for a duplicate to land in. The count is still
    asserted on what the fixture received: a press that sends nothing waits out the bound and
    counts zero."""
    return {"landed": {"path": path, "timeout": timeout}}


MARGIN = {"sleep": 0.5}


OBS = {"observe": {"name": R}}
PAY_FORM = ('<main><h1>Checkout</h1><section id="sum"><p id="total">Total € 84,20</p></section>'
            '<form id="f" action="/pay?quote=Q84" method="post"><label>Note '
            '<input name="note" value=""></label>'
            f'<button type="button" id="pay" data-k="pay" onclick="{SUBMIT}">Pay now</button>'
            '</form></main>')


# --------------------------------------------------------------------------- the tap (F3–F8)

def test_1_amount_changes_while_the_question_waits(tmp_path):
    out, req = gate_run(tmp_path, page(PAY_FORM), [
        act("click", "Pay now"), verdict(ASK),
        js("() => { document.getElementById('total').textContent = 'Total € 90,20'; }"),
        tap(), verdict(CHANGED)])
    judged, asked, _, rejudge, lapsed = out
    assert judged["outcome"] == "judgement_required" and asked["outcome"] == "asked"
    diff = packet(rejudge)["difference"]
    assert diff["blocks_changed"] == [{"before": "Total € 84,20", "after": "Total € 90,20"}]
    assert lapsed["outcome"] in ("judgement_required", "lapsed")
    assert hits(req, "/submit") == []


def test_2_lost_reply_then_duplicate_tap(tmp_path):
    out, req = gate_run(tmp_path, page(PAY_FORM), [
        act("click", "Pay now"), verdict(ASK), tap(), tap(),
        {"verdict": {"name": R, "verdict": SAME, "keep": True}},
        {"verdict": {"name": R, "verdict": SAME}}, landed("/submit?pay"), MARGIN])
    _, _, first, again, done, replay, _, _ = out
    assert again == {**first, "repeat": True}
    assert done["outcome"] == "done" and replay == {**done, "repeat": True}
    assert len(hits(req, "/submit?pay")) == 1


FRAMED = PAY_FORM + '<iframe id="fr" src="/frame1" title="ad"></iframe>'


@pytest.mark.parametrize("matches", [1, 2])
def test_3_generation_change_while_waiting(tmp_path, matches):
    extra = ("" if matches == 1 else
             "const b = document.getElementById('pay').cloneNode(true); b.id = 'pay2';"
             "document.getElementById('f').appendChild(b);")
    out, req = gate_run(tmp_path, page(FRAMED), [
        act("click", "Pay now"), verdict(ASK),
        js("() => { " + extra + "document.getElementById('fr').src = '/frame2'; }"),
        {"sleep": 1}, tap(), *([verdict(SAME), landed("/submit?pay"), MARGIN] if matches == 1
                             else [])])
    if matches == 1:
        assert out[-4]["outcome"] == "rejudgement_required" and out[-3]["outcome"] == "done"
        assert len(hits(req, "/submit?pay")) == 1
    else:
        assert out[-1]["outcome"] == "lapsed"
        assert hits(req, "/submit") == []


def test_4_overlay_over_the_point_is_moved(tmp_path):
    out, req = gate_run(tmp_path, page(PAY_FORM), [
        act("click", "Pay now"),
        js("() => { const d = document.createElement('div'); d.id = 'veil';"
           "d.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:9';"
           "document.body.appendChild(d); }"),
        verdict(GO)])
    assert out[-1]["outcome"] == "not_run" and out[-1]["reason"] == "moved"
    assert hits(req, "/submit") == []


TOTAL_ASIDE = PAY_FORM + '<aside><p id="order">Order total € 84,20</p></aside>'


def test_5_order_summary_total_outside_the_form_changes(tmp_path):
    """The change after the judged reading: a scripted ``same`` still runs nothing (the final
    read differs) and the re-judgement runs again."""
    out, req = gate_run(tmp_path, page(TOTAL_ASIDE), [
        act("click", "Pay now"), verdict(ASK), tap(),
        js("() => { document.getElementById('order').textContent = 'Order total € 96,20'; }"),
        verdict(SAME)])
    assert out[-1]["outcome"] == "rejudgement_required"
    assert packet(out[-1])["difference"]["blocks_changed"] == [
        {"before": "Order total € 84,20", "after": "Order total € 96,20"}]
    assert hits(req, "/submit") == []


def test_6_surcharge_block_appears(tmp_path):
    out, req = gate_run(tmp_path, page(PAY_FORM), [
        act("click", "Pay now"), verdict(ASK), tap(),
        js("() => { const p = document.createElement('p'); p.textContent = 'Surcharge € 4,00';"
           "document.getElementById('sum').appendChild(p); }"),
        verdict(SAME)])
    assert out[-1]["outcome"] == "rejudgement_required"
    assert "Surcharge € 4,00" in packet(out[-1])["difference"]["blocks_added"]
    assert hits(req, "/submit") == []


def test_7_counter_in_the_buttons_block(tmp_path):
    """Ruling 8: a counter ticking beside the button makes the final read differ; the
    re-judgement runs twice, then a new question follows."""
    html = page(PAY_FORM.replace('Pay now</button>', 'Pay now</button><span id="c">1</span>'),
                "let n = 1; setInterval(() => { document.getElementById('c').textContent = ++n; }, 150);")
    out, req = gate_run(tmp_path, html, [
        act("click", "Pay now"), verdict(ASK), tap(), verdict(SAME), verdict(SAME),
        verdict(ASK)])
    assert [o["outcome"] for o in out[-4:]] == ["rejudgement_required", "rejudgement_required",
                                                "judgement_required", "asked"]
    assert out[-1]["question_id"] != out[1]["question_id"]
    assert hits(req, "/submit") == []


def test_8_label_and_handler_change_during_pointer_movement(tmp_path):
    """F4 after a go: the button's label and handler change as the pointer arrives; the
    guard's re-read sees it, nothing is pressed, and the step is judged afresh."""
    html = page(PAY_FORM.replace('Pay now</button>', 'Download PDF</button>'),
                "const b = document.getElementById('pay'); let done = false;"
                "b.addEventListener('mousemove', () => { if (done) return; done = true;"
                "b.textContent = 'Pay € 90,00'; b.dataset.k = 'pay90'; });")
    out, req = gate_run(tmp_path, html, [act("click", "Download PDF"), verdict(GO)])
    assert out[-1]["outcome"] == "judgement_required"
    assert packet(out[-1])["target"]["name"] == "Pay € 90,00"
    assert hits(req, "/submit") == []


REFUND = PAY_FORM + ('<fieldset><legend>Refund to</legend><label><input type="radio" name="rf" '
                     'id="card" checked> Card</label><label><input type="radio" name="rf" '
                     'id="credit"> Store credit</label></fieldset>')


def test_9_refund_radio_outside_the_form_switched(tmp_path):
    out, req = gate_run(tmp_path, page(REFUND), [
        act("click", "Pay now"), verdict(ASK), tap(),
        js("() => { document.getElementById('credit').checked = true; }"), verdict(SAME)])
    assert out[-1]["outcome"] == "rejudgement_required"
    changed = packet(out[-1])["difference"]["controls_changed"]
    assert any("Store credit" in c["after"] and '"checked": true' in c["after"] for c in changed)
    assert hits(req, "/submit") == []


LONG = "".join(f"<p>Terms paragraph {i}: " + "lorem ipsum dolor sit amet " * 12 + "</p>"
               for i in range(80))


def test_10_surcharge_past_the_packet_cap(tmp_path):
    out, req = gate_run(tmp_path, page(PAY_FORM + LONG), [
        act("click", "Pay now"), verdict(ASK),
        js("() => { const p = document.createElement('p'); p.textContent = 'Surcharge € 4,00';"
           "document.body.appendChild(p); }"),
        tap(), verdict(CHANGED)])
    rejudge = packet(out[3])
    assert rejudge["current_packet"]["page"]["page_truncated"] is True
    assert "Surcharge € 4,00" not in json.dumps(rejudge["current_packet"], ensure_ascii=False)
    assert rejudge["difference"]["blocks_added"] == ["Surcharge € 4,00"]
    assert hits(req, "/submit") == []


def test_11_surcharge_during_the_rejudgement_round_trip(tmp_path):
    out, req = gate_run(tmp_path, page(PAY_FORM), [
        act("click", "Pay now"), verdict(ASK), tap(), {"sleep": 1},
        js("() => { const p = document.createElement('p'); p.textContent = 'Surcharge € 4,00';"
           "document.getElementById('sum').appendChild(p); }"),
        verdict(SAME)])
    assert out[-1]["outcome"] == "rejudgement_required" and hits(req, "/submit") == []


def test_12_total_and_counter_in_one_block(tmp_path):
    html = page(PAY_FORM.replace('<p id="total">Total € 84,20</p>',
                                 '<p id="total">Total € 84,20 · updated <b id="c">1</b> s ago</p>'),
                "let n = 1; setInterval(() => { document.getElementById('c').textContent = ++n; }, 150);")
    out, req = gate_run(tmp_path, html, [
        act("click", "Pay now"), verdict(ASK),
        js("() => { document.getElementById('total').firstChild.textContent = 'Total € 91,20 · updated '; }"),
        tap(), verdict(SAME)])
    assert any("€ 91,20" in c["after"] for c in packet(out[3])["difference"]["blocks_changed"])
    assert out[-1]["outcome"] == "rejudgement_required" and hits(req, "/submit") == []


def test_13_link_query_changes_with_the_same_masked_path(tmp_path):
    """Signed in: a link whose query changes while its masked path stays the same is a
    changed destination (app-private), whatever the re-judgement says."""
    account = (b'<title>A</title><main><p>Invoice</p>'
               b'<a id="l" href="https://site-a.example/pay?quote=Q84">Betalen</a></main>')
    out, req = gate_run(tmp_path, b"", [
        act("click", "Betalen"), verdict(ASK),
        js("() => { document.getElementById('l').href = 'https://site-a.example/pay?quote=Q90'; }"),
        tap(), verdict(SAME)], account_html=account)
    rejudge = packet(out[3])
    assert rejudge["destination_changed"] is True
    assert rejudge["current_packet"]["target"]["destination"]["path"] == "/pay"
    assert out[-1]["outcome"] in ("judgement_required", "lapsed")
    assert hits(req, "/pay") == []


FORMACTION = ('<main><p>Invoice Q84 € 84,20</p><form id="f" action="/msg" method="post">'
              '<button id="b" formaction="/pay?quote=Q84">Factuur downloaden</button></form>'
              '<form id="g" action="/pay?quote=Q84" method="post"></form>'
              '<button id="o" form="g">Betalen</button></main>')


def test_14_formaction_override_changes(tmp_path):
    out, req = gate_run(tmp_path, page(FORMACTION), [
        act("click", "Factuur downloaden"), verdict(ASK),
        js("() => { document.getElementById('b').setAttribute('formaction', '/pay?quote=Q90'); }"),
        tap(), verdict(SAME)])
    assert packet(out[3])["destination_changed"] is True
    assert out[-1]["outcome"] in ("judgement_required", "lapsed") and hits(req, "/pay") == []


def test_15_form_associated_submitter_whose_owner_changes(tmp_path):
    out, req = gate_run(tmp_path, page(FORMACTION), [
        act("click", "Betalen"), verdict(ASK),
        js("() => { document.getElementById('g').action = '/pay?quote=Q90'; }"),
        tap(), verdict(SAME)])
    assert packet(out[3])["destination_changed"] is True
    assert out[-1]["outcome"] in ("judgement_required", "lapsed") and hits(req, "/pay") == []


MSG = ('<main><p id="total">Total € 84,20</p><form id="m" action="/msg" method="post">'
       '<label>Bericht <input id="t" name="t" autocomplete="off"></label>'
       '<button>Versturen</button></form>'
       '<button type="button" id="del" data-k="del" onclick="' + SUBMIT + '">Delete account</button></main>')


def test_16_type_enter_question_whose_form_action_changes_while_typing(tmp_path):
    html = page(MSG, "let n = 0; document.getElementById('t').addEventListener('keydown', () => {"
                     "if (++n === 2) document.getElementById('m').action = '/delete'; });")
    out, req = gate_run(tmp_path, html, [
        act("type", "Bericht", text="Klopt dit?", enter=True), verdict(ASK_CHANGE), tap(),
        verdict(SAME), verdict(SAME)])
    typed = out[3]
    assert typed["outcome"] == "rejudgement_required"
    assert packet(typed)["destination_changed"] is True
    assert out[-1]["outcome"] in ("judgement_required", "lapsed")
    assert hits(req, "/msg") == [] and hits(req, "/delete") == []


def test_17_type_enter_question_whose_total_changes_while_typing(tmp_path):
    html = page(MSG, "let n = 0; document.getElementById('t').addEventListener('keydown', () => {"
                     "if (++n === 2) document.getElementById('total').textContent = 'Total € 99,20'; });")
    out, req = gate_run(tmp_path, html, [
        act("type", "Bericht", text="Klopt dit?", enter=True), verdict(ASK_CHANGE), tap(),
        verdict(SAME), verdict(CHANGED)])
    typed = out[3]
    assert {"before": "Total € 84,20", "after": "Total € 99,20"} in \
        packet(typed)["difference"]["blocks_changed"]
    assert hits(req, "/msg") == []


def test_type_replaces_an_input_or_textarea_and_stays_at_a_contenteditable_caret(tmp_path):
    """Real Chrome, the real pacer: 1 then 3 is 3, an empty text clears, a contenteditable keeps
    what it holds."""
    html = page('<label>Qty <input id="q" value="1"></label>'
                '<label>Phone <input id="m" type="tel" value="111"></label>'
                '<label>Note <textarea id="n">old note</textarea></label>'
                '<label id="sl">Sig</label><div id="s" contenteditable="true" aria-labelledby="sl" '
                'style="min-height:20px">kept</div>')
    out, _req = gate_run(tmp_path, html, [
        act("type", "Qty", text="3"), act("type", "Phone", text="222"),
        act("type", "Note", text=""), act("type", "Sig", text="!"),
        js("() => ['q', 'm', 'n'].map((i) => document.getElementById(i).value)"),
        js("() => document.getElementById('s').innerText")])
    assert [r["typed"] for r in out[:4]] == ["shown"] * 4, out[:4]
    assert out[4] == ["3", "222", ""]
    assert out[5].replace("\n", "") in ("kept!", "!kept")


def test_16b_tapped_type_enter_runs_once(tmp_path):
    out, req = gate_run(tmp_path, page(MSG), [
        act("type", "Bericht", text="Klopt dit?", enter=True), verdict(ASK_CHANGE), tap(),
        verdict(SAME), verdict(SAME), landed("/msg"), MARGIN])
    assert out[-3]["outcome"] == "done" and len(hits(req, "/msg")) == 1


@pytest.mark.parametrize("when", ["while typing a space", "between typing and Enter"])
def test_18_focus_moves_to_another_button(tmp_path, when):
    script = ("const t = document.getElementById('t'), d = document.getElementById('del');"
              + ("let n = 0; t.addEventListener('keyup', () => { if (++n === 1) setTimeout(() => d.focus(), 0); });"
                 if when == "while typing a space" else
                 "t.addEventListener('input', () => { if (t.value === 'a b') setTimeout(() => d.focus(), 0); });"))
    out, req = gate_run(tmp_path, page(MSG, script), [
        act("type", "Bericht", text="a b", enter=True), verdict(GO), {"sleep": 1}])
    assert out[1]["outcome"] in ("judgement_required", "not_run")
    assert hits(req, "/submit?del") == [] and hits(req, "/msg") == []


@pytest.mark.parametrize("text,want", [
    ("a\nb", {"outcome": "not_run", "reason": "one_line"}),
    ("ab\r", {"outcome": "not_run", "reason": "one_line"}),
    ("a\r\nb", {"outcome": "not_run", "reason": "one_line"}),
    ("a\tb", {"error": "unsupported"})], ids=["LF", "CR", "CRLF", "Tab"])
def test_19_21_control_characters_refused_before_any_key(tmp_path, text, want):
    """S2-4 rule 1 with slice 6b: a line break into a one-line field does not run (nothing
    judged, no key), any other control character is refused by the parser."""
    out, req = gate_run(tmp_path, page(MSG), [
        act("type", "Bericht", text=text, raw=True), {"sleep": 0.5},
        js("() => document.getElementById('t').value")])
    assert {k: out[0].get(k) for k in want} == want, out[0]
    assert out[-1] == "" and hits(req, "/msg") == []


def test_type_into_a_code_or_card_field_is_refused(tmp_path):
    """A value a packet never shows is never typed by the agent (the real READ's secret rule)."""
    html = page('<main><label>Code <input autocomplete="one-time-code"></label>'
                '<label>Card <input autocomplete="cc-number"></label></main>')
    out, req = gate_run(tmp_path, html, [
        act("type", "Code", text="938271"), act("type", "Card", text="4111111111111111"),
        js("() => [...document.querySelectorAll('input')].map(e => e.value)")])
    assert out[0] == {"error": "unsupported"} and out[1] == {"error": "unsupported"}
    assert out[-1] == ["", ""]


def test_22_type_space_at_a_button_and_a_link(tmp_path):
    html = page(PAY_FORM + '<a id="ln" href="/delete">Delete link</a>')
    out, req = gate_run(tmp_path, html, [
        act("type", "Pay now", text=" "), act("type", "Delete link", text=" "), {"sleep": 0.5},
        js("() => location.pathname")])
    assert out[0] == {"error": "unsupported"} and out[1] == {"error": "unsupported"}
    assert out[-1] == "/pub" and hits(req, "/submit") == [] and hits(req, "/delete") == []


NUMBER = ('<main><p>Settings</p><label>Quantity <input id="q" type="number" value="10" '
          'oninput="fetch(\'/save?\' + this.value, {method: \'POST\'})"></label>'
          + "".join(f"<p>Line {i} " + "text " * 30 + "</p>" for i in range(60)) + '</main>')


def test_23_scroll_never_edits_a_focused_number_input(tmp_path):
    html = page(NUMBER, "document.getElementById('q').style.cssText = "
                        "'position:fixed;left:0;top:0;width:100vw;height:80vh;font-size:60px';")
    out, req = gate_run(tmp_path, html, [
        js("() => { document.getElementById('q').focus(); }"),
        act("scroll", "__page__", direction="up", amount="step"),
        act("scroll", "__page__", direction="down", amount="page"), {"sleep": 0.5},
        js("() => document.getElementById('q').value")])
    assert out[1]["outcome"] == "done" and out[2]["outcome"] == "done"
    assert out[-1] == "10" and hits(req, "/save") == []


def test_24_public_link_to_a_registered_account_site_is_judged(tmp_path):
    html = page('<p>Your subscription</p><a id="c" href="https://site-a.example/subscription/'
                'cancel?confirm=1">Cancel subscription</a>')
    out, req = gate_run(tmp_path, html, [
        act("navigate", "Cancel subscription"), OBS, act("click", "Cancel subscription")])
    assert out[0]["outcome"] == "judgement_required"
    assert out[-1]["outcome"] == "judgement_required"
    assert packet(out[0])["where"]["session"] == "signed_in"
    assert hits(req, "/subscription") == []


STACK = ('<main>' + "".join(
    f'<p>Gap {i}</p><input type="number" id="n{i}" value="{i}" style="display:block;'
    f'height:90px;font-size:40px;width:80vw" oninput="fetch(\'/save?{i}\', {{method: \'POST\'}})">'
    for i in range(40)) + '</main>')


def test_25_multi_step_scroll_over_number_inputs(tmp_path):
    out, req = gate_run(tmp_path, page(STACK), [
        js("() => { document.getElementById('n1').focus(); }"),
        act("scroll", "__page__", direction="down", amount="page"),
        act("scroll", "__page__", direction="down", amount="page"), {"sleep": 0.5},
        js("() => [...document.querySelectorAll('input')].every((e, i) => e.value === String(i))")])
    assert out[-1] is True and hits(req, "/save") == []


SELECT = ('<main><p>Top-up</p><label>Amount <select id="s" disabled '
          'onchange="fetch(\'/save?\' + this.value, {method: \'POST\'})">'
          '<option value="0">Choose</option><option id="o" value="10">€ 10</option></select></label></main>')


def test_26_disabled_select_enabled_later_with_a_changed_option(tmp_path):
    html = page(SELECT, "setTimeout(() => { const s = document.getElementById('s'),"
                        "o = document.getElementById('o'); s.disabled = false;"
                        "o.textContent = '€ 90'; o.value = '90'; }, 2500);")
    out, req = gate_run(tmp_path, html, [
        act("select", "Amount", choice="€ 10", plan="Top up"), verdict(GO), {"sleep": 3}])
    assert out[1]["outcome"] in ("judgement_required", "not_run")
    assert hits(req, "/save") == []


def test_27_option_disabled_after_the_guard_then_reenabled_at_90(tmp_path):
    html = page(SELECT.replace(' disabled', ''),
                "const o = document.getElementById('o');"
                "document.getElementById('s').addEventListener('focus', () => {});"
                "window.flip = () => { o.disabled = true; setTimeout(() => { o.disabled = false;"
                "o.textContent = '€ 90'; o.value = '90'; }, 30); };")
    out, req = gate_run(tmp_path, html, [
        act("select", "Amount", choice="€ 10", plan="Top up"), js("() => window.flip()"), verdict(GO),
        {"sleep": 1}])
    assert [r for r in hits(req, "/save") if "90" in r[2]] == []


PERIOD = ('<main><p>Orders</p><label>Period <select id="p" onchange="fetch(\'/save?\' + '
          'this.value, {method: \'POST\'})"><option value="2026">2026</option>'
          '<option value="2025">2025</option></select></label></main>')


def test_28_collapsed_period_select_selects_once(tmp_path):
    out, req = gate_run(tmp_path, page(PERIOD), [
        act("select", "Period", choice="2025"), landed("/save?2025"), MARGIN,
        js("() => document.getElementById('p').value")])
    assert out[0]["outcome"] == "done" and out[-1] == "2025"
    assert [r[2] for r in hits(req, "/save")] == ["/save?2025"]


# --------------------------------------------------------------------------- the packet, for real

SECRETS = ('<main><h1>Payment</h1><form action="/pay?quote=Q84SECRET" method="post">'
           '<label>Card <input autocomplete="cc-number" value="4111111111111111"></label>'
           '<label>Code <input autocomplete="one-time-code" value="938271"></label>'
           '<label>Password <input type="password" value="hunter2-secret"></label>'
           '<button formaction="/pay?quote=FA-QUERY">Pay</button></form>'
           '<label>Expiry <select autocomplete="cc-exp-year"><option>2031</option></select></label>'
           '<label>Bank <select><option>https://bank.example/pay?token=SELQUERY</option></select></label>'
           '<a href="/doc?token=LINKQUERY">Doc</a></main>')


def test_real_reading_keeps_secrets_and_exact_destinations_out_of_the_packet(tmp_path):
    out, req = gate_run(tmp_path, page(SECRETS), [act("click", "Pay")])
    text = out[0]["judgement"]["packet"]
    for secret in ("4111111111111111", "938271", "hunter2-secret", "Q84SECRET", "FA-QUERY",
                   "LINKQUERY", "2031", "SELQUERY"):
        assert secret not in text, secret
    p = json.loads(text)
    assert p["target"]["submits_form"]["destination"]["path"] == "/pay"
    assert text.count("[not shown]") >= 3


def test_record_row_in_the_packet(tmp_path):
    table = ('<main><h1>Facturen</h1><table><tr><th>Factuur</th><th>Bedrag</th><th></th></tr>'
             '<tr><td>Factuur oktober 2026</td><td>€ 84,20</td><td><button>Download PDF</button></td></tr>'
             '<tr><td>Factuur september 2026</td><td>€ 79,60</td><td><button id="sep">Download PDF'
             '</button></td></tr></table></main>')
    out, req = gate_run(tmp_path, page(table), [
        js("() => { document.querySelectorAll('button')[0].textContent = 'Download oktober'; }"),
        OBS, act("click", "Download PDF")])
    blocks = [b["text"] for b in packet(out[-1])["page"]["blocks"]]
    assert blocks[:3] == ["Download PDF", "Factuur september 2026", "€ 79,60"]


# --------------------------------------------------------------------------- round r1's cases

def test_r1_link_changed_since_observation_is_not_opened(tmp_path):
    """The handle's address and the page's differ: the step is stale, nothing is judged or
    opened (the judged destination is the one executed)."""
    account = (b'<title>A</title><main><a id="l" href="https://site-a.example/delete">'
               b'Delete account</a></main>')
    out, req = gate_run(tmp_path, b"", [
        js("() => { const l = document.getElementById('l'); l.href = 'https://site-a.example/invoices';"
           "l.textContent = 'Read invoices'; }"),
        act("navigate", "Delete account"), {"sleep": 0.5}], account_html=account)
    assert out[1] == {"error": "stale_handle"}
    assert hits(req, "/delete") == [] and hits(req, "/invoices") == []


def test_r1_overlay_over_an_iframe_target_is_moved(tmp_path):
    frame = page(f'<button id="r" data-k="read" onclick="{SUBMIT}">Read invoices</button>')
    html = page('<main><p>Invoices</p><iframe id="fr" src="/frame1" style="width:600px;height:200px"></iframe></main>')
    out, req = gate_run(tmp_path, html, [
        {"sleep": 1}, OBS, act("click", "Read invoices"),
        js("() => { const b = document.createElement('button'); b.textContent = 'Delete account';"
           "b.dataset.k = 'del'; b.onclick = function () { fetch('/submit?del', {method: 'POST'}); };"
           "const r = document.getElementById('fr').getBoundingClientRect();"
           "b.style.cssText = `position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;z-index:9`;"
           "document.body.appendChild(b); }"),
        verdict(GO), {"sleep": 0.5}], pages={"/frame1": frame})
    assert out[-2]["outcome"] == "not_run" and out[-2]["reason"] == "moved"
    assert hits(req, "/submit") == []


def test_r1_free_search_typing_rechecks_the_field(tmp_path):
    html = page('<main><label>Zoek <input id="q" type="search"></label>'
                '<label><input type="checkbox" id="c" onchange="fetch(\'/submit?consent\', {method: \'POST\'})"> '
                'Consent</label></main>',
                "const q = document.getElementById('q'); let done = false;"
                "q.addEventListener('mousemove', () => { if (done) return; done = true; q.type = 'checkbox';"
                "q.onchange = () => fetch('/submit?consent', {method: 'POST'}); });")
    out, req = gate_run(tmp_path, html, [act("type", "Zoek", text=" "), {"sleep": 0.5}])
    assert out[0].get("outcome") != "done"
    assert hits(req, "/submit") == []


def test_r1_scroll_never_wheels_over_a_frame(tmp_path):
    frame = page('<input id="n" type="number" value="10" style="width:100vw;height:100vh;font-size:80px" '
                 'oninput="fetch(\'/save?\' + this.value, {method: \'POST\'})">'
                 '<script>document.getElementById("n").focus();</script>')
    html = page('<main><iframe id="fr" src="/frame1" style="position:fixed;inset:0;width:100vw;height:85vh;border:0"></iframe>'
                + "".join(f"<p>Line {i} " + "text " * 30 + "</p>" for i in range(80)) + '</main>')
    out, req = gate_run(tmp_path, html, [
        {"sleep": 1}, act("scroll", "__page__", direction="up", amount="step"),
        act("scroll", "__page__", direction="down", amount="page"), {"sleep": 0.5}],
        pages={"/frame1": frame})
    assert hits(req, "/save") == []


def test_r1_a_fee_in_a_child_frame_is_in_the_final_read(tmp_path):
    frame = page('<p id="fee">No extra fees</p>')
    html = page(PAY_FORM + '<iframe id="fr" src="/frame1" style="width:400px;height:80px"></iframe>')
    out, req = gate_run(tmp_path, html, [
        {"sleep": 1}, OBS, act("click", "Pay now"), verdict(ASK), tap(),
        js("() => { document.getElementById('fr').contentDocument.getElementById('fee').textContent ="
           " 'Additional fee € 90,00'; }"),
        verdict(SAME), {"sleep": 0.5}], pages={"/frame1": frame})
    assert out[-2]["outcome"] == "rejudgement_required"
    assert {"before": "No extra fees", "after": "Additional fee € 90,00"} in \
        packet(out[-2])["difference"]["blocks_changed"]
    assert hits(req, "/submit") == []


# --------------------------------------------------------------------------- round r2's cases

SCALED = (f'<p>Invoices</p><button id="r" data-k="read" onclick="{SUBMIT}" style="width:200px;height:60px">'
          'Read invoices</button>'
          f'<button id="d" data-k="del" onclick="{SUBMIT}" style="position:absolute;left:60px;top:40px;'
          'width:200px;height:60px">Delete account</button>')


@pytest.mark.parametrize("transform", ["scale(2)", "rotate(20deg)"])
def test_r2_hit_test_through_a_transformed_frame(tmp_path, transform):
    """A scaled frame maps the point through its scale; a rotated one is refused. Either way
    the press lands on the judged control or nothing is pressed — never on another."""
    html = page(f'<main><p>Invoices</p><iframe id="fr" src="/frame1" style="width:500px;height:300px;'
                f'transform:{transform};transform-origin:0 0;border:0"></iframe></main>')
    out, req = gate_run(tmp_path, html, [
        {"sleep": 1}, OBS, act("click", "Read invoices"), verdict(GO), landed("/submit?read"),
        MARGIN], pages={"/frame1": page(SCALED)})
    assert hits(req, "/submit?del") == []
    assert len(hits(req, "/submit?read")) == (1 if out[-3]["outcome"] == "done" else 0)


def test_r2_a_change_past_a_values_first_200_characters_is_shown(tmp_path):
    html = page(PAY_FORM + '<textarea id="t" style="width:600px;height:80px">'
                + "Terms and conditions apply. " * 10 + 'I accept a fee of 10 euros.</textarea>')
    out, req = gate_run(tmp_path, html, [
        act("click", "Pay now"), verdict(ASK),
        js("() => { const t = document.getElementById('t'); t.value = t.value.replace('10 euros', '900 euros'); }"),
        tap(), verdict(CHANGED)])
    changed = packet(out[3])["difference"]["controls_changed"]
    assert any("900 euros" in c["after"] for c in changed)
    assert hits(req, "/submit") == []


def test_r2_option_labels_are_masked_and_card_selects_refused(tmp_path):
    html = page('<main><label>Expiry <select autocomplete="cc-exp-year"><option>2030</option>'
                '<option>2031</option></select></label><label>Bank <select><option>Choose</option>'
                '<option>https://bank.example/pay?token=SELQUERY</option></select></label></main>')
    out, req = gate_run(tmp_path, html, [
        OBS,
        act("select", "Expiry", choice="bank.example"),
        act("select", "Bank", choice="bank.example", plan="Choose a bank")])
    # A card select lists no options, so no handle names its choice; another select's option
    # borrowed for it is refused.
    assert [lb for lb in out[0]["labels"] if "2030" in lb or "2031" in lb] == []
    assert "2031" not in out[0]["text"]
    assert out[1] == {"error": "unsupported"}
    assert "SELQUERY" not in out[2]["judgement"]["packet"]
    assert packet(out[2])["action"]["option"].startswith("https://bank.example")


# --------------------------------------------------------------------------- round r3's cases

@pytest.mark.parametrize("form", [
    '<form id="f" action="/read" method="post"><label>Zoek <input id="q" role="combobox"></label>'
    '<button id="s">Zoeken</button></form>',
    '<form id="f" action="/read" method="post"><label>Zoek <input id="q"></label>'
    '<input type="image" id="s" alt="Zoeken" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" '
    'style="width:20px;height:20px"></form>'], ids=["combobox-role", "image-submitter"])
def test_r3_enter_destination_of_every_text_input(tmp_path, form):
    """The Enter's destination is the form's real default button, whatever the input's role and
    when that button is an image input: a changed formaction sends no Enter."""
    out, req = gate_run(tmp_path, page(form), [
        act("type", "Zoek", text="invoice", enter=True),
        js("() => { document.getElementById('s').setAttribute('formaction', '/delete?confirmed=1'); }"),
        verdict(GO), {"sleep": 1}])
    assert packet(out[0])["target"]["enter_submits_form"]["destination"]["path"] == "/read"
    assert out[2]["outcome"] in ("judgement_required", "not_run")
    assert hits(req, "/delete") == [] and hits(req, "/read") == []


# --------------------------------------------------------------------------- the browser's hit test
# A cross-site frame is an out-of-process frame in Chrome.

XFRAME = (f'<p>Invoices</p><div style="height:%dpx"></div>'
          f'<button id="r" data-k="read" onclick="{SUBMIT}" style="width:200px;height:60px">Read invoices</button>'
          f'<button id="d" data-k="del" onclick="{SUBMIT}" style="position:absolute;left:60px;top:%dpx;'
          'width:200px;height:60px">Delete account</button><div style="height:1500px"></div>')


@pytest.mark.parametrize("style", ["scale:-1 1", "transform:scale(-1,1) rotate(8deg)", "none"])
def test_hit_through_a_cross_site_frame(tmp_path, style):
    """The browser's own answer through an out-of-process frame, mirrored by the scale property,
    rotated, or plain: never a press on "Delete"; a press is made only on "Read invoices"."""
    html = page(f'<main><p>Invoices</p><iframe id="fr" src="https://{C}/frame1" style="position:absolute;'
                f'left:700px;top:120px;width:500px;height:300px;border:0;transform-origin:0 0;{style}">'
                '</iframe></main>')
    out, req = gate_run(tmp_path, html, [
        {"sleep": 1.5}, OBS, act("click", "Read invoices"), verdict(GO), landed("/submit?read"),
        MARGIN], pages={"/frame1": page(XFRAME % (0, 40))})
    assert hits(req, "/submit?del") == []
    assert len(hits(req, "/submit?read")) == (1 if out[-3]["outcome"] == "done" else 0)
    if out[-3]["outcome"] != "done":
        assert out[-3]["reason"] == "moved"


def test_hit_in_a_scrolled_cross_site_frame_of_a_scrolled_page(tmp_path):
    """Both the page and the frame are scrolled: the press on the target is made (document
    coordinates per frame), once."""
    html = page('<main><div style="height:900px"></div><iframe id="fr" src="https://' + C + '/frame1" '
                'style="width:500px;height:300px;border:3px solid;padding:2px"></iframe>'
                '<div style="height:1500px"></div></main>')
    out, req = gate_run(tmp_path, html, [
        {"sleep": 1.5}, js("() => window.scrollTo(0, 700)"),
        js("() => { document.getElementById('fr').contentWindow.postMessage('scroll', '*'); }"),
        {"sleep": 0.5}, OBS, act("click", "Read invoices"), verdict(GO), landed("/submit?read"),
        MARGIN], pages={"/frame1": page(XFRAME % (1000, 2000),
                                        "addEventListener('message', () => scrollTo(0, 960));")})
    assert out[-3]["outcome"] == "done"
    assert len(hits(req, "/submit?read")) == 1 and hits(req, "/submit?del") == []


def test_hit_refuses_an_overlay_over_a_cross_site_frame(tmp_path):
    html = page(f'<main><iframe id="fr" src="https://{C}/frame1" style="position:absolute;left:50px;'
                'top:100px;width:500px;height:300px;border:0"></iframe>'
                f'<button id="veil" data-k="veil" onclick="{SUBMIT}" style="position:absolute;left:40px;'
                'top:90px;width:300px;height:120px">Delete account</button></main>')
    out, req = gate_run(tmp_path, html, [
        {"sleep": 1.5}, OBS, act("click", "Read invoices"), verdict(GO), {"sleep": 0.5}],
        pages={"/frame1": page(XFRAME % (0, 200))})
    assert out[-2]["outcome"] == "not_run" and out[-2]["reason"] == "moved"
    assert hits(req, "/submit") == []


def test_hit_ignores_a_page_element_named_like_the_check(tmp_path):
    """A same-process frame laid over the target holds an element whose id names a global the
    check might read: the press is refused, never delivered to that element."""
    over = page(f'<button id="__casaHitTarget" data-k="trap" onclick="{SUBMIT}" '
                'style="width:420px;height:260px">Delete account</button>')
    html = page(PAY_FORM + '<iframe src="/frame2" style="position:absolute;left:0;top:0;width:700px;'
                'height:400px;border:0;opacity:0.01"></iframe>')
    out, req = gate_run(tmp_path, html, [
        {"sleep": 1}, OBS, act("click", "Pay now"), verdict(GO), {"sleep": 0.5}],
        pages={"/frame2": over})
    assert out[-2]["outcome"] == "not_run" and out[-2]["reason"] == "moved"
    assert hits(req, "/submit") == []


def test_hit_accepts_slotted_and_pseudo_element_content(tmp_path):
    html = page('<style>#pb::before{content:"Invoice PDF";display:block;height:40px}</style><main>'
                f'<button id="pb" data-k="pdf" onclick="{SUBMIT}" style="width:300px;height:90px">Download</button>'
                '</main>')
    out, req = gate_run(tmp_path, html, [act("click", "Download"), verdict(GO),
                                         landed("/submit?pdf"), MARGIN])
    assert out[1]["outcome"] == "done" and len(hits(req, "/submit?pdf")) == 1


# --------------------------------------------------------------------------- round r4's cases

def test_r4_re_resolution_stays_in_the_targets_frame(tmp_path):
    """A child frame's target, the child navigated (same top-level address): a matching
    control in the main frame is never the re-resolved target."""
    child = page(f'<button data-k="read-child" onclick="{SUBMIT}">Open</button>')
    html = page(f'<main><button data-k="delete-main" onclick="{SUBMIT}">Open</button>'
                '<iframe id="fr" src="/frame1" style="width:400px;height:120px"></iframe></main>')
    out, req = gate_run(tmp_path, html, [
        {"sleep": 1}, OBS,
        {"act": {"name": R, "kind": "click", "label": "Open", "hold": True, "nth": 1}},
        js("() => { document.getElementById('fr').src = '/frame2'; }"), {"sleep": 1},
        verdict(GO), landed("/submit?read-child"), MARGIN],
        pages={"/frame1": child, "/frame2": child})
    assert hits(req, "/submit?delete-main") == []
    assert out[-3]["outcome"] in ("not_run", "judgement_required", "done")
    if out[-3]["outcome"] == "done":
        assert len(hits(req, "/submit?read-child")) == 1


def test_r4_enter_default_button_inside_a_shadow_root(tmp_path):
    html = page('<main><div id="host"></div></main>',
                "const r = document.getElementById('host').attachShadow({mode: 'open'});"
                "r.innerHTML = '<form action=\"/read\" method=\"post\"><label>Zoek <input id=q></label>"
                "<button id=s formaction=\"/read?account=one\">Zoeken</button></form>';")
    out, req = gate_run(tmp_path, html, [
        act("type", "Zoek", text="x", enter=True),
        js("() => { document.getElementById('host').shadowRoot.getElementById('s')"
           ".setAttribute('formaction', '/delete?account=one'); }"),
        verdict(GO), {"sleep": 1}])
    assert "account" not in out[0]["judgement"]["packet"]
    assert out[2]["outcome"] in ("judgement_required", "not_run")
    assert hits(req, "/delete") == [] and hits(req, "/read") == []


def test_r4_an_empty_form_action_is_the_current_page(tmp_path):
    html = page('<base href="https://pub-b.example/read"><main><form method="post">'
                '<label>Zoek <input id="q"></label><button>Zoeken</button></form></main>')
    out, req = gate_run(tmp_path, html, [act("type", "Zoek", text="x", enter=True)])
    assert packet(out[0])["target"]["enter_submits_form"]["destination"]["path"] == "/pub"


# --------------------------------------------------------------------------- A and B (after r5)
# A: the submission the browser would build is shown, frozen and compared; B: one secret rule.

def test_ab_enter_through_a_relabelled_default_button_is_not_sent(tmp_path):
    """r5: "View invoice" becomes "Delete invoice" (value view -> delete, same action) after the
    judgement: the Enter's frozen submission differs, no Enter is sent."""
    html = page('<main><form action="/i" method="post"><label>Factuur <input name="invoice"></label>'
                '<button id="b" name="operation" value="view">View invoice</button></form></main>')
    out, req = gate_run(tmp_path, html, [
        act("type", "Factuur", text="42", enter=True),
        js("() => { const b = document.getElementById('b'); b.value = 'delete'; b.textContent = 'Delete invoice'; }"),
        verdict(GO), {"sleep": 1}])
    sub = packet(out[0])["target"]["enter_submission"]
    assert sub["submitter"]["name"] == "View invoice" and ["operation", "view"] in sub["entries"]
    assert out[2]["outcome"] in ("judgement_required", "not_run") and hits(req, "/i") == []


def test_ab_an_added_recipient_is_in_the_difference(tmp_path):
    html = page('<main><form action="/send" method="post"><label>To <select name="to" multiple size="3">'
                '<option selected>alice</option><option>bob</option><option>carol</option></select></label>'
                '<button id="s">Send</button></form></main>')
    out, req = gate_run(tmp_path, html, [
        act("click", "Send"), verdict(ASK_CHANGE),
        js("() => { document.querySelector('option:nth-child(2)').selected = true; }"),
        tap(), verdict(CHANGED), {"sleep": 0.5}])
    diff = packet(out[3])["difference"]
    assert diff["submission_changed"]["after"][0]["entries"] == [["to", "alice"], ["to", "bob"]]
    assert any("bob" in json.dumps(c) for c in diff.get("controls_changed", []))
    assert hits(req, "/send") == []


def test_ab_a_buttonless_form_is_judged_and_its_enter_sent_once(tmp_path):
    """A form with one text field and no submit button submits on Enter (implicit submission):
    the Enter is judged as that submission, not refused, and sent once after a go."""
    html = page('<main><form action="/zoek" method="get"><label>Zoek <input name="q"></label></form></main>')
    out, req = gate_run(tmp_path, html, [
        act("type", "Zoek", text="factuur", enter=True), verdict(GO), landed("/zoek?q=factuur"),
        MARGIN])
    assert packet(out[0])["target"]["enter_submission"]["entries"] == [["q", "[the typed field]"]]
    assert out[1]["outcome"] == "done" and len(hits(req, "/zoek")) == 1


PASTE = "".join(f"item {i:05d}\n" for i in range(2_500)) + "trailing item zz"   # 27,516 characters
PASTE_PAGES = {
    "in-the-form": ('<main><form action="/save" method="post"><label>List '
                    '<textarea name="list" style="width:600px;height:200px"></textarea></label>'
                    '<button>Save</button></form></main>'),
    # no form owner: the page's own script sends what the field holds
    "no-form-owner": ('<main><label>List <textarea id="l" style="width:600px;height:200px">'
                      '</textarea></label><button type="button" onclick="fetch(\'/save\', '
                      '{method: \'POST\', body: document.getElementById(\'l\').value})">Save</button>'
                      '</main>'),
}


@pytest.mark.parametrize("case", list(PASTE_PAGES))
def test_a_long_paste_reaches_the_check_whole_exactly_once(tmp_path, case):
    """Astra S1 (run-r1), in Chrome with the gate's own READ: a 27,516-character list typed into
    a textarea (one insertion, sending nothing) is in the Save press's packet whole: its trailing
    item exactly once, with or without a form owner. Nothing is sent before a verdict."""
    out, req = gate_run(tmp_path, page(PASTE_PAGES[case]), [
        act("type", "List", text=PASTE),
        js("() => document.querySelector('textarea').value"),
        act("click", "Save")])
    assert out[0]["outcome"] == "done", out[0]
    assert out[1] == PASTE
    text = out[2]["judgement"]["packet"]
    assert text.count("trailing item zz") == 1, case
    assert text.count("item 01250") == 1 and text.count("item 02499") == 1
    assert hits(req, "/save") == []


ROWS_PAGE = ('<main><nav><ul><li><a href="/x">Menu</a></li></ul></nav><table>'
             + "".join(f'<tr><td>Line {i}</td><td><label>Qty {i} <input value="{i}"></label></td>'
                       f'<td><input type="checkbox" aria-label="Keep {i}"{" checked" if i % 2 else ""}>'
                       f'</td><td><button>Save {i}</button></td></tr>' for i in range(450))
             + '<tr><td>Secret row</td><td><input type="password" aria-label="Pin" value="PW445566">'
               '</td></tr></table><div hidden><ul><li>Hidden item</li></ul></div></main>')


def test_rows_read_every_record_with_its_field_values_past_the_control_cap(tmp_path):
    """The design's rows reading in Chrome: one JSON line per visible record holding no other,
    with what each field holds (past the 400-control cap, which page text never shows), its
    buttons, a secret value never, a hidden record not at all."""
    out, _req = gate_run(tmp_path, page(ROWS_PAGE), [{"read": {"name": R, "rows": True}}])
    got = out[0]
    lines = [json.loads(line) for line in got["text"].split("\n")]
    assert got["last"]["complete"] is True
    assert len(lines) == 452, len(lines)                  # the menu item, 450 rows, the secret row
    assert lines[0] == {"text": "Menu", "fields": [], "press": ["Menu"]}
    assert lines[450] == {"text": "Line 449 Qty 449 Save 449", "fields": [["Qty 449", "449"],
                                                                ["Keep 449", True]],
                          "press": ["Save 449"]}
    # a secret field is named as an observation names it: never by what the page puts on it
    assert lines[451]["fields"] == [["", "[not shown]"]]
    assert "PW445566" not in got["text"] and "Hidden item" not in got["text"]


def test_rows_join_a_value_split_across_spans_before_masking(tmp_path):
    """A token the page splits across inline spans reaches masking whole, as the gate's own
    reader joins it, so neither half reaches the pad; cells still read apart."""
    html = page('<main><ul><li>Token: <span>ghp_</span><span>abcdefghijklmnopqrstuvwxyzABCDEF</span></li>'
                '<li><span>ghp_</span><div>x</div><span>abcdefghijklmnopqrstuvwxyzABCDEF</span></li>'
                '</ul><table><tr><td>Alpha</td><td>Beta</td></tr></table></main>')
    out, _req = gate_run(tmp_path, html, [{"read": {"name": R, "rows": True}}])
    text = out[0]["text"]
    # The second item has a block between the halves: its own text still joins, as an
    # observation joins it.
    assert text.count("[masked]") == 2, text
    assert "abcdefghijklmnop" not in text and "ghp_" not in text
    assert '"Alpha Beta"' in text, text


def test_rows_read_a_record_that_is_a_field_or_hosts_its_own_shadow_root(tmp_path):
    """A record that is itself editable, and a record whose content lives in its own shadow
    root, are read whole: none of their values is silently dropped."""
    html = page('<main><ul><li contenteditable="true">MARKER-EDIT-1</li></ul>'
                '<div role="row" id="h"></div></main>'
                '<script>const s = document.getElementById("h").attachShadow({mode: "open"});'
                's.innerHTML = "<span>MARKER-SHADOW-2</span> <input aria-label=Qty value=MARKER-INPUT-3>";'
                '</script>')
    out, _req = gate_run(tmp_path, html, [{"read": {"name": R, "rows": True}}])
    text = out[0]["text"]
    for marker in ("MARKER-EDIT-1", "MARKER-SHADOW-2", "MARKER-INPUT-3"):
        assert text.count(marker) == 1, (marker, text)


def test_rows_are_the_observations_reading_grouped_by_record(tmp_path):
    """Rows read what an observation reads, by the same code: a select shows its option's label,
    masked; nothing a record holds is lost — a record with no box of its own, a list editable
    through its parent, a parent's own text around a nested record — and text no record holds
    comes last, in one row marked outside."""
    token = "abcdefghijklmnopqrstuvwxyzABCDEF"
    html = page(f'<main><ul><li><select aria-label="Pick"><option label="ghp_{token}" selected>{token}'
                '</option></select></li></ul>'
                '<div role="row" style="display:contents">NOTE-CONTENTS<input aria-label="F" '
                'value="FIELD-CONTENTS"></div>'
                '<ul contenteditable="true"><li>NOTE-EDITABLE</li></ul>'
                '<article>OUTER-NOTE<input aria-label="O" value="OUTER-FIELD"><div id="h"></div></article>'
                '<article><div>-----BEGIN PRIVATE KEY-----</div><article>YWJjZGVmZ2hpamts</article>'  # gitleaks:allow - synthetic fixture; this test exists to prove masking works
                '<div>-----END PRIVATE KEY-----</div></article>'
                '<article>PRESSHOST<details><summary aria-label="SUMMARYMARK"></summary>X</details>'
                '<div tabindex="0" style="cursor:pointer;width:30px;height:30px" aria-label="CLICKMARK">'
                '</div></article>'
                '<article><select aria-label="Cat" size="3"><option selected label="MARKCHOSEN"></option>'
                '<option label="MARKOTHER"></option></select>'
                '<select autocomplete="cc-exp-year" aria-label="Exp"><option>2030</option>'
                '<option selected>2031</option></select></article>'
                '<p>LOOSE-NOTE</p></main>'
                '<script>document.getElementById("h").attachShadow({mode: "open"}).innerHTML = '
                '"<div role=row>INNER-NOTE</div>";</script>')
    out, _req = gate_run(tmp_path, html, [{"read": {"name": R, "rows": True}}])
    text = out[0]["text"]
    lines = [json.loads(line) for line in text.split("\n")]
    # the option label, and the key masked whole across its two records
    assert token not in text, text
    assert "YWJjZGVmZ2hpamts" not in text, text
    assert text.count("[masked]") == 3, text     # the select's value, its listed option, the key
    for marker in ("SUMMARYMARK", "CLICKMARK"):              # what an observation lists
        assert text.count(marker) == 1, (marker, text)
    # a select gives its options as an observation lists them; a card select gives none
    cats = [f for ln in lines for f in ln["fields"] if f[0] == "Cat"]
    assert cats == [["Cat", "MARKCHOSEN", ["MARKCHOSEN", "MARKOTHER"]]], lines
    assert "2030" not in text and "2031" not in text, text
    for marker in ("NOTE-CONTENTS", "FIELD-CONTENTS", "NOTE-EDITABLE", "OUTER-NOTE", "OUTER-FIELD",
                   "INNER-NOTE", "LOOSE-NOTE"):
        assert text.count(marker) == 1, (marker, text)
    outer = [ln for ln in lines if "OUTER-NOTE" in ln["text"]]
    assert len(outer) == 1 and "INNER-NOTE" not in json.dumps(outer[0]), lines
    assert outer[0]["fields"] == [["O", "OUTER-FIELD"]], lines
    assert lines[-1].get("outside") is True and "LOOSE-NOTE" in lines[-1]["text"], lines
    assert sum(1 for ln in lines if ln.get("outside")) == 1, lines


def test_s4_a_signed_in_sites_buttonless_search_loads_its_result_page(tmp_path):
    """Handed over from slice 2: on a signed-in site, a buttonless search form's Enter is
    judged and sent; its result page, on the site's own origin, loads in the account session
    and the run reads it (it was refused as a page off the session before design §4)."""
    account = (b'<title>A</title><main><form action="/zoek" method="get">'
               b'<label>Zoek <input name="q"></label></form></main>')
    out, req = gate_run(tmp_path, b"", [
        act("type", "Zoek", text="factuur", enter=True), verdict(GO), landed("/zoek?q=factuur"),
        MARGIN, OBS], account_html=account,
        pages={"/zoek": b"<title>Results</title><p>Zoekresultaten voor de factuur</p>"})
    assert packet(out[0])["where"]["session"] == "signed_in"
    assert out[1]["outcome"] == "done", out[1]
    zoek = hits(req, "/zoek")
    assert [(m, host.split(":")[0]) for m, host, _p in zoek] == [("GET", A)], zoek
    assert "Zoekresultaten" in out[4].get("text", ""), out[4]


def test_ab_secrets_never_reach_a_packet(tmp_path):
    html = page('<main><form action="/pay" method="post">'
                '<div contenteditable autocomplete="one-time-code"><span contenteditable>CODE778899</span></div>'
                '<input type="submit" name="otpbtn" autocomplete="one-time-code" value="CODE112233">'
                '<label>Amount <input name="amount" value="10"></label>'
                '<input type="password" name="amount" value="PW445566" disabled>'
                '<button>Pay</button></form></main>')
    out, req = gate_run(tmp_path, html, [act("click", "Pay")])
    text = out[0]["judgement"]["packet"]
    for secret in ("778899", "112233", "445566"):
        assert secret not in text, secret
    # a form holding a secret shows its entries' names, never their values
    assert ["amount", "[not shown]"] in packet(out[0])["target"]["submission"]["entries"]


# --------------------------------------------------------------------------- A and B (after a2)
# The gate's reader against the browser itself: each page is read by ``gate.read`` in the
# image's Chrome, optionally changed, and then pressed (or Enter), and what Chrome sends is
# what the reading must bind: a change in the sent request is a change in the frozen facts and
# in the final read, and no secret value is in a packet, the frozen facts or the reading.

def _form(inner: str, attrs: str = "") -> str:
    return f'<form action="/send?fixed=yes" method="post" {attrs}>{inner}</form>'


_GO = '<button id="t" name="op" value="go">Go</button>'
_IMG = ('<input type="image" id="t" name="map" alt="Go" style="width:100px;height:50px" '
        'src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">')
_FACE = ("<script>customElements.define('x-field', class extends HTMLElement { static formAssociated = true;"
         " constructor() { super(); this.attachInternals().setFormValue('S3CR1T-FACE'); } })</script>")
_SHADOW_PW = ("<script>customElements.define('x-pw', class extends HTMLElement { static formAssociated = true;"
              " constructor() { super(); const i = this.attachInternals();"
              " this.attachShadow({mode: 'open'}).innerHTML = '<input type=password value=S3CR1T-SHADOW>';"
              " i.setFormValue('S3CR1T-SHADOW'); } })</script>")

# (name, page body, how, a change made after the reading or None)
CHANGES = [
    ("image-hidden-collision", _form('<input type="hidden" id="h" name="map.x" value="10">' + _IMG),
     "click", "document.getElementById('h').value = '9000'"),
    ("secret-name-collision", _form('<input type="password" name="amount" disabled value="10">'
                                    '<input type="password" name="amount" disabled value="9000">'
                                    '<input id="h" type="hidden" name="amount" value="10">' + _GO),
     "click", "document.getElementById('h').value = '9000'"),
    ("charset", _form('<input name="q" value="café €"><input type="hidden" name="_charset_">' + _GO,
                      'accept-charset="bogus windows-1252"'),
     "click", "document.querySelector('form').acceptCharset = 'bogus UTF-8'"),
    ("hard-wrap", _form('<textarea name="note" cols="5" wrap="hard">abcdefghijklmno</textarea>' + _GO),
     "click", "document.querySelector('textarea').cols = 10"),
    ("secret-hidden-change", _form('<input type="password" name="pw" value="S3CR1T-PW">'
                                   '<input id="h" type="hidden" name="amount" value="10">' + _GO),
     "click", "document.getElementById('h').value = '9000'"),
    ("r7-span-inside-submit-button", _form('<input id="h" type="hidden" name="amount" value="10">'
                                           '<button name="op" value="go"><span id="t" role="button">Confirm</span></button>'),
     "click", "document.getElementById('h').value = '9000'"),
    ("r7-label-of-a-submit-input", _form('<input id="h" type="hidden" name="amount" value="10">'
                                         '<input type="submit" id="s" name="op" value="go">'
                                         '<label id="t" for="s" role="button">Confirm</label>'),
     "click", "document.getElementById('h').value = '9000'"),
    ("default-button-value", _form('<input id="t" name="invoice" value="42">'
                                   '<button id="b" name="operation" value="view">View invoice</button>'),
     "enter", "document.getElementById('b').value = 'delete'"),
]

SECRET_PAGES = [
    ("secret-occurrence", _form('<input type="hidden" name="pw" value="S3CR1T-A">'
                                '<input type="password" name="pw" value="S3CR1T-A">' + _GO)),
    ("secret-textarea-wrap", _form('<textarea name="otp" autocomplete="one-time-code" cols="4" wrap="hard">'
                                   'S3CR1T-WRAPPED</textarea>' + _GO)),
    ("secret-button-text", _form('<button id="t" autocomplete="one-time-code" name="otp" value="S3CR1T-B">'
                                 'S3CR1T-B</button>')),
    ("secret-editable-barrier", _form('<div contenteditable autocomplete="one-time-code">'
                                      '<div contenteditable="false"><span contenteditable="true">S3CR1T-C</span>'
                                      '</div></div>' + _GO)),
    ("secret-custom-control", _form('<x-field name="otp" autocomplete="one-time-code"></x-field>' + _GO) + _FACE),
    ("secret-shadow-password", _form('<x-pw name="pw"></x-pw>' + _GO) + _SHADOW_PW),
    ("secret-inside-editable", _form('<div contenteditable>Prefix <span autocomplete="one-time-code">'
                                     'S3CR1T-G</span> suffix</div>' + _GO)),
    ("secret-option-hint", _form('<select name="s"><option selected autocomplete="one-time-code">S3CR1T-H'
                                 '</option></select>' + _GO)),
    ("r8-revealed-password", _form('<input type="text" autocomplete="current-password" name="password" '
                                   'value="S3CR1T-I"><input type="text" autocomplete="section-a new-password" '
                                   'name="pw2" value="S3CR1T-J">' + _GO)),
    ("secret-baseline", _form('<input type="password" name="pw" value="S3CR1T-D">'
                              '<input name="otp" autocomplete="one-time-code" value="S3CR1T-E">'
                              '<select autocomplete="cc-type" name="card"><option selected>S3CR1T-F</option></select>'
                              + _GO)),
]


async def _read_and_press(body: str, how: str, change: str | None):
    """Read the page in Chrome (target ``#t``), optionally change it and read again, then press
    (or Enter); returns (first reading, second reading or None, the requests Chrome sent)."""
    from casa_browser import gate
    from patchright.async_api import async_playwright
    sent: list = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path="/opt/google/chrome/chrome",
                                           headless=True, args=["--no-sandbox"])
        try:
            ctx = await browser.new_context(service_workers="block")
            first = True

            async def route(rt):
                nonlocal first
                if "favicon" in rt.request.url:
                    await rt.fulfill(status=404, body="")
                elif first:
                    first = False
                    await rt.fulfill(content_type="text/html; charset=utf-8",
                                     body="<!doctype html><meta charset=utf-8>" + body)
                else:
                    sent.append((rt.request.method, rt.request.url, rt.request.post_data_buffer))
                    await rt.fulfill(content_type="text/html", body="done")
            await ctx.route("**/*", route)
            p = await ctx.new_page()
            await p.goto("https://t.test/page")
            el = await p.query_selector("#t")
            if how == "enter":
                await el.focus()
            a = await gate.read(p.main_frame, el)
            b = None
            if change:
                await p.evaluate(change)
                b = await gate.read(p.main_frame, el)
            if how == "enter":
                await p.keyboard.press("Enter")
            else:
                await el.click()
            await p.wait_for_timeout(300)
        finally:
            await browser.close()
    return a, b, sent


@pytest.mark.parametrize("name,body,how,change", CHANGES, ids=[c[0] for c in CHANGES])
async def test_ab_a_change_in_what_chrome_sends_is_a_change_in_the_frozen_facts(name, body, how, change):
    from casa_browser import gate
    a, b, _ = await _read_and_press(body, how, None)
    a2, b2, sent = await _read_and_press(body, how, change)
    assert a.target and b2.target
    enter = how == "enter"
    # unchanged page: two readings bind the same submission
    assert gate.target_facts(a.target, enter=enter) == gate.target_facts(a2.target, enter=enter)
    # the changed page: the frozen facts differ, the final read differs, and the difference says so
    assert gate.target_facts(a2.target, enter=enter) != gate.target_facts(b2.target, enter=enter)
    assert not gate.same_reading(a2.data, b2.data)
    assert "submission_changed" in gate.difference(a2.data, b2.data)
    assert len(sent) == 1


@pytest.mark.parametrize("name,body", SECRET_PAGES, ids=[c[0] for c in SECRET_PAGES])
async def test_ab_no_secret_in_a_packet_the_facts_or_the_reading(name, body):
    from casa_browser import gate
    a, _, sent = await _read_and_press(body, "click", None)
    pkt = gate.build_packet(a.data, a.target, {"kind": "click"}, task="t", session="signed_in", site="S")
    text = json.dumps([pkt, gate.target_facts(a.target), a.data], ensure_ascii=False)
    assert "S3CR1T" not in text
    assert len(sent) == 1


@pytest.mark.parametrize("body,expect", [
    ('<form action="/s" method="post"><input type="date" id="t" name="d" value="2026-10-29">'
     '<button name="op" value="go">Go</button></form>', "submits"),
    ('<form action="/s" method="post"><input type="bogus" id="t" name="q" value="x">'
     '<button name="op" value="go">Go</button></form>', "submits"),
    ('<form action="/s" method="get"><input type="search" id="t" name="q" value="x"></form>', "submits"),
    ('<form action="/s"><input id="t" name="a" value="x"><input name="b"></form>', "none"),
], ids=["date-default-button", "unknown-type", "buttonless-search", "two-fields-no-button"])
async def test_ab_enter_prediction_matches_chrome(body, expect):
    a, _, sent = await _read_and_press(body, "enter", None)
    sub = a.target["enter_submission"]
    if expect == "submits":
        assert sub and "entries" in sub and len(sent) == 1
    else:
        assert sub == {"none": "no_implicit_submission"} and sent == []


async def test_ab_a_file_input_holding_a_file_is_not_judged():
    """Ruling after a3: any file input holding a file (even an empty, unnamed one) makes the
    submission unreadable, so the step answers ``unsupported``; an empty file input is
    ``[no file]``."""
    body = _form('<input type="file" id="f" name="up"><input name="q" value="hi">' + _GO,
                 'enctype="multipart/form-data"')
    a, _, _ = await _read_and_press(body, "click", None)
    assert ["up", "[no file]"] in a.target["submission"]["entries"]
    a, b, _ = await _read_and_press(body, "click",
        "(() => { const d = new DataTransfer(); d.items.add(new File([], '', {type: 'text/plain'}));"
        " document.getElementById('f').files = d.files; })()")
    assert b.target["submission"] == {"unreadable": "file"}


async def test_r7_a_press_inside_a_link_binds_the_links_address():
    """r7: a control inside a link (a span with role=button) navigates to the link's address on
    a press: that address is the target's exact destination, so a change in it is a change."""
    from casa_browser import gate
    body = '<a id="l" href="/doc?id=1"><span id="t" role="button">Open</span></a>'
    a, b, sent = await _read_and_press(body, "click", "document.getElementById('l').href = '/delete?id=1'")
    assert a.target["activator_href"].endswith("/doc?id=1")
    assert gate.target_facts(a.target) != gate.target_facts(b.target)
    assert len(sent) == 1 and sent[0][1].endswith("/delete?id=1")


async def test_r7_the_hit_test_refuses_a_press_landing_on_a_link_inside_the_target():
    """r7: a press whose point lands on an activatable element inside the target (a link in a
    clickable row) would run that element, not the target: the hit test answers no; a press on
    a span inside a button (the button runs, bound as the target's submitter) is yes."""
    from casa_browser import gate
    from patchright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path="/opt/google/chrome/chrome",
                                           headless=True, args=["--no-sandbox"])
        try:
            p = await browser.new_page()
            await p.set_content('<div id="row" role="button" style="width:300px;height:60px">'
                                '<a href="/delete" style="display:block;width:300px;height:60px">Delete</a></div>'
                                '<form><button><span id="in" role="button">Go</span></button></form>')
            for sel, want in (("#row", False), ("#in", True)):
                el = await p.query_selector(sel)
                box = await el.bounding_box()
                point = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                assert await gate.PageOps().hit(el, point) is want, sel
        finally:
            await browser.close()


# --------------------------------------------------------------------------- slice 5g


def test_s5g_a_time_field_takes_the_judged_value_and_the_form_submits_it(tmp_path):
    """Slice 5g: typing a time is judged as typing (the same packet), then set as one in-page
    step in Chrome; the page's own listener sees it, and the form submits exactly that value."""
    html = page('<main><h1>Order</h1><form action="/submit" method="get"><label>Pickup time '
                '<input type="time" name="t" id="t"></label><button>Order</button></form>'
                '<p id="seen"></p></main>',
                "document.getElementById('t').addEventListener('change', e => "
                "document.getElementById('seen').textContent = 'seen ' + e.target.value);")
    out, req = gate_run(tmp_path, html, [
        act("type", "Pickup time", text="19:30"), {"sleep": 0.5}, OBS,
        act("click", "Order"), verdict(GO), landed("/submit?t=19%3A30"), MARGIN])
    assert out[0]["outcome"] == "done" and out[0]["typed"] == "shown", out[0]
    assert "seen 19:30" in out[2]["text"]
    assert [p for _m, _h, p in hits(req, "/submit")] == ["/submit?t=19%3A30"]


def test_s5g_a_value_the_browser_format_refuses_is_not_judged(tmp_path):
    html = page('<main><form action="/submit"><label>Pickup time <input type="time" name="t">'
                '</label><button>Order</button></form></main>')
    out, req = gate_run(tmp_path, html, [act("type", "Pickup time", text="7:30 PM")])
    assert out[0]["outcome"] == "not_run" and out[0]["reason"] == "value_format", out[0]
    assert hits(req, "/submit") == []


async def _chrome_page(body: str):
    from patchright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(executable_path="/opt/google/chrome/chrome",
                                       headless=True, args=["--no-sandbox", "--lang=en"])
    p = await browser.new_page()
    await p.set_content("<!doctype html><meta charset=utf-8>" + body)
    return pw, browser, p


@pytest.mark.parametrize("kind,good,bad", [
    ("time", "19:30", "7:30 PM"), ("date", "2026-11-03", "03/11/2026"),
    ("datetime-local", "2026-11-03T19:30", "2026-11-03 19:30"), ("month", "2026-11", "Nov 2026"),
    ("week", "2026-W45", "45"), ("color", "#ff0000", "red"), ("range", "50", "55")])
async def test_s5g_the_admission_and_the_in_page_step_agree_with_chrome(kind, good, bad):
    """The admission's detached probe and the in-page step accept and refuse what Chrome's own
    value sanitisation does; a refused value fires nothing and keeps the old value; the page's
    listener sees an accepted one; a page that changes the value after it is reported."""
    from casa_browser import gate
    extra = ' min="0" max="100" step="10"' if kind == "range" else ""
    pw, browser, p = await _chrome_page(
        f'<input type="{kind}" id="t"{extra}><input type="time" id="r"><p id="log"></p><script>'
        "const L = document.getElementById('log');"
        "for (const id of ['t', 'r']) for (const k of ['input', 'change'])"
        " document.getElementById(id).addEventListener(k, e => L.textContent += k + ':' + e.target.value + ';');"
        "document.getElementById('r').addEventListener('change', e => { e.target.value = '19:45'; });"
        "</script>")
    try:
        el, ops = await p.query_selector("#t"), gate.PageOps()
        assert (await ops.text_facts(el, good))["accepts"] is True
        assert (await ops.text_facts(el, bad))["accepts"] is False
        before = await el.evaluate("e => e.value")
        assert await ops.set_value(el, kind, bad) == "not_taken"
        assert await el.evaluate("e => e.value") == before
        assert await p.evaluate("document.getElementById('log').textContent") == ""
        assert await ops.set_value(el, kind, good) == "ok"
        assert await el.evaluate("e => e.value") == good
        log = await p.evaluate("document.getElementById('log').textContent")
        assert log == f"input:{good};change:{good};"
        assert await ops.set_value(el, "date" if kind != "date" else "time", good) == "field"
        rounding = await p.query_selector("#r")
        assert await ops.set_value(rounding, "time", "19:37") == "changed_by_page"
    finally:
        await browser.close()
        await pw.stop()


async def test_s5g_custom_widget_parts_are_listed_and_the_cap_stays_honest():
    """The shared rule: a pointer-cursor time list's entries, inline-block calendar cells, an
    icon beside a read-only field and an inline-onclick element are listed by the observation
    and the gate's reading alike; a grid whose links fill half the cap stays complete."""
    from casa_browser import gate, observe
    body = ('<label>Pickup <input id="f" readonly value="pick"></label><span class="ic"></span>'
            '<style>.times{cursor:pointer}.ic{cursor:pointer;display:inline-block;width:20px;'
            'height:20px}.d{display:inline-block;cursor:pointer}</style>'
            '<ul class="times"><li>09:00</li><li>09:<b>30</b></li></ul>'
            '<div><span class="d">4</span><span class="d">5</span></div>'
            '<div onclick="1">Pick me</div>')
    pw, browser, p = await _chrome_page(body)
    try:
        r = await gate.read(p.main_frame)
        ex = await observe.extract(p.main_frame)
        names = [(c["role"], c["name"]) for c in r.data["controls"]]
        assert names == [("textbox", "Pickup"), ("clickable", ""), ("clickable", "09:00"),
                         ("clickable", "09:30"), ("clickable", "4"), ("clickable", "5"),
                         ("clickable", "Pick me")], names
        assert [(e["role"], e["name"]) for e in ex.elements] == names
        assert r.data["complete"]
        await p.set_content("<table>" + "".join(
            f'<tr><td><a href="/r{i}">row {i}</a></td><td tabindex="0">cell {i}</td></tr>'
            for i in range(201)) + "</table>")
        r = await gate.read(p.main_frame)
        assert r.data["complete"] and len(r.data["controls"]) == gate_cap()
    finally:
        await browser.close()
        await pw.stop()


async def test_s5g_extra_elements_never_take_a_semantic_control_or_option_place():
    """r1 Terra, Astra: one budget — 398 clickable rows, then a select with two options and a
    button: both lists keep the select and the button, the observation both options, and the
    clickable rows give way; the reading stays complete."""
    from casa_browser import gate, observe
    rows = "".join(f'<div style="cursor:pointer">row {i}</div>' for i in range(398))
    pw, browser, p = await _chrome_page(rows + '<select id="s"><option>A</option><option>B</option>'
                                        '</select><button>Book</button>')
    try:
        r = await gate.read(p.main_frame)
        ex = await observe.extract(p.main_frame)
        read = [c["name"] for c in r.data["controls"] if c["role"] != "clickable"]
        seen = [e["name"] for e in ex.elements if e["role"] != "clickable"]
        assert r.data["complete"] and read == ["", "Book"], read
        assert seen == ["", "A", "B", "Book"], seen
        assert len(ex.elements) == gate_cap() and len(r.data["controls"]) == gate_cap() - 2
        assert [c["name"] for c in r.data["controls"] if c["role"] == "clickable"] == \
            [e["name"] for e in ex.elements if e["role"] == "clickable"]
    finally:
        await browser.close()
        await pw.stop()


async def test_s5g_a_range_steps_from_its_value_attribute():
    """r1 Astra: with no min, a range's step base is its value attribute: the admission takes 15
    and refuses 20 exactly as Chrome does."""
    from casa_browser import gate
    pw, browser, p = await _chrome_page('<input type="range" id="t" step="10" value="5">')
    try:
        el, ops = await p.query_selector("#t"), gate.PageOps()
        assert (await ops.text_facts(el, "15"))["accepts"] is True
        assert (await ops.text_facts(el, "20"))["accepts"] is False
        assert await ops.set_value(el, "range", "15") == "ok"
    finally:
        await browser.close()
        await pw.stop()


def gate_cap() -> int:
    from casa_browser import observe
    return observe.MAX_ELEMENTS


async def test_s5g_the_live_view_reaches_a_time_field():
    """Slice 5g: the live view's probe lists a focused time field (its keys go to it); a range
    or colour stays out."""
    from casa_browser.liveview import PROBE
    pw, browser, p = await _chrome_page('<input type="time" id="t"><input type="range" id="g">'
                                        '<input type="color" id="c">')
    try:
        await p.focus("#t")
        got = await p.evaluate(PROBE)
        assert [(f["type"], f["focused"]) for f in got["fields"]] == [("time", True)]
        await p.keyboard.type("0730P")
        assert await p.evaluate("document.getElementById('t').value") == "19:30"
    finally:
        await browser.close()
        await pw.stop()


# --------------------------------------------------------------------------- slice 6b

# A chat composer: Enter on keydown sends (shift+Enter would be a new line), and the page
# counts every Enter key and every line-break input type it sees, in the DOM.
CHAT = ('<main><h1>Klantenservice</h1><form id="m" action="/msg" method="post">'
        '<label>Bericht <textarea id="t" name="t" data-enter="0" data-lb="0"></textarea></label>'
        '<button>Versturen</button></form>'
        '<span id="nl">Notitie</span><div id="ce" contenteditable="true" role="textbox" '
        'aria-labelledby="nl" style="min-height:20px"></div></main>')
CHAT_JS = ("const t = document.getElementById('t'), f = document.getElementById('m');"
           "t.addEventListener('keydown', e => { if (e.key === 'Enter') {"
           " t.dataset.enter = String(+t.dataset.enter + 1);"
           " if (!e.shiftKey) { e.preventDefault(); fetch('/msg?enter', {method: 'POST'}); f.requestSubmit(); } } });"
           "t.addEventListener('beforeinput', e => { if (/LineBreak|Paragraph/.test(e.inputType))"
           " t.dataset.lb = String(+t.dataset.lb + 1); });")
LETTER = "Beste klantenservice,\r\n\r\nMijn bestelling is niet aangekomen.\nGroet, Jan"
SHOWN = ("() => { const t = document.getElementById('t');"
         " return [t.value, t.dataset.enter, t.dataset.lb]; }")


@pytest.mark.parametrize("how", ["go", "tap"])
def test_s6b_multi_line_text_goes_into_a_chat_textarea_as_text(tmp_path, how):
    """Slice 6b in Chrome: on a step that carries a plan, the check's packet holds the whole
    multi-line text; after a go (or a tapped question) it lands in the real <textarea> with its
    line breaks as text: no Enter key reaches the page's keydown handler, so the composer sends
    nothing."""
    steps = [act("type", "Bericht", text=LETTER, raw=True, plan="Write the letter")]
    steps += ([verdict(GO)] if how == "go" else [verdict(ASK_CHANGE), tap(), verdict(SAME)])
    out, req = gate_run(tmp_path, page(CHAT, CHAT_JS), steps + [{"sleep": 1}, js(SHOWN)])
    pkt = packet(out[0])
    want = LETTER.replace("\r\n", "\n")
    assert pkt["action"] == {"kind": "type", "text": want, "then_enter": False}
    assert pkt["target"]["tag"] == "textarea"
    assert out[-3]["outcome"] == "done" and out[-3]["typed"] == "shown", out[-3]
    assert out[-1] == [want, "0", "0"]
    assert hits(req, "/msg") == []


def test_s6b_line_breaks_only_into_a_textarea_and_never_with_enter(tmp_path):
    """A contenteditable editor stays one line; text with line breaks takes no Enter (sending
    stays its own checked step); nothing is judged, typed or sent."""
    out, req = gate_run(tmp_path, page(CHAT, CHAT_JS), [
        act("type", "Notitie", text="a\nb", raw=True),
        act("type", "Bericht", text="a\nb", enter=True, raw=True), {"sleep": 0.5},
        js(SHOWN), js("() => document.getElementById('ce').textContent")])
    assert out[0] == {"outcome": "not_run", "generation": out[0]["generation"],
                      "reason": "one_line"}, out[0]
    assert out[1]["outcome"] == "not_run" and out[1]["reason"] == "no_enter", out[1]
    assert out[-2] == ["", "0", "0"] and out[-1] == ""
    assert hits(req, "/msg") == []


def test_s6b_a_public_search_textarea_takes_a_line_break_as_text(tmp_path):
    """Typing sends nothing: a line break goes into a real textarea as text, search box or not."""
    html = page('<main><form action="/submit" method="get"><label>Zoek <textarea id="q" '
                'name="q" role="searchbox"></textarea></label></form></main>')
    out, req = gate_run(tmp_path, html, [
        act("type", "Zoek", text="pizza\nroma", raw=True), {"sleep": 0.5},
        js("() => document.getElementById('q').value")])
    assert out[0]["outcome"] == "done", out[0]
    assert out[-1] == "pizza\nroma" and hits(req, "/submit") == []


async def test_s6b_chrome_says_which_field_is_a_textarea():
    """The admission's textarea fact and the observation's multi-line kind are Chrome's own
    element facts: only a real <textarea> (not an input, a contenteditable or a textbox role)."""
    from casa_browser import gate, observe
    pw, browser, p = await _chrome_page(
        '<label>A <textarea id="a"></textarea></label><label>B <input id="b"></label>'
        '<span id="lc">C</span><div id="c" contenteditable="true" aria-labelledby="lc"></div>'
        '<span id="ld">D</span><div id="d" role="textbox" tabindex="0" aria-labelledby="ld" style="min-height:20px"></div>')
    try:
        ops = gate.PageOps()
        facts = {i: (await ops.text_facts(await p.query_selector("#" + i), "x\ny"))["textarea"]
                 for i in "abcd"}
        assert facts == {"a": True, "b": False, "c": False, "d": False}
        ex = await observe.extract(p.main_frame)
        kinds = {e["name"]: e.get("kind") for e in ex.elements}
        assert kinds == {"A": "multi-line", "B": None, "C": None, "D": None}, kinds
    finally:
        await browser.close()
        await pw.stop()


# --------------------------------------------------------------------------- slice 5m

FRAMED_TIME = ('<main><h1>Try it</h1><iframe id="res" name="iframeResult" '
               'style="width:600px;height:200px;border:0"></iframe></main>'
               '<template id="inner"><form action="/submit" method="get"><label>Pickup time '
               '<input type="time" name="t" id="t"></label><button>Order</button></form>'
               '<p id="seen"></p><script>document.getElementById("t").addEventListener("change", '
               'e => document.getElementById("seen").textContent = "seen " + e.target.value);'
               '</script></template>')
WRITE_FRAME = ("const d = document.getElementById('res').contentWindow.document;"
               "d.open(); d.write(document.getElementById('inner').innerHTML); d.close();")


def test_s5m_a_time_field_in_a_written_frame_is_seen_set_and_read_back(tmp_path):
    """Slice 5m (phone round 9, W3Schools' demo frame): a field in a frame the page wrote
    (Playwright's address about:blank) is in Fido's observation; typing a time there is judged
    with the frame's own facts, the check allows it, the frame's listener sees it, the next
    observation shows it, and the frame's form submits exactly that value."""
    out, req = gate_run(tmp_path, page(FRAMED_TIME, WRITE_FRAME), [
        {"observe": {"name": R, "values": True}},
        act("type", "Pickup time", text="19:30", plan="Set the time"), verdict(GO), {"sleep": 0.5},
        {"observe": {"name": R, "values": True}},
        act("click", "Order"), verdict(GO), landed("/submit?t=19%3A30"), MARGIN])
    seen, judged, typed, _, after = out[:5]
    assert "Pickup time" in seen["labels"] and seen["values"]["Pickup time"] == "", seen
    assert "[a frame on this page is not shown]" not in seen["text"], seen["text"]
    pkt = packet(judged)
    assert pkt["action"] == {"kind": "type", "text": "19:30", "then_enter": False}
    assert pkt["target"]["input_type"] == "time", pkt["target"]
    assert typed["outcome"] == "done" and typed["typed"] == "shown", typed
    assert after["values"]["Pickup time"] == "19:30", after
    assert "seen 19:30" in after["text"], after["text"]
    assert [p for _m, _h, p in hits(req, "/submit")] == ["/submit?t=19%3A30"]


# --------------------------------------------------------------------------- general abilities

R_ = {"read": {"name": R}}
ASK_PLAN = {"verdict": "ask", "category": "account_change", "reason": "adds the listed cards",
            "question": {"text": "Add the 2 listed cards to the wishlist? Nothing is paid.",
                         "confirm_label": "Add 2 cards", "extra_options": [], "screenshot": "useful",
                         "highlight": [], "text_incomplete": False}}
GO_APPROVED = {"verdict": "go", "category": "account_change", "reason": "carries out the plan"}
PLAN_STEPS = """<button id=c data-k=create onclick="%s">Create</button>
<button id=a data-k=add onclick="%s">Add</button>""" % (SUBMIT, SUBMIT)


PASSWORD_MANAGER_BUTTON = """const pm = document.createElement('com-pm-button'); document.body.append(pm);
  pm.attachShadow({mode: 'closed'}).innerHTML = '<button>Fill</button><div>menu</div>';"""


def test_a_yes_on_a_page_with_a_password_managers_button_runs_once(tmp_path):
    """A closed root with no field leaves the reading whole, so his Yes is re-judged and runs
    instead of lapsing as too much to compare."""
    out, req = gate_run(tmp_path, page(PAY_FORM, PASSWORD_MANAGER_BUTTON), [
        act("click", "Pay now"), verdict(ASK), tap(), verdict(SAME), landed("/submit?pay"), MARGIN])
    assert out[2]["outcome"] == "rejudgement_required", out[2]
    assert out[3]["outcome"] == "done", out[3]
    assert len(hits(req, "/submit")) == 1


def test_a_field_in_a_closed_root_still_has_no_same(tmp_path):
    field = """const h = document.createElement('div'); document.body.append(h);
      h.attachShadow({mode: 'closed'}).innerHTML = '<input value="hidden">';"""
    out, req = gate_run(tmp_path, page(PAY_FORM, field), [act("click", "Pay now"), verdict(ASK), tap()])
    assert out[1]["outcome"] == "asked", out[1]
    assert out[2] == {"error": "unsupported"}, out[2]        # the live view only: no Yes to press
    assert hits(req, "/submit") == []


CHURN = """let n = 0; setInterval(() => { document.getElementById('price').textContent = 'Now ' + (++n); }, 150);"""
CHURN_PAGE = ('<p id="price">Now 0</p><button id="one" data-k="one" onclick="%s">One</button>'
              '<button id="two" data-k="two" onclick="%s">Two</button>' % (SUBMIT, SUBMIT))


def test_a_reading_step_runs_under_a_changing_price(tmp_path):
    out, req = gate_run(tmp_path, page(CHURN_PAGE, CHURN), [
        act("click", "One"), verdict(GO), landed("/submit?one"), MARGIN])
    assert out[1]["outcome"] == "done", out[1]
    assert len(hits(req, "/submit?one")) == 1


def test_a_page_that_loads_on_scroll_is_judged_with_its_picture_and_runs_once(tmp_path):
    """The target scrolls into view for the picture; the page loads more text as it does. The
    packet, read after the picture, holds that text; the go runs once, with no fresh judgement."""
    html = page(f'''<div style="height:3000px">Top</div><button id=b data-k=one onclick="{SUBMIT}">Open</button>
        <div id=more></div>''', """
        new IntersectionObserver((seen, io) => { if (seen[0].isIntersecting) {
          document.getElementById('more').textContent = 'Loaded when it scrolled in'; io.disconnect(); }
        }).observe(document.getElementById('b'));""")
    out, req = gate_run(tmp_path, html, [act("click", "Open"), {"picture": {"name": R}},
                                         verdict(GO), landed("/submit?one"), MARGIN])
    assert "Loaded when it scrolled in" in out[0]["judgement"]["packet"]
    assert out[1]["png"] is True and out[1]["png_bytes"] > 100
    assert out[2]["outcome"] == "done"
    assert len(hits(req, "/submit")) == 1


def test_a_long_list_replaces_the_field_as_one_insertion_exact(tmp_path):
    """5,489 characters over old text: the field holds exactly it, no key event reached the page,
    in a moment."""
    text = "\n".join(f"{n} Card number {n}" for n in range(1, 287))[:5489]
    html = page("""<label>List <textarea id=t rows=5
        onkeydown="window.keys = (window.keys || 0) + 1">old text here</textarea></label>""")
    value = "document.getElementById('t').value"
    out, req = gate_run(tmp_path, html, [
        js("Date.now()"), act("type", "List", text=text), js("Date.now()"),
        js(value), js("window.keys || 0")])
    assert len(text) == 5489
    assert out[1]["outcome"] == "done" and out[1]["typed"] == "shown"
    assert out[2] - out[0] < 5000
    assert out[3] == text
    assert out[4] == 0


def test_a_field_whose_page_refuses_an_insertion_answers_not_shown(tmp_path):
    html = page("""<label>List <textarea id=t oninput="this.value = ''"></textarea></label>""")
    out, _req = gate_run(tmp_path, html, [act("type", "List", text="1 Card\n" * 100)])
    assert out[0]["outcome"] == "done" and out[0]["typed"] == "not_shown"


def _lines(count):
    return "".join(f"<p>line {n:06d} of the list</p>" for n in range(count))


def test_a_150000_character_page_reads_whole_through_next_offset(tmp_path):
    count = 150_000 // 23 + 1
    out, _req = gate_run(tmp_path, page(_lines(count)), [OBS, R_])
    assert out[0]["text_cut"] is True and len(out[0]["text"]) == 20_000
    read = out[1]
    assert read["text"].splitlines() == [f"line {n:06d} of the list" for n in range(count)]
    assert read["windows"] == 6 and read["last"]["complete"] is True
    assert read["last"]["next_offset"] is None and read["last"]["masked"] is False


def test_a_page_past_400000_characters_is_never_complete(tmp_path):
    count = 450_000 // 23
    out, _req = gate_run(tmp_path, page(_lines(count)), [OBS, R_],
                         timeout=600)
    assert out[0]["text_cut"] is True
    read = out[1]
    assert read["last"]["complete"] is False and read["last"]["next_offset"] is None
    assert read["text"].startswith("line 000000") and 399_000 < len(read["text"]) <= 400_000


def test_a_plan_runs_on_one_yes_and_each_press_once(tmp_path):
    """The whole path on a real page: the plan's question, the tap, the press; the next step of
    the plan runs on a go under the approval; each control is pressed once."""
    out, req = gate_run(tmp_path, page(PLAN_STEPS), [
        act("click", "Create", plan="Create the wishlist\nAdd 2 cards"), verdict(ASK_PLAN),
        {"question": {"name": R}}, tap(), verdict(SAME), landed("/submit?create"),
        OBS, act("click", "Add"), verdict(GO_APPROVED), landed("/submit?add"), MARGIN])
    assert packet(out[0])["plan"] == "Create the wishlist\nAdd 2 cards"
    assert out[2]["view"]["plan"] == "Create the wishlist\nAdd 2 cards"
    assert out[2]["view"]["screenshot_id"] is None
    assert out[4]["outcome"] == "done"
    assert packet(out[7])["approved"] == "Create the wishlist\nAdd 2 cards"
    assert out[8]["outcome"] == "done"
    assert len(hits(req, "/submit?create")) == 1 and len(hits(req, "/submit?add")) == 1


def test_a_fee_shown_after_the_judgement_is_never_pressed_on_the_approval(tmp_path):
    """On a real page: under the approval, a fee that appears after the judged reading stops the
    press; the step is judged afresh with the fee in its packet."""
    out, req = gate_run(tmp_path, page(PLAN_STEPS + "<p id=note>Adding is free</p>"), [
        act("click", "Create", plan="Create the wishlist\nAdd 2 cards"), verdict(ASK_PLAN), tap(),
        verdict(SAME), landed("/submit?create"), OBS, act("click", "Add"),
        js("() => { document.getElementById('note').textContent = 'EUR 25 fee, charged now'; }"),
        verdict(GO_APPROVED), {"sleep": 1}])
    assert "approved" in packet(out[6]) and "EUR 25 fee" not in out[6]["judgement"]["packet"]
    assert out[8]["outcome"] == "judgement_required" and "EUR 25 fee" in out[8]["judgement"]["packet"]
    assert hits(req, "/submit?add") == []


# --------------------------------------------------------------------------- filling in

FILL = ('<main><h1>Checkout</h1><p>Total € 84,20</p><form action="/pay?quote=Q84" method="post">'
        '<label>Note <input name="note"></label>'
        '<label>Lines <textarea name="lines"></textarea></label>'
        '<label><input type="checkbox" name="insure" value="yes"> Extra insurance € 40,00</label>'
        '<label><input type="radio" name="ship" value="air"> By air € 15,00</label>'
        '<label>Bank <select name="bank"><option>ING</option><option>ABN AMRO</option></select></label>'
        '<details><summary>Terms</summary>The terms.</details>'
        '<button type="button" aria-expanded="false">More options</button>'
        '<button type="submit">Pay now</button></form></main>')


def fill_steps():
    return [act("type", "Note", text="Ring twice"), act("type", "Lines", text="one\ntwo", raw=True),
            act("click", "Extra insurance"), act("click", "By air"),
            act("select", "Bank", choice="ABN AMRO"), act("click", "Terms"), act("click", "More options"),
            OBS]


def test_filling_in_runs_unjudged_and_the_press_that_pays_is_judged_on_all_of_it(tmp_path):
    out, req = gate_run(tmp_path, page(FILL), [
        *fill_steps(), act("click", "Pay now"), verdict(ASK), {"sleep": 1}, tap(), verdict(SAME),
        landed("/pay"), MARGIN])
    filled, pressed, asked = out[:7], out[8], out[9]
    assert [r["outcome"] for r in filled] == ["done"] * 7
    assert filled[0]["typed"] == "shown"
    assert not any("judgement" in r for r in filled)
    # The press is judged, and its packet holds every tick and value made without a check.
    assert pressed["outcome"] == "judgement_required" and asked["outcome"] == "asked"
    target = packet(pressed)["target"]
    fields = {f["label"]: f for f in target["form"]["fields"]}
    assert fields["Note"]["value"] == "Ring twice" and fields["Lines"]["value"] == "one\ntwo"
    assert fields["Extra insurance € 40,00"]["checked"] is True
    assert fields["By air € 15,00"]["checked"] is True and fields["Bank"]["selected"] == "ABN AMRO"
    assert sorted(target["submission"]["entries"]) == [
        ["bank", "ABN AMRO"], ["insure", "yes"], ["lines", "one\ntwo"], ["note", "Ring twice"],
        ["ship", "air"]]
    # Nothing left before the operator's Yes; the Yes sends once.
    assert out[10:13][2]["outcome"] == "done"
    assert len(hits(req, "/pay")) == 1 and hits(req, "/submit") == []


def test_enter_after_filling_in_is_judged_on_all_of_it(tmp_path):
    out, req = gate_run(tmp_path, page(FILL), [
        act("click", "Extra insurance"), OBS, act("type", "Note", text="x", enter=True), {"sleep": 1}])
    pressed = out[2]
    assert out[0]["outcome"] == "done" and pressed["outcome"] == "judgement_required"
    target = packet(pressed)["target"]
    assert ["insure", "yes"] in target["enter_submission"]["entries"]
    assert hits(req, "/pay") == []


def test_a_tick_after_the_question_voids_the_yes(tmp_path):
    """A box ticked with no check while nothing else moved: the frozen submission at the press
    differs, so a Yes on the earlier reading does not send it."""
    out, req = gate_run(tmp_path, page(FILL), [
        act("click", "Pay now"), verdict(ASK),
        js("() => { document.querySelector('[name=insure]').checked = true; }"),
        tap(), verdict(CHANGED), {"sleep": 1}])
    assert "submission_changed" in packet(out[3])["difference"]
    assert hits(req, "/pay") == []


@pytest.mark.parametrize("control, label, nth", [
    ('<button type="submit" role="checkbox" aria-checked="false">Accept</button>', "Accept", 0),
    ('<button aria-expanded="false">Plan</button>', "Plan", 0),        # no type: it submits
    ('<label><input type="image" alt="Buy" role="radio" src="/x.png"> Buy</label>', "Buy", 0),
    ('<button type="button" onclick="fetch(\'/submit\')">Delete</button>', "Delete", 0),
    ('<div role="switch" tabindex="0" aria-checked="false">Auto renew</div>', "Auto renew", 0),
    ('<div role="menuitem" tabindex="0">Cancel plan</div>', "Cancel plan", 0),
    # Measured in Chromium: a click on these runs the enclosing link or button as well (the
    # second control of that name is the inner one; the first, the link, is free in public).
    ('<a href="/pay?x=1"><button type="button" aria-expanded="false">Plan menu</button></a>', "Plan menu", 1),
    ('<a href="/pay?x=1"><div role="checkbox" tabindex="0">Keep it</div></a>', "Keep it", 1),
    ('<a href="/pay?x=1"><summary>Odd summary</summary></a>', "Odd summary", 1),
    ('<button><summary>Odd summary</summary></button>', "Odd summary", 1),
    ('<button><span role="button" tabindex="0" aria-expanded="false">Span menu</span></button>', "Span menu", 1),
    ('<button>Buy <span id="l">Inner menu</span><input type="button" aria-labelledby="l" '
     'aria-expanded="false"></button>', "Inner menu", -1),
    ('<button>Buy <span id="l">Inner box</span><input type="checkbox" aria-labelledby="l"></button>', "Inner box", -1),
    # An SVG link is one too.
    ('<svg width="300" height="100"><a href="/pay?x=1"><g role="button" aria-expanded="false"><rect width="220" '
     'height="60" fill="blue"/><text x="10" y="35">Svg menu</text></g></a></svg>', "Svg menu", -1),
    ('<svg width="300" height="100" xmlns:xlink="http://www.w3.org/1999/xlink"><a xlink:href="/pay?x=1"><g role="button" '
     'aria-expanded="false"><rect width="220" height="60" fill="blue"/><text x="10" y="35">Old svg menu</text></g></a></svg>',
     "Old svg menu", -1),
])
def test_what_can_send_stays_judged(tmp_path, control, label, nth):
    out, req = gate_run(tmp_path, page(f'<main><form action="/pay" method="post">{control}</form></main>'),
                        [act("click", label, nth=nth), {"sleep": 0.5}])
    assert out[0]["outcome"] == "judgement_required", out[0]
    assert hits(req, "/pay") == [] and hits(req, "/submit") == []


@pytest.mark.parametrize("child", [
    '<button style="width:100%;height:60px">Pay</button>',
    '<a href="/pay?x=1" style="display:block;height:60px">Pay</a>'], ids=["submit", "link"])
def test_a_box_holding_a_submit_or_link_never_presses_it(tmp_path, child):
    """The press point lands on the child: the hit test says moved, and nothing is sent."""
    out, req = gate_run(tmp_path, page('<main><form action="/pay" method="post"><div role="checkbox" '
                                       f'tabindex="0" style="width:300px">Wrap {child}</div></form></main>'),
                        [act("click", "Wrap"), {"sleep": 1}])
    assert out[0]["outcome"] == "not_run" and out[0]["reason"] == "moved", out[0]
    assert hits(req, "/pay") == []


def test_a_field_inside_a_link_is_judged(tmp_path):
    """Measured in Chromium: the click into a text field inside a link follows the link."""
    out, req = gate_run(tmp_path, page('<main><a href="/pay"><label>Odd field <input></label></a></main>'),
                        [act("type", "Odd field", text="x", nth=-1), {"sleep": 0.5}])
    assert out[0]["outcome"] == "judgement_required", out[0]
    assert packet(out[0])["target"]["destination"]["path"] == "/pay"
    assert hits(req, "/pay") == []


def test_a_field_inside_a_submit_button_is_judged(tmp_path):
    out, req = gate_run(tmp_path, page('<main><form action="/pay" method="post"><button>Buy '
                                       '<span id="l">Odd field</span><input aria-labelledby="l"></button></form></main>'),
                        [act("type", "Odd field", text="x", nth=-1), {"sleep": 0.5}])
    assert out[0]["outcome"] == "judgement_required", out[0]
    assert packet(out[0])["target"]["submission"]["method"] == "post"
    assert hits(req, "/pay") == []


def test_a_link_on_a_signed_in_site_keeps_its_check(tmp_path):
    out, _req = gate_run(tmp_path, b"", [act("click", "Pay by link"), {"sleep": 0.5}], account_html=page(
        '<main><a role="checkbox" href="/pay">Pay by link</a><a href="/x" aria-expanded="false">Menu</a></main>'))
    assert out[0]["outcome"] == "judgement_required", out[0]


def test_an_address_on_the_host_open_runs_with_no_check(tmp_path):
    """Real Chrome, the signed-in session: a navigation and an address search on the host open run
    with no packet; the requests are plain GETs, each once. Clicking the same link, a fragment link
    and a press stay checked, and nothing they would send goes."""
    account = page('<main><a href="/next">Next</a>'
                   '<a href="#top">Top</a><button id="b">Send</button></main>')
    pages = {path: page(f'<main><p>{path}</p></main>') for path in ("/next", "/next3")}
    out, req = gate_run(tmp_path, b"", [
        act("click", "Top"), {"sleep": 0.3},
        act("click", "Send"), {"sleep": 0.3},
        act("click", "Next"), {"sleep": 0.3},
        js("() => location.pathname"),
        act("navigate", "Next"), landed("/next"), MARGIN, OBS,
        {"search": {"name": R, "query": f"https://{A}/next3"}}, landed("/next3"), MARGIN],
        account_html=account, pages=pages)
    assert [out[n]["outcome"] for n in (0, 2, 4)] == ["judgement_required"] * 3, out[:5]
    assert out[6] != "/next"
    assert out[7]["outcome"] == "done", out[7]
    assert out[11].get("outcome") == "done", out[11]
    assert [(m, p) for m, _h, p in hits(req, "/next")] == [("GET", "/next"), ("GET", "/next3")], req


def test_a_script_behind_a_link_on_the_host_open_waits_for_the_check(tmp_path):
    """Real Chrome: a link whose click runs a script that sends. Nothing is sent until the check,
    which sees the link's visible name, says go; then the real press runs it once."""
    account = page('<main><a href="/next" onclick="fetch(\'/delete\', {method: \'POST\'}); '
                   'return false">Remove all</a></main>')
    out, req = gate_run(tmp_path, b"", [
        act("click", "Remove all"), {"sleep": 0.5}, js("() => 1"), verdict(GO), MARGIN],
        account_html=account)
    assert out[0]["outcome"] == "judgement_required", out[0]
    assert packet(out[0])["target"]["name"] == "Remove all"
    assert out[3]["outcome"] == "done", out[3]
    assert [m for m, _h, _p in hits(req, "/delete")] == ["POST"], req


def test_a_box_that_turned_into_a_submit_is_not_pressed(tmp_path):
    """Checked at delivery: the target's facts at the press are those it was free for."""
    html = page('<main><form action="/pay" method="post"><label><input id="c" type="checkbox" name="b" value="1"> Box</label>'
                '</form></main>',
                "const c = document.getElementById('c'); c.addEventListener('mousemove', () => { c.type = 'submit'; });")
    out, req = gate_run(tmp_path, html, [act("click", "Box"), {"sleep": 1}])
    assert out[0] == {**out[0], "outcome": "not_run", "reason": "changed"}
    assert hits(req, "/pay") == []


def test_a_step_carrying_a_plan_goes_to_the_check(tmp_path):
    out, _req = gate_run(tmp_path, page(FILL), [
        act("type", "Note", text="x", plan="Add a note and pay")])
    assert out[0]["outcome"] == "judgement_required"
    assert packet(out[0])["plan"] == "Add a note and pay"


# ------------------------------------------------------------- a long page: a Yes that can press

LONG_LIST = "".join(f'<button>Item {n}</button><br>' for n in range(420))
SAVE = f'<button id="go" data-k="go" onclick="{SUBMIT}">Save</button>'
TO_THE_END = "() => { scrollTo(0, document.body.scrollHeight); }"


def test_a_yes_presses_on_a_stable_long_page_scrolled_meanwhile(tmp_path):
    out, req = gate_run(tmp_path, page(SAVE + LONG_LIST), [
        act("click", "Save"), verdict(ASK_CHANGE), {"question": {"name": R}}, js(TO_THE_END),
        tap(), verdict(SAME), landed("/submit?go"), MARGIN])
    _, asked, question, _, rejudge, done, _, _ = out
    assert asked["outcome"] == "asked" and question["view"]["confirm_offered"] is True
    assert rejudge["outcome"] == "rejudgement_required"
    assert packet(rejudge)["difference"] == {}
    assert done["outcome"] == "done"
    assert len(hits(req, "/submit?go")) == 1


def test_a_yes_presses_on_a_long_page_with_a_hover_cursor_rule(tmp_path):
    """The pointer's arrival adds a control to the list before the guard's final read."""
    wrapped = ('<style>.wrap:hover { cursor: pointer; }</style><div class="wrap">' + SAVE
               + '<div>Row text <span tabindex="0">Inner</span></div></div>')
    out, req = gate_run(tmp_path, page(wrapped + LONG_LIST), [
        act("click", "Save"), verdict(ASK_CHANGE), tap(), verdict(SAME), landed("/submit?go"),
        MARGIN])
    assert out[3]["outcome"] == "done"
    assert len(hits(req, "/submit?go")) == 1


def test_an_unlisted_control_changing_during_the_wait_stops_the_press(tmp_path):
    beyond = LONG_LIST + '<a id="l" href="/one">A link</a>'
    out, req = gate_run(tmp_path, page(SAVE + beyond), [
        act("click", "Save"), verdict(ASK_CHANGE), tap(),
        js("() => { document.getElementById('l').href = '/two'; }"), verdict(SAME)])
    assert out[-1]["outcome"] == "rejudgement_required"
    assert packet(out[-1])["difference"] == {"controls_beyond_list_changed": True}
    assert hits(req, "/submit") == []


REMOVE_ROWS = ('<table>'
               '<tr data-k="alice"><td>Alice</td><td><button class="rm">Remove</button></td></tr>'
               '<tr data-k="bob"><td>Bob</td><td><button class="rm">Remove</button></td></tr>'
               '</table>')
REMOVE_ON_ITS_ROW = ("document.addEventListener('click', (e) => { if (e.target.matches('.rm')) "
                     "fetch('/submit?' + e.target.closest('tr').dataset.k, {method: 'POST'}); });")
SWAP_THE_BUTTONS = """() => { const [x, y] = document.querySelectorAll('.rm');
  const px = x.parentNode, py = y.parentNode; px.append(y); py.append(x); }"""


@pytest.mark.parametrize("filler", ["", LONG_LIST], ids=["fits", "long"])
def test_a_button_moved_to_another_row_during_the_wait_presses_nothing(tmp_path, filler):
    out, req = gate_run(tmp_path, page(REMOVE_ROWS + filler, REMOVE_ON_ITS_ROW), [
        act("click", "Remove"), verdict(ASK_CHANGE), tap(), js(SWAP_THE_BUTTONS), verdict(SAME)])
    assert out[-1]["outcome"] == "rejudgement_required"
    assert packet(out[-1])["difference"] == {"target_record_changed": {
        "before": ["Alice", "Remove"], "after": ["Bob", "Remove"]}}
    assert hits(req, "/submit") == []


SELECT_ROWS = ('<div id="a" tabindex="0" aria-checked="true">Alice</div>'
               '<div id="b" tabindex="0" aria-checked="false">Bob</div>'
               f'<button id="rm" data-k="selected" onclick="{SUBMIT}">Remove selected</button>')
MOVE_SELECTION = """() => { document.getElementById('a').setAttribute('aria-checked', 'false');
  document.getElementById('b').setAttribute('aria-checked', 'true'); }"""


def test_a_selection_held_in_tabindex_rows_moving_during_the_wait_presses_nothing(tmp_path):
    out, req = gate_run(tmp_path, page(SELECT_ROWS + LONG_LIST), [
        act("click", "Remove selected"), verdict(ASK_CHANGE), tap(), js(MOVE_SELECTION),
        verdict(SAME)])
    assert out[-1]["outcome"] == "rejudgement_required"
    assert packet(out[-1])["difference"]["controls_beyond_list_changed"] is True
    assert hits(req, "/submit") == []


def test_a_checkbox_in_a_small_frame_beside_a_long_page_changing_presses_nothing(tmp_path):
    small = page('<label><input id="c" type="checkbox"> Share profile publicly</label>')
    frame = '<iframe id="fr" src="/small" style="width:300px;height:100px"></iframe>'
    tick = ("() => { document.getElementById('fr').contentDocument"
            ".getElementById('c').checked = true; }")
    out, req = gate_run(tmp_path, page(frame + SAVE + LONG_LIST), [
        act("click", "Save"), verdict(ASK_CHANGE), tap(), js(tick), verdict(SAME)],
        pages={"/small": small})
    assert out[-1]["outcome"] == "rejudgement_required"
    difference = packet(out[-1])["difference"]
    assert difference["controls_beyond_list_changed"] is True
    assert len(difference["controls_changed"]) == 1
    assert hits(req, "/submit") == []


CLOSED_FIELD = ('<div id="h"></div>', "document.getElementById('h').attachShadow({mode: 'closed'})"
                                      ".innerHTML = '<input>';")


@pytest.mark.parametrize("body,script", [
    CLOSED_FIELD, ("<p>" + "word " * 41_000 + "</p>", "")], ids=["closed_shadow", "long_block"])
def test_a_page_the_app_cannot_read_whole_offers_no_yes(tmp_path, body, script):
    out, req = gate_run(tmp_path, page(SAVE + body, script), [
        act("click", "Save"), verdict(ASK_CHANGE), {"question": {"name": R}}, tap()])
    _, asked, question, tapped = out
    assert asked["outcome"] == "asked"
    assert question["view"]["confirm_offered"] is False
    assert question["view"]["live_only_copy"] == ("I can't re-check this page before pressing, "
                                                  "so please do it in the live view.")
    assert tapped == {"error": "unsupported"}
    assert hits(req, "/submit") == []
