"""The secrets page's stylesheets and scripts, which nginx serves from /opt/casa/vault_static.

The page looks like Casa Browser's pages because it uses the same two files:
`ha.css` and `theme.js` are copies of Casa Browser's, byte for byte. Its own
additions are `vault.css` and `page.js`. Under the page's Content-Security-Policy
nothing may come from elsewhere, and the script must never turn text into
markup or reach anything but the page's own forms. Every class the page's HTML
uses has a rule, so no part of the page is left unstyled.
"""
import re
from pathlib import Path
from urllib.parse import urlencode

import pytest

from vault_page import Page, Request
from vault_store import Field, Store

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "casa/rootfs/opt/casa/vault_static"
BROWSER_STATIC = REPO / "casa_browser/rootfs/opt/casa_browser/static"
FILES = ("ha.css", "vault.css", "theme.js", "page.js")
PREFIX = "/api/hassio_ingress/abc"


@pytest.mark.parametrize("ours,theirs", [("ha.css", "style.css"), ("theme.js", "theme.js")])
def test_the_shared_files_are_casa_browsers_own(ours, theirs):
    assert (STATIC / ours).read_bytes() == (BROWSER_STATIC / theirs).read_bytes(), (
        f"copy casa_browser/rootfs/opt/casa_browser/static/{theirs} "
        f"to casa/rootfs/opt/casa/vault_static/{ours} again")


def test_the_page_links_exactly_these_files():
    store_page = Page(None)
    body = store_page.handle(Request("GET", "/", {"x-ingress-path": PREFIX}, b"")).body.decode()
    linked = re.findall(re.escape(PREFIX) + r"/vault/static/([^\"]+)", body)
    assert sorted(linked) == sorted(FILES)
    assert sorted(p.name for p in STATIC.iterdir()) == sorted(FILES)


@pytest.mark.parametrize("name", FILES)
def test_nothing_is_loaded_from_elsewhere(name):
    text = (STATIC / name).read_text()
    for word in ("url(", "@import", "http:", "https:"):
        assert word not in text, (name, word)


def _code(text):
    """page.js without its comments."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$|\s//\s.*$", "", text)


def test_the_script_writes_text_only_and_talks_only_to_its_forms():
    text = (STATIC / "page.js").read_text()
    for word in ("eval", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                 "Function(", "XMLHttpRequest", "sendBeacon", "WebSocket", "localStorage",
                 "sessionStorage"):
        assert word not in text, word
    calls = re.findall(r"fetch\(([^,)]*)", text)
    assert calls == ["form.action"]


def test_the_script_defines_no_globals():
    lines = [line for line in _code((STATIC / "page.js").read_text()).splitlines()
             if line.strip()]
    assert lines[0] == "(() => {"
    assert lines[-1] == "})();"
    assert "'use strict';" in lines[1]



def test_every_await_is_followed_by_the_check_that_its_value_was_not_hidden():
    """A value that arrives, or a clipboard that answers, after its field was hidden must not be
    written anywhere: after each await the script goes on only through `live(op)` (or hands the
    value over through `handOver(op)`, which checks it), or returns. The fetch itself is exempt:
    its only use of the answer is to store it under the same check."""
    lines = _code((STATIC / "page.js").read_text()).splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("  const ask = async"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "  };")
    assert "if (live(op)) op.value = value;" in "\n".join(lines[start:end])
    checked = 0
    for i, line in enumerate(lines):
        if "await " not in line or start <= i <= end:
            continue
        j = i
        while ";" not in lines[j]:
            j += 1
        if "handOver(op)" in "".join(lines[i:j + 1]):
            checked += 1
            continue
        j += 1
        while re.fullmatch(r"\s*(\}|\} catch \(err\) \{|\} finally \{)?\s*", lines[j]):
            j += 1
        assert ("live(op)" in lines[j] or "handOver(op)" in lines[j]
                or re.fullmatch(r"\s*return( true)?;", lines[j])), (line, lines[j])
        checked += 1
    assert checked >= 7

def _defined_classes():
    names = set()
    for name in ("ha.css", "vault.css"):
        css = re.sub(r"/\*.*?\*/", "", (STATIC / name).read_text(), flags=re.DOTALL)
        names.update(re.findall(r"\.(-?[A-Za-z_][\w-]*)", css))
    return names


def _rendered(store, tmp_path):
    """Every kind of page a person meets, by name, with a secret field shown on one."""
    one = store.create(title="wifi", category="PASSWORD", tags=[], writer="op", fields=[
        Field(id="", label="network", type="STRING", value="homenet"),
        Field(id="", label="notes", type="STRING", value="first\nsecond"),
        Field(id="", label="password", type="CONCEALED", value="secret-value"),
        Field(id="", label="empty", type="CONCEALED", value=""),
    ])
    key = store.create(title="key", category="SSH_KEY", tags=[], writer="op", fields=[
        Field(id="", label="fingerprint", type="STRING", value="SHA256:x"),
        Field(id="", label="private key", type="SSHKEY", value="k")])
    store.create(title="token", category="PASSWORD", tags=[], writer="page", fields=[
        Field(id="", label="credential", type="CONCEALED", value="t")])
    page = Page(store)
    headers = {"x-ingress-path": PREFIX}

    def get(path, of=page):
        return of.handle(Request("GET", path, headers, b"")).body.decode()

    def post(path, form, sent=None):
        if sent is None:
            sent = {**headers, "content-type": "application/x-www-form-urlencoded",
                    "origin": "https://ha.example", "x-forwarded-proto": "https",
                    "x-forwarded-host": "ha.example"}
        body = urlencode({"code": page.codes.issue(path), **form}).encode()
        return page.handle(Request("POST", path, sent, body)).body.decode()

    return {
        "list": get("/"),
        "empty list": get("/", Page(Store.open(tmp_path / "empty", None))),
        "add": get("/add"),
        "secret": get(f"/s/{one.id}"),
        "shown": post(f"/s/{one.id}/show", {"field": "2"}),
        "edit": get(f"/s/{one.id}/edit"),
        "delete": get(f"/s/{one.id}/delete"),
        "ssh key": get(f"/s/{key.id}"),
        "ssh key delete": get(f"/s/{key.id}/delete"),
        "refused form": post("/add", {"name": "", "label0": "x"}),
        "refused edit": post(f"/s/{one.id}/edit", {"add4": "1", "label4": "", "remove0": "1"}),
        "changed meanwhile": post(f"/s/{one.id}/edit", {"stamp": "old"}),
        "refused post": post("/add", {}, sent=headers),
        "not found": get("/nowhere"),
        "show by get": get(f"/s/{one.id}/show"),
        "unavailable": get("/", Page(None)),
    }


def _classes(body):
    return {name for attribute in re.findall(r'class="([^"]*)"', body)
            for name in attribute.split()}


def test_every_class_the_page_uses_is_styled(tmp_path):
    defined = _defined_classes()
    pages = _rendered(Store.open(tmp_path / "store", None), tmp_path)
    assert '<pre class="v-value v-revealed">\nsecret-value</pre>' in pages["shown"]
    assert 'class="v-actions v-reveal"' in pages["list"]
    assert 'aria-invalid="true"' in pages["refused form"]
    for name, body in pages.items():
        assert "style=" not in body, name
        assert _classes(body), name
        assert _classes(body) - defined == set(), name


def test_the_classes_the_script_adds_are_styled():
    added = set()
    for names in re.findall(r"className = '([^']*)'", _code((STATIC / "page.js").read_text())):
        added.update(names.split())
    assert added == {"v-value", "v-revealed", "v-dots", "v-button", "v-button--quiet"}
    assert added - _defined_classes() == set()


def test_nothing_stays_on_screen_and_no_page_has_a_bottom_bar(tmp_path):
    """The buttons are where the forms end and in the top bar, never in a bar a phone's keyboard
    can cover: no footer on any page and nothing sticky or fixed. The only button outside its form
    is the top bar's Save, which submits the form below it."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "vault.css").read_text(), flags=re.DOTALL)
    assert not re.search(r"position:\s*(sticky|fixed)", css)
    assert "footer" not in css
    pages = _rendered(Store.open(tmp_path / "store", None), tmp_path)
    for name, body in pages.items():
        assert "<footer" not in body, name
        outside = re.findall(r'\sform="([^"]*)"', body)
        assert set(outside) <= {"secret-form"}, name
        if outside:
            assert body.count('id="secret-form"') == 1, name
            assert outside == ["secret-form"], name


def _rules():
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "vault.css").read_text(), flags=re.DOTALL)
    return [(selector.strip(), rules) for selector, rules in
            re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def _font_sizes(rules):
    """Every font size a rule's declarations set, `font` shorthands included, as written."""
    sizes = re.findall(r"font-size:\s*([^;]+?)\s*(?:;|$)", rules)
    for shorthand in re.findall(r"(?:^|;)\s*font:\s*([^;]+?)\s*(?:;|$)", rules):
        if shorthand.strip() == "inherit":
            sizes.append("inherit")
        else:
            sizes += re.findall(r"(?:^|\s)([\d.]+[a-z%]*)(?:/|\s|$)",
                                shorthand.split(",")[0])[-1:]
    return sizes


def _px(size):
    """A font size in px on a 16px root, or None for one that depends on its parent."""
    match = re.fullmatch(r"([\d.]+)(px|rem)", size)
    if not match:
        return None
    return float(match.group(1)) * (16 if match.group(2) == "rem" else 1)


def test_boxes_keep_16px_text_targets_are_44px_and_nothing_floats():
    """iOS zooms into a box whose text is under 16px; a tap target under 44px is missed."""
    rules = dict(_rules())
    assert _font_sizes(rules["body"]) == ["16px"]
    for selector, declarations in _rules():
        assert "float" not in declarations, selector
        targets = [part.split()[-1] for part in selector.split(",")]
        if any(re.match(r"(input|textarea|\.v-input|\.v-textarea)\b", last) for last in targets):
            for size in _font_sizes(declarations):
                # a box's own size, never one taken from its parent (a label is 14px)
                assert (_px(size) or 0) >= 16, (selector, size)
    # every box carries its size itself
    for selector in ("input, textarea", ".v-input, .v-textarea"):
        assert [_px(size) for size in _font_sizes(rules[selector])] == [16], selector
    for selector in (".v-button", ".v-choice", ".v-label-options > summary"):
        assert "min-height: 44px" in rules[selector], selector
    assert "min-height: 48px" in rules[".v-input, .v-textarea"]


def test_every_toggle_keeps_a_checkbox_the_keyboard_reaches():
    """Replace, Remove and "Add another value" are drawn by their labels; the checkbox under
    each stays in the page, reachable by the keyboard, and is never taken out of it."""
    checkboxes = (".v-slot-enable", ".v-remove-input", ".v-replace-input", ".v-choice input")
    seen = set()
    for selector, declarations in _rules():
        for part in selector.split(","):
            last = part.strip().split()[-1]
            if any(re.fullmatch(re.escape(box.split()[-1]) + r"(:[\w-]+(\([^)]*\))?)*", last)
                   and (box.split()[0] in part) for box in checkboxes):
                seen.add(part.strip())
                assert not re.search(r"display:\s*none|visibility:\s*hidden|"
                                     r"pointer-events:\s*none|content-visibility", declarations), \
                    (selector, declarations)
    assert {".v-slot-enable", ".v-choice input"} <= seen
    assert "tabindex" not in (STATIC / "page.js").read_text()
