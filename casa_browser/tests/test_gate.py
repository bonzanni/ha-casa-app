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
