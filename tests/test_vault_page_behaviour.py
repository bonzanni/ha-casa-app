"""The secrets page's script, run for real: a value hidden while a Show or a Copy is in flight
is written nowhere afterwards.

A value can arrive after the moment it should be gone: the person taps Show or Copy, then taps
Hide, the 30 seconds a Show or Copy has from its tap, or a shown value from when it appeared,
run out, or the page goes out of sight before the answer or the clipboard comes back.
`tests/js/vault_page_harness.js` runs the real `page.js` against a small stand-in for the
browser objects it touches, holds the server's answer and the clipboard back, runs its timers on
a simulated clock, and settles them after the hiding event. In every case the value must be in
neither the page, a node taken off it, an operation of the script, a clipboard call nor another
text box, and the single-use code the server sent back must still reach the form, so the next
tap works; a code is never sent twice, and a request carries its own form's code and field (the
stand-in server answers nothing else). A block runs one Show or Copy at a time, the 30 seconds
hold even when the page runs its timer late, and a form's Show and Copy are both off only while
its request is out. Controls show the harness does see a value where one belongs, and only there. It also plays what the script adds to the forms (a closed slot sends
nothing and keeps its draft, a suggestion fills the label, Replace and Remove turn boxes on and
off, an open slot sends exactly its own label and value, a slip is said before anything is sent,
none of it sends the form), the list's Copy, and the Hide of a page a Show answered.

The pages are the ones `Page.handle` renders, handed to the harness as trees. The test needs
`node` on PATH (no packages); without it, it is skipped.
"""
import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from vault_page import Page, Request
from vault_store import Field, Store

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests/js/vault_page_harness.js"
SCRIPT = REPO / "casa/rootfs/opt/casa/vault_static/page.js"
SECRET = "REVIEW_SECRET_7Hq2"
OTHER = "OTHER_ROW_VALUE_3Kd9"
SCENARIOS = [
    "show pending, page hide event, answer",
    "show pending, page hidden and back, answer",
    "show done, the value goes at 30 s and not before",
    "copy pending on ClipboardItem, Hide, clipboard refuses",
    "copy pending on writeText, Hide, clipboard refuses",
    "copy pending on the answer (no clipboard), page hidden and back, answer",
    "copy pending on ClipboardItem and the answer, page hidden and back, answer, refuses",
    "copy pending on writeText and the answer, page hidden and back, answer, refuses",
    "copy on writeText, the clipboard waits past 30 s, then refuses",
    "copy on ClipboardItem, the clipboard waits past 30 s, then refuses",
    "a shown value copied at 20 s (no clipboard) still goes at 30 s",
    "show, Hide, Show again sends the code the first answer brought",
    "show answered after 45 s shows nothing, says it took too long, and the next code arrives",
    "copy past 30 s whose answer is refused says it took too long and sends nothing again",
    "a finished Copy does not cut short a Show tapped 25 s later",
    "a Show answered at 29 s stays 30 s from then",
    "show, Hide at 5 s, Show at 25 s answered at 31 s: the second value stays its own 30 s",
    "copy pending on the clipboard, then Show, the clipboard refuses: the value goes 30 s after"
    " it appeared",
    "copy pending on ClipboardItem, then Show, the clipboard refuses: the value goes 30 s after"
    " it appeared",
    "copy pending on the clipboard, then Show, the clipboard copies: the value goes 30 s after"
    " it appeared",
    "copy pending on the clipboard, Show pending, the clipboard refuses: nothing shows before"
    " the Show, whose Hide works",
    "two Copies pending on the clipboard: the second ends the first, and hiding the page leaves"
    " nothing",
    "two Copies pending on ClipboardItem: the second ends the first, and hiding the page leaves"
    " nothing",
    "show answered after its 30 s, before its timer ran, shows nothing and says it took too long",
    "copy answered after its 30 s, before its timer ran, copies nothing",
    "copy on ClipboardItem answered after its 30 s, before its timer ran, copies nothing",
    "the tapped button is off while its request is out and on again once it is answered",
    "copy on ClipboardItem calls the clipboard once, inside the tap",
    "control: show displays the value",
    "control: copy hands the value to the clipboard",
    *(f"{first} out: {other} is off and takes no tap; both on once it is {how}"
      for first, other in (("show", "copy"), ("copy", "show")) for how in ("answered", "refused")),
    *(f"{where} {how}, tapped again: nothing is sent twice"
      for where in ("show", "copy", "list copy") for how in ("refused (409)", "lost")),
    "add form, a closed slot sends nothing, opening it turns its boxes on and focuses its value",
    "add form, closing a slot keeps what was typed and sends none of it",
    "add form, each extra slot sends its own label and value while open and nothing while closed",
    "edit form, each extra slot sends its own label and value while open and nothing while closed",
    "add form, a suggestion fills the label and marks itself, typing unmarks it",
    "add form, a slip is said before anything is sent",
    "edit form, Replace turns the new-value box on and keeps its draft when turned off",
    "edit form, Remove turns a value off and Undo turns it back as it was",
    "list, Copy hands the value to the clipboard and says so",
    "list, a refused clipboard shows the value in the row until its Hide",
    "list, a value shown after a refused clipboard goes at 30 s and not before",
    "list, a shown value copied at 20 s (no clipboard) still goes at 30 s",
    "list, a shown value goes when the page is hidden",
    "list, copy pending on the answer, page hide event, answer",
    "list, copy on writeText, the clipboard waits past 30 s, then refuses",
    "list, Copy of an empty value copies nothing and says Empty",
    "shown page, its Hide link becomes a button that hides the value",
    "shown page, its value goes at 30 s and not before",
    "shown page loaded out of sight: the value is gone at once",
]
_VOID = {"meta", "link", "input", "br", "img", "hr"}


class _Tree(HTMLParser):
    """The page as nested lists: [tag, {attrs}, ...children], text as strings."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = None
        self.stack = []

    def handle_starttag(self, tag, attrs):
        node = [tag, {name: value or "" for name, value in attrs}]
        if self.stack:
            self.stack[-1].append(node)
        else:
            self.root = node
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack:
            self.stack[-1].append(data)


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH; the page script's behaviour test needs it")
    tmp = tmp_path_factory.mktemp("vault-page-behaviour")
    store = Store.open(tmp / "store", None)
    one = store.create(title="t", category="PASSWORD", tags=[], writer="op", fields=[
        Field(id="", label="note", type="STRING", value="plain"),
        Field(id="", label="pw", type="CONCEALED", value=SECRET)])
    # The list: two rows with a Copy of their own before the one the scenarios tap.
    rows = {title: store.create(title=title, category="PASSWORD", tags=[], writer="op", fields=[
        Field(id="", label="pw", type="CONCEALED", value=value)])
        for title, value in (("alpha", OTHER), ("blank", ""), ("single", SECRET))}
    page = Page(store)
    get = {"x-ingress-path": "/p"}
    post = {**get, "content-type": "application/x-www-form-urlencoded",
            "sec-fetch-site": "same-origin"}
    show = f"/s/{one.id}/show"
    # What the stand-in server answers, by the path and field a request names.
    values = {f"/p/vault/s/{one.id}/show#1": SECRET}
    values.update({f"/p/vault/s/{item.id}/show#0": item.fields[0].value
                   for item in rows.values()})
    pages = {}
    for name, request in (
            ("page", Request("GET", f"/s/{one.id}", get, b"")),
            ("add", Request("GET", "/add", get, b"")),
            ("edit", Request("GET", f"/s/{one.id}/edit", get, b"")),
            ("list", Request("GET", "/", get, b"")),
            ("shown", Request("POST", show, post,
                              f"code={page.codes.issue(show)}&field=1".encode()))):
        html = page.handle(request).body
        assert (SECRET.encode() in html) == (name == "shown"), name
        assert OTHER.encode() not in html, name
        tree = _Tree()
        tree.feed(html.decode("utf-8"))
        pages[name] = tree.root
    bundle = tmp / "bundle.json"
    bundle.write_text(json.dumps({"secret": SECRET, "other": OTHER, "values": values,
                                  "pages": pages}))
    run = subprocess.run([node, str(HARNESS), str(SCRIPT), str(bundle)],
                         capture_output=True, text=True, timeout=120)
    print(run.stdout, run.stderr)
    lines = {}
    for line in run.stdout.splitlines():
        verdict, _, rest = line.partition(" ")
        name, _, why = rest.partition("\t") if verdict == "FAIL" else (rest, "", "")
        lines[name] = (verdict, why)
    return lines, run.stdout + run.stderr


def test_the_harness_plays_every_scenario(outcomes):
    lines, output = outcomes
    assert sorted(lines) == sorted(SCENARIOS), output


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_a_hidden_value_is_written_nowhere_and_the_next_code_arrives(outcomes, scenario):
    lines, output = outcomes
    assert scenario in lines, output
    verdict, why = lines[scenario]
    assert verdict == "ok", f"{scenario}: {why}"
