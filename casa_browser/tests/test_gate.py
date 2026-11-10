"""The action gate's pure parts (action model slice 2: S2-1, S2-2, S2-3, S2-10): design §3's
scope table, the packet (its block order with the target's record, its secrecy), the canonical
bytes and digest, and the tap-time difference and final-read comparison. No browser: the
readings are synthetic, in ``READ``'s shape. Everything here is invented."""
from __future__ import annotations

import hashlib
import json

import pytest

from casa_browser import gate

# --------------------------------------------------------------------------- scope (§3)

# (action, facts, signed-in session, public session): design §3's table, rows for rows.
SCOPE_ROWS = [
    ("navigate", {"is_link": True}, True, False),
    ("click", {"is_link": True}, True, False),               # a link click
    ("search", {}, True, False),
    ("type", {"search_field": True}, True, False),
    ("type", {}, True, True),                                 # any other text field
    ("click", {}, True, True),                                # anything not a link
    ("select", {}, True, True),
    ("back", {}, True, False),
    ("download", {"download_via": "link"}, True, False),
    ("download", {"download_via": "button"}, True, True),
    ("scroll", {}, False, False),
]


@pytest.mark.parametrize("action,facts,signed_in,public", SCOPE_ROWS,
                         ids=[f"{a}-{'-'.join(f) or 'plain'}" for a, f, _s, _p in SCOPE_ROWS])
def test_scope_table_rows(action, facts, signed_in, public):
    assert gate.in_scope("account", action, **facts) is signed_in
    assert gate.in_scope("public", action, **facts) is public


@pytest.mark.parametrize("url,page,same", [
    ("https://a.example/list", "https://a.example/home", True),
    ("https://www.a.example/list", "https://www.a.example/home", True),
    ("https://www.a.example/list", "https://shop.a.example/home", False),
    ("https://www.example.test/list", "https://www.example.test/home", True),
    ("https://example.test/list", "https://www.example.test/home", False),       # a subdomain
    ("https://www.example.test/list", "https://example.test/home", False),
    ("https://beta.co.uk/list", "https://alpha.co.uk/home", False),              # one public suffix
    ("https://b.github.io/list", "https://a.github.io/home", False),
    ("http://a.example/list", "https://a.example/home", True),
    ("https://a.example/home", "https://a.example/home", True),                  # a reload loads
    ("https://a.example/home#top", "https://a.example/list", True),
    ("https://a.example/home#", "https://a.example/home", True),                 # no fragment
    ("https://a.example/home#top", "https://a.example/home", False),
    ("https://a.example/home#top", "https://a.example/home#bottom", False),
    ("https://b.example/list", "https://a.example/home", False),
    ("https://a.example/list", "about:blank", False),
    ("https://a.example/list", "chrome-error://chromewebdata/", False),
    ("javascript:void(0)", "https://a.example/home", False),
    ("https:///list", "https://a.example/home", False),
    ("https://[bad/list", "https://a.example/home", False),
])
def test_same_host_load(url, page, same):
    assert gate.same_host_load(url, page) is same


def test_only_a_navigation_on_the_host_open_is_free_on_an_account_session():
    assert not gate.in_scope("account", "navigate", is_link=True, same_host=True)
    for action, facts in (("click", {"is_link": True}), ("click", {}), ("back", {}), ("type", {}),
                          ("select", {}),
                          ("download", {"download_via": "link"}),
                          ("download", {"download_via": "button"})):
        assert gate.in_scope("account", action, same_host=True, **facts), action


def _control(role, tag, **facts):
    return {"role": role, "tag": tag, "state": {}, **facts}


@pytest.mark.parametrize("kind,target,enter,free", [
    ("type", _control("textbox", "input"), False, True),
    ("type", _control("textbox", "input"), True, False),
    ("select", _control("combobox", "select"), False, True),
    # a text field or select inside a link: pressing into it follows the link
    ("type", _control("textbox", "input", activator_href="https://a.example/x"), False, False),
    ("select", _control("combobox", "select", activator_href="https://a.example/x"), False, False),
    ("click", _control("checkbox", "input"), False, True),
    ("click", _control("radio", "input"), False, True),
    ("click", _control("checkbox", "button"), False, True),      # a custom box, no submitter
    ("click", _control("button", "summary"), False, True),
    ("click", _control("button", "button", state={"expanded": "false"}), False, True),
    ("click", _control("button", "button"), False, False),       # any other button can act
    ("click", _control("switch", "button"), False, False),
    ("click", _control("menuitem", "div"), False, False),
    ("click", _control("tab", "button", state={"expanded": "false"}), False, False),
    # anything with a destination sends, whatever it calls itself
    ("click", _control("checkbox", "a", href="https://a.example/pay"), False, False),
    ("click", _control("button", "span", activator_href="https://a.example/pay",
                       state={"expanded": "false"}), False, False),
    ("click", _control("checkbox", "button", is_submitter=True, submit_action="https://a.example/pay",
                       submit_method="post", submit_target=""), False, False),
    ("click", _control("button", "summary", is_submitter=True), False, False),
    ("click", _control("checkbox", "input", secret=True), False, False),
    ("click", None, False, False),
    ("navigate", _control("checkbox", "input"), False, False),
    ("back", None, False, False),
])
def test_sends_nothing_only_for_what_prepares(kind, target, enter, free):
    assert gate.sends_nothing(kind, target, enter=enter) is free


def test_canonical_bytes_and_digest():
    p = {"b": "€ 84,20", "a": [1, {"d": None, "c": True}]}
    data = gate.canonical(p)
    assert data == '{"a":[1,{"c":true,"d":null}],"b":"€ 84,20"}'.encode("utf-8")
    assert gate.digest(data) == hashlib.sha256(data).hexdigest()
    assert gate.canonical({"a": 1, "b": 2}) == gate.canonical({"b": 2, "a": 1})


# --------------------------------------------------------------------------- packets (§2.2)

def _control(idx, role, name, **kw):
    c = {"idx": idx, "role": role, "name": name, "tag": kw.pop("tag", "button"),
         "input_type": kw.pop("input_type", None), "href": kw.pop("href", None),
         "is_submitter": False, "submit_action": None, "submit_method": None,
         "submit_target": None, "form": None, "container": None, "record": None, "block": -1,
         "heading": "Facturen", "state": {}, "secret": False, "download_attr": False,
         "form_self": -1}
    c.update(kw)
    return c


def invoice_table() -> dict:
    """A synthetic invoice table: three rows (records 11, 12, 13), each with its month, date,
    amount, status and a download control; the middle row's control is a button."""
    rows = [("Factuur oktober 2026", "31-10-2026", "€ 84,20", "Openstaand"),
            ("Factuur september 2026", "30-09-2026", "€ 79,60", "Betaald"),
            ("Factuur augustus 2026", "31-08-2026", "€ 77,10", "Betaald")]
    blocks = [{"text": "Facturen", "containers": [1], "record": None},
              {"text": "Klantnummer 1048. Hier vind je al je facturen.", "containers": [1],
               "record": None}]
    controls = []
    for r, cells in enumerate(rows):
        record = 11 + r
        for cell in cells:
            blocks.append({"text": cell, "containers": [1], "record": record})
        blocks.append({"text": "Download PDF", "containers": [1], "record": record})
        controls.append(_control(len(controls), "button" if r == 1 else "link", "Download PDF",
                                 block=len(blocks) - 1, record=record, container=1,
                                 href=None if r == 1 else f"https://shop.example/f/{r}.pdf"))
    blocks.append({"text": "Wat staat er op mijn factuur?", "containers": [1], "record": None})
    return {"href": "https://shop.example/facturen/", "title": "Facturen", "complete": True,
            "blocks": blocks, "controls": controls, "target": None, "target_idx": -1,
            "has_cc": False}


LONG = "".join(f"item {i:05d}\n" for i in range(2_500)) + "trailing item zz"   # 27,516 characters


def _pasted(form_owner: bool, *, other_frame: bool = False) -> tuple[dict, dict]:
    """A page with a long list pasted into a textarea and a Save button: in the button's form,
    or with no form owner (the page's script sends it)."""
    reading = invoice_table()
    form = {"id": 7, "method": "post", "action": "https://shop.example/save", "target": "",
            "fields": [{"label": "List", "type": "textarea", "value": LONG}]} if form_owner else None
    area = _control(len(reading["controls"]), "textbox", "List", tag="textarea", form=form,
                    form_self=0 if form_owner else -1, state={"value": LONG})
    save = _control(len(reading["controls"]) + 1, "button", "Save", form=form)
    reading["controls"] += [area, save]
    if other_frame:                       # the same ids in another frame's reading
        child = {"href": "https://shop.example/f", "complete": True, "blocks": [],
                 "controls": [dict(area, idx=0, form=form, state={"value": "other " + LONG})]}
        gate.merge_frame(reading, child, 1, shown=True)
    return reading, save


@pytest.mark.parametrize("form_owner", [True, False], ids=["in-the-form", "no-form-owner"])
def test_a_long_pasted_value_is_in_the_packet_whole_exactly_once(form_owner):
    """Astra S1 (run-r1): the check sees the whole masked value a press can send, a form or not:
    once in ``target.form.fields`` when the form lists it, else once in its control line."""
    reading, save = _pasted(form_owner)
    pkt = gate.build_packet(reading, save, {"kind": "click"}, task="t", session="signed_in",
                            site="Shop")
    text = json.dumps(pkt, ensure_ascii=False)
    assert text.count("trailing item zz") == 1
    assert text.count("item 02499") == 1
    if form_owner:
        assert pkt["target"]["form"]["fields"][0]["value"] == LONG
    else:
        assert any(line.startswith("textbox: List") and "trailing item zz" in line
                   for line in pkt["page"]["controls"])


def test_another_frames_field_with_the_same_ids_is_shown_whole_in_its_line():
    """A form id belongs to one frame's reading: another frame's field is never taken for one of
    the target's form fields, so its value is whole in its own line."""
    reading, save = _pasted(True, other_frame=True)
    pkt = gate.build_packet(reading, save, {"kind": "click"}, task="t", session="signed_in",
                            site="Shop")
    text = json.dumps(pkt, ensure_ascii=False)
    assert text.count("trailing item zz") == 2      # the form's field, and the frame's own line
    assert text.count("other item 00000") == 1


def test_an_editable_inside_the_form_that_the_form_does_not_list_is_whole_in_its_line():
    """A contenteditable inside the target's form element is no field of it (``form_self`` -1):
    the form's fields never show it, so its line does."""
    reading, save = _pasted(True)
    note = _control(50, "textbox", "Note", tag="div", form=save["form"], form_self=-1,
                    state={"value": "x " * 300 + "editable end"})
    reading["controls"].append(note)
    text = json.dumps(gate.build_packet(reading, save, {"kind": "click"}, task="t",
                                        session="signed_in", site="Shop"), ensure_ascii=False)
    assert text.count("editable end") == 1 and text.count("trailing item zz") == 1


def test_a_whole_value_is_still_masked_and_a_secret_stays_not_shown():
    reading, save = _pasted(False)
    token = "Zq81Yt7Wm2Pk5Rn9Xs4Bv6Cd3Lh0Fg"  # gitleaks:allow - synthetic fixture; this test exists to prove masking works
    reading["controls"][-2]["state"]["value"] = LONG + " " + token + " https://bank.example/x?k=1"
    reading["controls"].append(_control(99, "textbox", "Code", tag="input",
                                        state={"value": gate.NOT_SHOWN}))
    text = json.dumps(gate.build_packet(reading, save, {"kind": "click"}, task="t",
                                        session="signed_in", site="Shop"), ensure_ascii=False)
    assert token not in text and "k=1" not in text and "[masked]" in text
    assert 'textbox: Code {\\"value\\": \\"[not shown]\\"}' in text


def test_packet_orders_target_record_form_rest():
    """The measurement's one packet finding: a table cell's button carries its row, before
    any other row."""
    reading = invoice_table()
    target = reading["controls"][1]                 # September's button
    pkt = gate.build_packet(reading, target, {"kind": "download"}, task="Get my invoice",
                            session="signed_in", site="Shop")
    texts = [b["text"] for b in pkt["page"]["blocks"]]
    assert texts[:5] == ["Download PDF", "Factuur september 2026", "30-09-2026", "€ 79,60",
                         "Betaald"]
    assert pkt["target"]["block"] == 1 and pkt["target"]["record_blocks"] == [2, 3, 4, 5]
    assert texts.index("Factuur september 2026") < texts.index("Factuur oktober 2026")
    assert set(pkt) == {"task", "where", "action", "target", "page", "run_so_far"}
    assert set(pkt["target"]) == {"block", "record_blocks", "role", "name", "tag", "input_type",
                                  "is_link", "nearest_heading"}
    # Without a record (a control outside any row), the measured order: block, container, rest.
    lone = dict(target, record=None, block=1)
    pkt = gate.build_packet(reading, lone, {"kind": "click"}, task="t", session="signed_in",
                            site="Shop")
    assert pkt["target"]["record_blocks"] == []
    assert pkt["page"]["blocks"][0]["text"] == reading["blocks"][1]["text"]


def test_packet_cap_marks_truncation():
    reading = invoice_table()
    reading["blocks"].append({"text": "x " * 7000, "containers": [], "record": None})
    reading["blocks"].append({"text": "y " * 7000, "containers": [], "record": None})
    pkt = gate.build_packet(reading, reading["controls"][1], {"kind": "click"}, task="t",
                            session="signed_in", site="Shop")
    assert pkt["page"]["page_truncated"] is True
    assert sum(len(b["text"]) for b in pkt["page"]["blocks"]) <= gate.PAGE_CAP


def test_exact_destination_never_in_packet():
    reading = invoice_table()
    link = _control(9, "link", "Pay", href="https://shop.example/pay?quote=Q84&token=abc")
    submit = _control(10, "button", "Factuur downloaden", is_submitter=True,
                      submit_action="https://shop.example/betalen/start?invoice=OCT-77",
                      submit_method="post", submit_target="",
                      form={"id": 3, "method": "get", "action": "https://shop.example/f?x=SECRETQ",
                            "target": "", "fields": []})
    reading["controls"] += [link, submit]
    for target in (link, submit):
        data = gate.canonical(gate.build_packet(reading, target, {"kind": "click"}, task="t",
                                                session="signed_in", site="Shop"))
        for raw in ("Q84", "token=abc", "OCT-77", "invoice=", "SECRETQ", "pay?", "start?", "f?x"):
            assert raw.encode() not in data, raw
    pkt = gate.build_packet(reading, submit, {"kind": "click"}, task="t", session="signed_in",
                            site="Shop")
    assert pkt["target"]["submits_form"] == {"method": "post", "destination": {
        "origin": "https://shop.example", "path": "/betalen/start", "same_site": True}}


def test_secret_values_never_in_packet():
    """Password, one-time-code and payment-card values arrive from READ as ``[not shown]`` and
    stay so; ordinary values are masked text (a credential-format token is masked)."""
    reading = invoice_table()
    form = {"id": 4, "method": "post", "action": "https://shop.example/pay", "target": "",
            "fields": [{"label": "Card number", "type": "text", "value": "[not shown]"},
                       {"label": "Code", "type": "text", "value": "[not shown]"},
                       {"label": "Note", "type": "text",
                        "value": "ref AKIAABCDEFGHIJKLMNOP done"}]}  # gitleaks:allow - synthetic fixture; this test exists to prove masking works
    reading["controls"] += [
        _control(9, "textbox", "Card number", tag="input", input_type="text", form=form,
                 state={"value": "[not shown]"}, secret=True),
        _control(10, "textbox", "Note", tag="input", input_type="text", form=form,
                 state={"value": "ref AKIAABCDEFGHIJKLMNOP done"})]  # gitleaks:allow - synthetic fixture; this test exists to prove masking works
    data = gate.canonical(gate.build_packet(reading, reading["controls"][-1], {"kind": "click"},
                                            task="t", session="signed_in", site="Shop"))
    assert b"AKIAABCDEFGHIJKLMNOP" not in data  # gitleaks:allow - synthetic fixture; this test exists to prove masking works
    assert data.count("[not shown]".encode()) >= 3


# --------------------------------------------------------------------------- tap time (§2.3)

def test_difference_is_over_the_full_reading():
    """The difference covers the whole reading: a surcharge past the packet cap is in it."""
    a = invoice_table()
    a["blocks"] += [{"text": "filler " * 2500, "containers": [], "record": None}]
    c = json.loads(json.dumps(a))
    c["blocks"].append({"text": "Toeslag € 2,50", "containers": [], "record": None})
    target = a["controls"][1]
    pkt = gate.build_packet(c, target, {"kind": "click"}, task="t", session="signed_in", site="S")
    assert "Toeslag € 2,50" not in json.dumps(pkt, ensure_ascii=False)     # past the cap
    assert gate.difference(a, c) == {"blocks_added": ["Toeslag € 2,50"]}
    c["controls"][1]["state"] = {"disabled": True}
    c["controls"][0]["name"] = "Pay"
    d = gate.difference(a, c)
    assert d["controls_changed"] == [{"before": "button: Download PDF",
                                      "after": 'button: Download PDF {"disabled": true}'}]
    assert d["controls_removed"] == ["link: Download PDF"] and d["controls_added"] == ["link: Pay"]
    assert gate.difference(a, json.loads(json.dumps(a))) == {}


def test_difference_too_large_has_no_same():
    a = invoice_table()
    c = json.loads(json.dumps(a))
    c["blocks"] += [{"text": f"new block {i} " + "z" * 100, "containers": [], "record": None}
                    for i in range(150)]
    assert not gate.difference_fits(gate.difference(a, c))
    c = json.loads(json.dumps(a))
    c["blocks"][0]["text"] = "Facturen 2026"
    assert gate.difference_fits(gate.difference(a, c))


def test_incomplete_reading_has_no_same():
    a = invoice_table()
    b = json.loads(json.dumps(a))
    assert gate.same_reading(a, b)
    b["complete"] = False
    assert not gate.same_reading(a, b) and not gate.same_reading(b, json.loads(json.dumps(b)))


def test_final_read_has_no_exemption():
    """Ruling 8: any difference at the final read — a block's text, a control's state, an exact
    destination (its query included) — is a difference."""
    a = invoice_table()
    for mutate in (lambda r: r["blocks"][1].update(text="Klantnummer 1049."),
                   lambda r: r["controls"][0]["state"].update(disabled=True),
                   lambda r: r["controls"][0].update(href="https://shop.example/f/0.pdf?v=2"),
                   lambda r: r["blocks"].append({"text": "1 s ago", "containers": [],
                                                 "record": None})):
        b = json.loads(json.dumps(a))
        mutate(b)
        assert not gate.same_reading(a, b)


def test_target_facts_freeze_identity_and_values():
    form = {"id": 1, "method": "post", "action": "https://shop.example/r", "target": "",
            "fields": [{"label": "Reason", "type": "select", "selected": "Te klein"},
                       {"label": "Comment", "type": "text", "value": ""}]}
    t = _control(0, "textbox", "Comment", tag="input", input_type="text", form=form,
                 state={"value": ""}, form_self=1, enter_action="https://shop.example/r",
                 enter_method="post", enter_target="")
    frozen = gate.target_facts(t, enter=True)
    typed = gate.target_facts(dict(t, state={"value": "x", "expanded": "true"}), enter=True)
    assert gate.facts_but_state(frozen) == gate.facts_but_state(typed) and frozen != typed
    other = json.loads(json.dumps(t))
    other["form"]["fields"][0]["selected"] = "Te groot"        # another field of the form moved
    assert gate.facts_but_state(gate.target_facts(other, enter=True)) != gate.facts_but_state(frozen)
    moved = dict(t, enter_action="https://shop.example/delete")
    assert gate.target_facts(moved, enter=True)["enter_destination"] != frozen["enter_destination"]


def test_verdict_bounds_match_the_check():
    ok = {"verdict": "ask", "category": "payment", "reason": "x" * 600,
          "question": {"text": "x" * 600, "confirm_label": "x" * 32,
                       "extra_options": ["x" * 60] * 3, "screenshot": "required",
                       "highlight": ["x" * 200] * 6, "text_incomplete": False}}
    assert gate.verdict_within_bounds("action", ok)
    for key, bad in (("reason", "x" * 601), ("category", "browsing")):
        assert not gate.verdict_within_bounds("action", {**ok, key: bad})
    for key, bad in (("confirm_label", "x" * 33), ("extra_options", ["x" * 61]),
                     ("extra_options", ["a"] * 4), ("highlight", ["h"] * 7),
                     ("screenshot", "maybe"), ("text_incomplete", "no"), ("text", "x" * 601)):
        assert not gate.verdict_within_bounds("action", {**ok, "question": {**ok["question"],
                                                                             key: bad}})
    assert gate.verdict_within_bounds("rejudge", {"verdict": "same", "reason": "r",
                                                  "text_incomplete": False})
    assert not gate.verdict_within_bounds("rejudge", {"verdict": "go", "reason": "r",
                                                      "text_incomplete": False})
    assert not gate.verdict_within_bounds("action", {"verdict": "same", "category": "reading"})


def test_every_state_text_is_masked_and_secret_selects_hidden():
    """r1: a select's chosen option and options, and every other state text, pass masking;
    a secret select (``cc-*``) arrives from READ as ``[not shown]`` and stays so."""
    reading = invoice_table()
    url = "https://bank.example/pay?token=Zq81Yt7Wm2Pk5Rn9Xs4Bv6Cd3Lh0Fg"  # gitleaks:allow - synthetic fixture; this test exists to prove masking works
    reading["controls"] += [
        _control(9, "combobox", "Bank", tag="select", state={"selected": url, "options": [url]}),
        _control(10, "combobox", "Expiry", tag="select", state={"selected": "[not shown]"})]
    data = gate.canonical(gate.build_packet(reading, reading["controls"][-2], {"kind": "select",
                                            "option": "x"}, task="t", session="signed_in", site="S"))
    assert b"token=" not in data and b"Zq81Yt7" not in data
    assert 'combobox: Expiry {"selected": "[not shown]"}' in json.loads(data)["page"]["controls"]


def test_r6_a_multiple_selects_chosen_labels_in_the_targets_form_are_masked():
    """r6: list-valued form fields (a multiple select's chosen labels) pass the same masking as
    every other packet text; an exact destination never reaches the packet."""
    reading = invoice_table()
    url = "https://accounts.example/transfer?recipient=alice&amount=99"
    t = dict(reading["controls"][1], form={"method": "post", "action": "https://shop.example/save",
             "target": "", "fields": [{"label": "To", "type": "select", "selected": [url, "plain"]}]})
    data = gate.canonical(gate.build_packet(reading, t, {"kind": "click"}, task="t",
                                            session="signed_in", site="S"))
    assert b"recipient=alice" not in data and b"amount=99" not in data
    assert json.loads(data)["target"]["form"]["fields"][0]["type"] == "select"


def test_r7_a_submissions_browsing_context_name_is_masked():
    """r7: the form's target name is page text: masked in a packet and a difference."""
    sub = dict(SUB, target="https://bank.example/transfer?recipient=alice&amount=99")
    for cut in (True, False):
        shown = json.dumps(gate.shown_submission(sub, "https://shop.example/", cut=cut))
        assert "recipient=alice" not in shown and "amount=99" not in shown


def test_every_page_string_a_packet_carries_is_masked():
    """r2, r6, r7 had one shape (a page string reaching the packet unmasked: an option label, a
    multiple select's list, a form's target name). Every string READ takes from the page is set
    to an address with a private query; none of the query reaches the packet or a difference."""
    leak = "https://bank.example/x?tok=PRIVATEQ"
    reading = invoice_table()
    reading["title"] = leak
    reading["blocks"].append({"text": leak, "containers": [1], "record": 12})
    sub = {"method": "post", "action": leak, "enctype": "application/x-www-form-urlencoded",
           "target": leak, "submitter": {"role": "button", "name": leak},
           "entries": [[leak, leak]], "raw": "0" * 64}
    t = dict(reading["controls"][1], name=leak, heading=leak, is_submitter=True,
             submit_action=leak, submit_method="post", submit_target=leak, activator_href=None,
             state={"value": leak, "selected": [leak], "options": [leak]},
             form={"method": "post", "action": leak, "target": leak,
                   "fields": [{"label": leak, "type": "select", "value": leak, "selected": [leak]}]},
             submission=sub, enter_submission=sub, enter_action=leak, enter_method="post",
             enter_target=leak)
    reading["controls"].append(dict(t, idx=len(reading["controls"])))
    reading["target"] = t
    for action in ({"kind": "click"}, {"kind": "type", "text": "x", "then_enter": True},
                   {"kind": "select", "option": leak}):
        data = gate.canonical(gate.build_packet(reading, t, action, task="t",
                                                session="signed_in", site="S"))
        assert b"PRIVATEQ" not in data, action
    other = json.loads(json.dumps(reading))
    other["target"]["submission"]["entries"] = [[leak, leak + "2"]]
    other["controls"][-1]["state"]["selected"] = [leak, leak]
    assert "PRIVATEQ" not in json.dumps(gate.difference(reading, other))


def test_frames_join_the_page_reading():
    """r1: another frame's blocks and controls join the reading with their ids kept apart; a
    frame the page-view gate does not admit is a placeholder whose content is compared by
    digest, so a change in it is a difference and the final read differs."""
    base = invoice_table()
    child = {"href": "https://pay.example/frame", "title": "", "complete": True,
             "blocks": [{"text": "Fee € 2,00", "containers": [1], "record": 11}],
             "controls": [_control(0, "button", "Confirm", block=0, record=11, container=1)],
             "target": None, "target_idx": -1, "has_cc": True}
    shown = json.loads(json.dumps(base))
    shown["blocks"], shown["controls"] = list(shown["blocks"]), list(shown["controls"])
    gate.merge_frame(shown, child, 1, shown=True)
    assert shown["blocks"][-1]["text"] == "Fee € 2,00" and shown["has_cc"] is True
    assert shown["blocks"][-1]["record"] != 11 and shown["controls"][-1]["record"] != 11
    assert shown["controls"][-1]["idx"] == len(base["controls"])
    hidden_a = json.loads(json.dumps(base))
    gate.merge_frame(hidden_a, child, 1, shown=False)
    assert hidden_a["blocks"][-1]["text"] == gate.FRAME_WITHHELD
    assert "Fee" not in json.dumps(gate.build_packet(hidden_a, base["controls"][1], {"kind": "click"},
                                                     task="t", session="signed_in", site="S"))
    changed = dict(child, blocks=[{"text": "Fee € 9,00", "containers": [1], "record": 11}])
    hidden_b = json.loads(json.dumps(base))
    gate.merge_frame(hidden_b, changed, 1, shown=False)
    assert not gate.same_reading(hidden_a, hidden_b)
    assert gate.difference(hidden_a, hidden_b) == {"frames_not_shown_changed": [1]}
    assert gate.same_reading(hidden_a, json.loads(json.dumps(hidden_a)))


def test_difference_shows_whole_values_and_masked_changes():
    """r2: a change past a value's first 200 characters is in the difference, whole; a change
    that masking hides is still marked; the chosen option's label is masked in the packet."""
    a = invoice_table()
    a["controls"].append(_control(9, "textbox", "Note", tag="textarea",
                                  state={"value": "x " * 150 + "I accept a fee of 10 euros."}))
    b = json.loads(json.dumps(a))
    b["controls"][-1]["state"]["value"] = "x " * 150 + "I accept a fee of 900 euros."
    d = gate.difference(a, b)
    assert "900 euros" in d["controls_changed"][0]["after"]
    assert gate.control_line(a["controls"][-1]) == gate.control_line(b["controls"][-1])
    t1 = "ref Zq81Yt7Wm2Pk5Rn9Xs4Bv6Cd3Lh0Fg"
    c = json.loads(json.dumps(a))
    c["blocks"][0]["text"] = t1
    e = json.loads(json.dumps(c))
    e["blocks"][0]["text"] = "ref Yq81Yt7Wm2Pk5Rn9Xs4Bv6Cd3Lh0Fh"
    changed = gate.difference(c, e)["blocks_changed"][0]
    assert changed["masked_text_changed"] is True and "Zq81" not in json.dumps(changed)
    f = json.loads(json.dumps(a))
    f["controls"][-1]["state"]["value"] = t1
    g = json.loads(json.dumps(f))
    g["controls"][-1]["state"]["value"] = "ref Yq81Yt7Wm2Pk5Rn9Xs4Bv6Cd3Lh0Fh"
    assert gate.difference(f, g)["controls_changed"][0]["masked_text_changed"] is True
    url = "https://bank.example/pay?token=Zq81Yt7Wm2Pk5Rn9Xs4Bv6Cd3Lh0Fg"  # gitleaks:allow - synthetic fixture; this test exists to prove masking works
    pkt = gate.build_packet(a, a["controls"][1], {"kind": "select", "option": url}, task="t",
                            session="signed_in", site="S")
    assert "token" not in json.dumps(pkt) and pkt["action"]["option"].startswith("https://bank.example")


def test_re_resolution_counts_only_the_targets_frame():
    """r4: a match is a control in a frame at the target's own frame address; a matching control
    of another frame never resolves the target."""
    reading = invoice_table()
    for c in reading["controls"]:
        c["frame_href"] = "https://shop.example/facturen/"
    other = dict(reading["controls"][1], idx=9, frame_href="https://shop.example/frame")
    reading["controls"].append(other)
    frozen = gate.target_facts(reading["controls"][1])
    assert gate.resolve_matches(reading, frozen, "https://shop.example/frame") == [9]
    assert gate.resolve_matches(reading, frozen, "https://shop.example/facturen/") == [1]
    assert gate.resolve_matches(reading, frozen, "https://shop.example/gone") == []


SUB = {"method": "post", "action": "https://shop.example/i?token=Zq81Yt7Wm2Pk5Rn9Xs4Bv6Cd3Lh0Fg",  # gitleaks:allow - synthetic fixture; this test exists to prove masking works
       "enctype": "application/x-www-form-urlencoded", "target": "",
       "submitter": {"role": "button", "name": "View invoice"},
       "entries": [["invoice", "42"], ["operation", "view"], ["pin", "[not shown]"]]}


def test_a_submission_is_shown_compared_and_differenced_whole():
    """A: the submission the browser would build is in the packet (masked, destination as
    origin and masked path), in the frozen facts, in the final read and in the difference."""
    a = invoice_table()
    t = dict(a["controls"][1], is_submitter=True, submit_action=SUB["action"], submit_method="post",
             submit_target="", submission=SUB)
    a["target"] = t
    pkt = gate.build_packet(a, t, {"kind": "click"}, task="t", session="signed_in", site="S")
    shown = pkt["target"]["submission"]
    assert shown["entries"] == [["invoice", "42"], ["operation", "view"], ["pin", "[not shown]"]]
    assert shown["destination"]["path"] == "/i" and "token" not in json.dumps(pkt)
    assert shown["submitter"] == {"role": "button", "name": "View invoice"}
    b = json.loads(json.dumps(a))
    b["target"]["submission"]["entries"][1] = ["operation", "delete"]
    b["target"]["submission"]["submitter"]["name"] = "Delete invoice"
    assert not gate.same_reading(a, b)
    d = gate.difference(a, b)
    assert d["submission_changed"]["after"][0]["entries"][1] == ["operation", "delete"]
    assert gate.target_facts(a["target"]) != gate.target_facts(b["target"])
    many = dict(SUB, entries=[["f%d" % i, "x" * 300] for i in range(150)])
    s = gate.shown_submission(many, "https://shop.example/")
    assert len(s["entries"]) == 100 and s["entries_truncated"] and len(s["entries"][0][1]) == 200
    assert len(gate.shown_submission(many, "https://shop.example/", cut=False)["entries"]) == 150


def test_a_the_raw_digest_binds_what_masking_hides_and_never_reaches_a_packet():
    """A after a2: a change only in a value not shown (the salted ``raw`` digest) is a change in
    the frozen facts and the final read, flagged in the difference; the digest is never shown."""
    a = invoice_table()
    t = dict(a["controls"][1], is_submitter=True, submit_action=SUB["action"], submit_method="post",
             submit_target="", submission=dict(SUB, raw="1" * 64))
    a["target"] = t
    pkt = gate.build_packet(a, t, {"kind": "click"}, task="t", session="signed_in", site="S")
    assert "raw" not in pkt["target"]["submission"] and "1" * 64 not in json.dumps(pkt)
    b = json.loads(json.dumps(a))
    b["target"]["submission"]["raw"] = "2" * 64
    assert not gate.same_reading(a, b)
    assert gate.target_facts(a["target"]) != gate.target_facts(b["target"])
    d = gate.difference(a, b)
    assert d["submission_changed"]["masked_text_changed"] is True
    assert d["submission_changed"]["before"] == d["submission_changed"]["after"]
    assert "1" * 64 not in json.dumps(d)


def test_b_one_secret_rule_is_spliced_into_every_script():
    """B: one secret function, spliced into the page reading, the text facts and the value
    reader; nothing else in the gate's scripts decides secrecy."""
    for script in (gate.READ, gate._TEXT_FACTS, gate._VALUE):
        assert "const secretEl" in script and "/*SECRET*/" not in script
        assert script.count("current-password|new-password|one-time-code|cc-") == 1
    from casa_browser import observe
    assert "hidesSecret" in observe._secret_field_js()
    # every value or label read goes through one function
    assert "const valueOf" in gate.READ and "e.innerText" not in gate.READ.replace("valueOf(e, () => e.innerText)", "")


def test_read_page_reads_only_the_sessions_page(monkeypatch):
    """S4-4 (r2 Astra 1a-y): the page a packet is read from is the session's own, asked of the
    page-view gate before and after the read; another origin's page is never read."""
    import asyncio
    from types import SimpleNamespace
    import proxyharness as h
    from casa_browser import gate as gate_mod
    policy = h.account_policy()
    reads = []

    async def fake_read(frame, *args, **kw):
        reads.append(frame.url)
        return gate_mod.Reading(dict(href=frame.href, title="T", complete=True, blocks=[],
                                     controls=[], has_cc=False), None)

    def page(url, href=None):
        frame = SimpleNamespace(url=url, href=href or url, is_detached=lambda: False,
                                parent_frame=None)
        return SimpleNamespace(url=url, main_frame=frame, frames=[frame])

    monkeypatch.setattr(gate_mod, "read", fake_read)
    ops = gate_mod.PageOps()

    async def no_shadow(_page):
        return False
    ops.closed_shadow = no_shadow
    got = asyncio.run(ops.read_page(page("https://site.example/a"), policy=policy))
    assert got.data["href"] == "https://site.example/a" and reads == ["https://site.example/a"]
    reads.clear()
    with pytest.raises(LookupError):
        asyncio.run(ops.read_page(page("https://other.example/x"), policy=policy))
    assert reads == []                                   # never read
    with pytest.raises(LookupError):                     # moved off the site during the read
        asyncio.run(ops.read_page(page("https://site.example/a", "https://other.example/x"),
                                  policy=policy))


def test_read_page_shows_a_frame_only_by_its_own_reading(monkeypatch):
    """r3 Astra 1a-y, generalised: another frame's reading is shown only when the gate admits
    the address the reading itself reports, the frame's address before the read; a frame that
    was elsewhere when read is a digest, whatever its address afterwards."""
    import asyncio
    from types import SimpleNamespace
    import proxyharness as h
    from casa_browser import gate as gate_mod
    policy = h.account_policy()
    own, foreign = "https://site.example/a", "https://other.example/private"
    main = SimpleNamespace(url=own, is_detached=lambda: False, parent_frame=None)
    child = SimpleNamespace(url=foreign, is_detached=lambda: False, parent_frame=main)
    page = SimpleNamespace(url=own, main_frame=main, frames=[main, child])

    async def fake_read(frame, *args, **kw):
        text = "Foreign balance" if frame is child else "Own page"
        data = dict(href=frame.url, title="T", complete=True, has_cc=False, controls=[],
                    blocks=[dict(text=text, containers=[], record=None)])
        if frame is child:
            child.url = own                      # it navigated after the extraction
        return gate_mod.Reading(data, None)

    async def no_shadow(_page):
        return False
    monkeypatch.setattr(gate_mod, "read", fake_read)
    ops = gate_mod.PageOps()
    ops.closed_shadow = no_shadow
    got = asyncio.run(ops.read_page(page, policy=policy))
    texts = [b["text"] for b in got.data["blocks"]]
    assert "Foreign balance" not in texts and len(got.data.get("frames_withheld") or []) == 1


def test_signin_reading_shows_no_control_value():
    """Slice 5 (d1 Astra B): a sign-in reading lists each control by role and name only — no
    value, state or secret of any field, a revealed password included — and says whether a
    password field is shown."""
    from casa_browser import gate
    data = {"href": "https://a.example/account/12345678?x=1", "title": "Account",
            "blocks": [{"text": "Welcome back"}, {"text": "x" * 4000}],
            "controls": [{"role": "textbox", "name": "Email", "input_type": "email",
                          "state": {"value": "alice-example"}},
                         {"role": "textbox", "name": "Password", "input_type": "text",
                          "state": {"value": "test-secret"}, "secret": True},
                         {"role": "textbox", "name": "Old", "input_type": "password",
                          "state": {"value": "older-secret"}}]}
    out = gate.signin_reading(data)
    assert out == {"title": "Account", "host": "a.example", "path": "/account/[masked]",
                   "blocks": ["Welcome back"], "controls": ["textbox: Email",
                                                            "textbox: Password", "textbox: Old"],
                   "password_field": True}
    blob = json.dumps(out)
    assert not any(v in blob for v in ("alice-example", "test-secret", "older-secret"))


def test_read_page_shows_a_written_frame_by_the_one_frame_rule(monkeypatch):
    """Slice 5m: a frame with no address of its own (about:blank, a document the page wrote)
    is shown under its shown parent's origin, a target inside it is read; the same kind of
    frame under a withheld frame is a digest, and so is a sandboxed (opaque) one."""
    import asyncio
    from types import SimpleNamespace
    import proxyharness as h
    from casa_browser import gate as gate_mod
    policy = h.account_policy()
    own, o = "https://site.example/a", "https://site.example"
    main = SimpleNamespace(url=own, is_detached=lambda: False, parent_frame=None)
    foreign = SimpleNamespace(url="https://other.example/f", is_detached=lambda: False,
                              parent_frame=main)
    written = SimpleNamespace(url="about:blank", is_detached=lambda: False, parent_frame=main)
    nested = SimpleNamespace(url="about:blank", is_detached=lambda: False, parent_frame=foreign)
    boxed = SimpleNamespace(url="about:srcdoc", is_detached=lambda: False, parent_frame=main)
    inside = SimpleNamespace(url=own + "/in", is_detached=lambda: False, parent_frame=foreign)
    sandboxed = SimpleNamespace(url=own + "/sb", is_detached=lambda: False, parent_frame=main)
    page = SimpleNamespace(url=own, main_frame=main,
                           frames=[main, nested, inside, written, foreign, boxed, sandboxed])
    facts = {id(nested): ("about:blank", "https://other.example", "Nested"),
             id(written): (own, o, "Written"), id(foreign): ("https://other.example/f",
                                                             "https://other.example", "Foreign"),
             id(boxed): ("about:srcdoc", "null", "Boxed"), id(main): (own, o, "Own"),
             id(inside): (own + "/in", o, "Inside"), id(sandboxed): (own + "/sb", "null", "Sandboxed")}
    target = object()

    async def fake_read(frame, t=None, **kw):
        href, origin, text = facts[id(frame)]
        return gate_mod.Reading(dict(href=href, origin=origin, title="T", complete=True,
                                     has_cc=False, blocks=[dict(text=text, containers=[], record=None)],
                                     controls=[], target={"name": "x"} if t else None), None)

    async def no_shadow(_page):
        return False

    async def frame_of(_page, el):
        return written if el is target else main
    monkeypatch.setattr(gate_mod, "read", fake_read)
    ops = gate_mod.PageOps()
    ops.closed_shadow, ops.frame_of = no_shadow, frame_of
    got = asyncio.run(ops.read_page(page, policy=policy))
    texts = [b["text"] for b in got.data["blocks"]]
    assert "Written" in texts, texts
    assert not {"Nested", "Foreign", "Boxed", "Inside", "Sandboxed"} & set(texts), texts
    assert len(got.data["frames_withheld"]) == 5
    aimed = asyncio.run(ops.read_page(page, target, policy=policy))
    assert aimed.data["blocks"][0]["text"] == "Written" and aimed.target == {"name": "x"}
    facts[id(written)] = ("about:blank", "https://other.example", "Written")   # not its parent's
    with pytest.raises(LookupError):
        asyncio.run(ops.read_page(page, target, policy=policy))


def _plain_packet(**kw):
    reading = {"href": "https://a.example/x", "title": "T", "complete": True, "has_cc": False,
               "blocks": [{"text": "Hello", "containers": [], "record": None}], "controls": []}
    return gate.build_packet(reading, None, {"kind": "back"}, task="t", session="signed_in",
                             site="A", **kw)


def test_a_packet_with_neither_stays_byte_identical():
    """The check's measured packets keep their bytes: no member unless it is present."""
    bytes_ = gate.canonical(_plain_packet())
    assert bytes_ == (b'{"action":{"kind":"back"},"page":{"blocks":[{"n":1,"text":"Hello"}],"controls":[],'
                      b'"page_truncated":false},"run_so_far":[],"target":{"role":"page"},"task":"t",'
                      b'"where":{"host":"a.example","path":"/x","session":"signed_in","site":"A","title":"T"}}')
    both = _plain_packet(plan="p", approved="q")
    assert both["plan"] == "p" and both["approved"] == "q"
    assert gate.canonical(_plain_packet(plan=None, approved=None)) == bytes_


def test_go_allowed():
    click, enter = {"action": {"kind": "click"}}, {"action": {"kind": "type", "then_enter": True}}
    for category in gate.GO_CATEGORIES:
        assert gate.go_allowed(category, click) and gate.go_allowed(category, {**click, "approved": "p"})
        assert gate.go_allowed(category, enter)
    assert gate.go_allowed("account_change", {**click, "approved": "p"})
    assert not gate.go_allowed("account_change", click)
    assert not gate.go_allowed("account_change", {**click, "plan": "p"})
    assert not gate.go_allowed("account_change", {**enter, "approved": "p"})
    for category in ("payment", "commitment", "personal_data", "unclear"):
        assert not gate.go_allowed(category, {**click, "approved": "p"})
        assert not gate.go_allowed(category, click)


def test_an_approved_go_is_held_to_all_the_judged_packet_was_built_from():
    base = {"complete": False, "title": "T", "href": "https://a.example/x", "has_cc": False,
            "blocks": [{"text": "a"}], "controls": []}
    assert not gate.same_reading(base, dict(base))      # a tap's final read wants a complete one
    assert gate.same_as_judged(base, dict(base))        # a capped reading: what it shows
    for change in ({"blocks": [{"text": "a fee"}]}, {"title": "Charges EUR 25"},
                   {"href": "https://a.example/checkout"}, {"has_cc": True},
                   {"frames_withheld": ["x"]}, {"target": {"submission": {"entries": [1]}}},
                   {"controls": [{"role": "button", "name": "Pay"}]}):
        assert not gate.same_as_judged(base, {**base, **change}), change


# --------------------------------------------------------------------------- past the list's cap

def capped_reading(all_controls=("d1",)) -> dict:
    """A reading the control cap left short: its list holds what was in view."""
    return {**invoice_table(), "complete": False, "all_controls": list(all_controls)}


def targeted(reading: dict, name="Alice", record=7) -> dict:
    return {**reading, "target": {"record": record, "name": "Remove"},
            "blocks": reading["blocks"] + [{"text": name, "containers": [], "record": record}]}


def test_a_capped_reading_is_compared_by_its_digests_not_its_list():
    a = capped_reading()
    shifted = json.loads(json.dumps(a))
    shifted["controls"] = shifted["controls"][1:]
    assert gate.same_reading(a, shifted)
    assert gate.difference(a, shifted) == {}


def test_a_capped_final_read_binds_everything_beside_the_controls():
    a = capped_reading()
    for mutate in (lambda r: r.update(all_controls=["d2"]),
                   lambda r: r["blocks"][1].update(text="Klantnummer 1049."),
                   lambda r: r["blocks"].append({"text": "1 s ago", "containers": [], "record": None}),
                   lambda r: r.update(frames_withheld=["x"]),
                   lambda r: r.update(target={"submission": {"raw": "other"}}),
                   lambda r: r.update(blind=True)):
        b = json.loads(json.dumps(a))
        mutate(b)
        assert not gate.same_reading(a, b) and not gate.same_reading(b, a), b


def test_one_reading_capped_and_one_not_is_unequal():
    a = capped_reading()
    fits = {k: v for k, v in a.items() if k != "all_controls"} | {"complete": True}
    assert not gate.same_reading(a, fits) and not gate.same_reading(fits, a)
    assert gate.difference(a, fits) == {"controls_beyond_list_changed": True}


def test_without_a_digest_the_listed_controls_decide_as_before():
    a = invoice_table()
    b = json.loads(json.dumps(a))
    b["controls"][0]["state"] = {"disabled": True}
    assert not gate.same_reading(a, b)
    assert "all_controls" not in a and not gate.blind(a)


def test_an_incomplete_reading_with_no_digest_counts_as_blind():
    assert gate.blind({"complete": False}) and gate.blind({})
    assert gate.blind({"complete": True, "blind": True})
    assert gate.blind({"complete": False, "all_controls": ["d"], "blind": True})
    assert not gate.blind({"complete": False, "all_controls": ["d"]})
    assert not gate.blind({"complete": True})


def test_same_as_judged_ignores_the_digests_and_holds_the_list():
    a = capped_reading()
    assert gate.same_as_judged(a, {**a, "all_controls": ["d2"]})
    shifted = json.loads(json.dumps(a))
    shifted["controls"] = shifted["controls"][1:]
    assert not gate.same_as_judged(a, shifted)


def test_the_target_record_is_its_own_blocks_text():
    a = targeted(invoice_table())
    assert gate.target_record(a) == ["Alice"]
    assert gate.target_record(invoice_table()) == []                       # no target
    assert gate.target_record({**a, "target": {"record": None}}) == []     # outside any record
    b = targeted(invoice_table(), name="Bob", record=9)
    assert gate.target_record(b) == ["Bob"]
    # The record's id belongs to one reading: the same text under another id is the same record.
    assert gate.same_reading(a, targeted(invoice_table(), record=9))


@pytest.mark.parametrize("capped", [False, True])
def test_a_target_moved_to_another_record_is_a_difference(capped):
    base = capped_reading() if capped else invoice_table()
    a, b = targeted(base), targeted(base)
    b["blocks"][-1] = {"text": "Alice", "containers": [], "record": 8}
    b["blocks"].append({"text": "Bob", "containers": [], "record": 7})
    a["blocks"].append({"text": "Bob", "containers": [], "record": 8})
    assert [x["text"] for x in a["blocks"]] == [x["text"] for x in b["blocks"]]
    assert not gate.same_reading(a, b)
    assert gate.difference(a, b) == {"target_record_changed": {"before": ["Alice"],
                                                               "after": ["Bob"]}}


def test_a_target_record_change_is_shown_masked():
    secret = "tok_" + "aB3dE9fG2hJ7kL4mN8pQ6rS1"
    a, b = targeted(invoice_table(), name=secret), targeted(invoice_table(), name=secret + "x")
    changed = gate.difference(a, b)["target_record_changed"]
    assert secret not in json.dumps(changed)


def test_unequal_digests_keep_a_listed_change_and_drop_the_lists_noise():
    a = capped_reading()
    b = json.loads(json.dumps(a))
    b["all_controls"] = ["d2"]
    b["controls"][0]["state"] = {"disabled": True}         # a listed control changed
    b["controls"].pop()                                    # the list shifted: one went
    b["controls"].append({"role": "button", "name": "Item 400"})     # and another came
    d = gate.difference(a, b)
    assert d["controls_beyond_list_changed"] is True and len(d["controls_changed"]) == 1
    assert "controls_added" not in d and "controls_removed" not in d


def test_equal_digests_drop_every_controls_key_and_keep_the_rest():
    a = capped_reading()
    b = json.loads(json.dumps(a))
    b["controls"][0]["state"] = {"disabled": True}
    b["blocks"].append({"text": "A fee", "containers": [], "record": None})
    assert gate.difference(a, b) == {"blocks_added": ["A fee"]}


def test_a_blind_frame_makes_the_page_blind_shown_or_withheld():
    child = {"href": "https://a.example/f", "complete": False, "blind": True, "blocks": [],
             "controls": []}
    for shown in (True, False):
        base = invoice_table()
        gate.merge_frame(base, child, 1, shown=shown)
        assert base["blind"] is True and base["complete"] is False
    base = invoice_table()
    gate.merge_frame(base, dict(child, blind=None, complete=True), 1, shown=True)
    assert "blind" not in base and base["complete"] is True


def _frames(*datas, shown=None, target=0):
    from types import SimpleNamespace
    frames = [SimpleNamespace(n=i) for i in range(len(datas))]
    got = [(f, None if d is None else gate.Reading(d)) for f, d in zip(frames, datas)]
    views = {id(f): ((shown or {}).get(i, True), "o") for i, f in enumerate(frames)}
    return frames[target], got, views


def _frame(name="Own", **extra):
    return {"href": "https://a.example/", "complete": True, "has_cc": False, "blocks": [],
            "controls": [_control(0, "button", name)], "target": None, "target_idx": -1, **extra}


def test_assemble_adds_nothing_when_nothing_overflowed():
    data = gate.PageOps._assemble(*_frames(_frame(), _frame("Child")), False, False).data
    assert "all_controls" not in data and "blind" not in data and data["complete"] is True


def test_assemble_keeps_one_value_per_shown_frame_in_frame_order():
    capped = _frame(complete=False, all_controls="page-digest")
    small, withheld = _frame("Share publicly"), _frame("Elsewhere")
    args = _frames(capped, small, withheld, shown={2: False})
    data = gate.PageOps._assemble(*args, False, False).data
    assert data["all_controls"] == ["page-digest", gate.listed_digest(small)]
    assert len(data["frames_withheld"]) == 1
    # the same list when the target's frame, the reading's first, is the small one
    data = gate.PageOps._assemble(*_frames(capped, small, withheld, shown={2: False}, target=1),
                                  False, False).data
    assert data["all_controls"] == ["page-digest", gate.listed_digest(small)]


def test_assemble_a_small_frames_change_changes_its_value():
    capped = _frame(complete=False, all_controls="page-digest")
    a = gate.PageOps._assemble(*_frames(capped, _frame("Share")), False, False).data
    b = gate.PageOps._assemble(*_frames(capped, _frame("Keep")), False, False).data
    assert a["all_controls"][0] == b["all_controls"][0]
    assert a["all_controls"][1] != b["all_controls"][1]


def test_listed_digest_binds_what_a_control_does_and_no_id_of_one_read():
    base = _control(0, "button", "Send")
    def value(**change):
        return gate.listed_digest({"controls": [{**base, **change}]})
    for key in gate.COMPARED:
        assert value(**{key: "other"}) != value(), key
    for key in ("idx", "block", "record", "container"):
        assert value(**{key: 99}) == value(), key


def test_assemble_an_overflow_only_in_a_withheld_frame_still_compares_by_digest():
    capped = _frame("Elsewhere", complete=False, all_controls="its-digest")
    own = _frame()
    data = gate.PageOps._assemble(*_frames(own, capped, shown={1: False}), False, False).data
    assert data["complete"] is False and "blind" not in data
    assert data["all_controls"] == [gate.listed_digest(own)]


def test_assemble_an_unreadable_frame_or_a_closed_shadow_is_blind():
    data = gate.PageOps._assemble(*_frames(_frame(), None), False, False).data
    assert data["blind"] is True and data["complete"] is False
    data = gate.PageOps._assemble(*_frames(_frame()), False, True).data
    assert data["blind"] is True and data["complete"] is False
