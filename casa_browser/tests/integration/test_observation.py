"""Task 7 in the image: the real extraction code, gate and screenshot masking, on branded
Chrome against a synthetic fixture site."""
from __future__ import annotations

import base64
import struct
import zlib

import pytest

import proxyharness as h
from browserprobe import run_scenario
from casa_browser.proxy_client import encode_policy
from sitefixture import ChromeSites

pytestmark = pytest.mark.docker

SITE, OTHER = "site-a.example", "site-b.example"
TOKEN = "tok_Q7w8E9r0T1y2U3i4"  # gitleaks:allow - synthetic fixture; this test exists to prove masking works
BEARER = f"https://{SITE}/invoices/in_1PqR9sT2uV3wX4yZ/download?token={TOKEN}"
KEY = "".join(["ghp_", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"])
PAGE = f"""<html><head><title>Invoices {BEARER}</title></head><body style="margin:0">
<h1>Invoices</h1>
<p>Ready: {BEARER}</p>
<a id=dl href="{BEARER}">Download invoice</a>
<a id=raw href="{BEARER}">{BEARER}</a>
<a id=img href="{BEARER}"><img alt="{BEARER}" width=40 height=20></a>
<button title="{BEARER}"></button><button></button>
<p id=err>Error: could not load {BEARER}</p>
<p id=key style="font-size:28px">Your API key: {KEY}</p>
<label>Password <input id=pw type=password value="CanaryPassword-0004" style="width:300px;height:40px"></label>
<iframe src="https://{OTHER}/frame" width=300 height=120 style="border:0"></iframe>
<script>document.getElementById('pw').value = 'CanaryPassword-0004';</script>
</body></html>""".encode()


def png_pixels(data: bytes):
    """Minimal PNG reader (8-bit RGB/RGBA, non-interlaced): width, height, rows of tuples."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    idat = b""
    while pos < len(data):
        n, = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            chunks["ihdr"] = body
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    w, hgt, depth, ctype = struct.unpack(">IIBB", chunks["ihdr"][:10])
    assert depth == 8 and ctype in (2, 6)
    bpp = 3 if ctype == 2 else 4
    raw = zlib.decompress(idat)
    stride = w * bpp
    rows, prev, i = [], bytearray(stride), 0
    for _ in range(hgt):
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        prev = line
    return w, hgt, bpp, rows


def all_black(px, box):
    w, hgt, bpp, rows = px
    x0, y0, bw, bh = (int(v) for v in box)
    assert 0 <= x0 and 0 <= y0 and x0 + 4 < w and y0 + 4 < hgt, (box, w, hgt)  # inside the image
    for y in range(y0 + 2, min(y0 + bh - 2, hgt)):
        for x in range(x0 + 2, min(x0 + bw - 2, w)):
            if rows[y][x * bpp:x * bpp + 3] != b"\x00\x00\x00":
                return False
    return True


def _policy(*_urls):
    """The account session of a site on SITE only: its pages belong to the session (design
    §4); OTHER is off the site."""
    s = h.site("sa", origins=(f"https://{SITE}",), login=())
    return encode_policy(h.account_policy(sites=(s,)))


def test_observation_no_raw_targets_and_masked_screenshot():
    with ChromeSites([SITE, OTHER]) as sites:
        sites.pages["/invoices"] = ("text/html", PAGE, {})
        sites.pages["/frame"] = ("text/html", b"<html><body>FRAME CONTENT</body></html>", {})
        url = f"https://{SITE}/invoices"
        policy = _policy(url)
        from casa_browser.proxy_client import decode_policy
        sites.install(decode_policy(policy))
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": url}, {"wait": 1},
                            {"observe": policy}, {"screenshot": policy},
                            {"box": "#pw"}, {"box": "#key"}, {"box": "h1"}, {"box": "iframe"}])
        obs = res[3]
        wire = repr(obs)
        for leak in ("/invoices/in_", TOKEN, "token=", KEY, "CanaryPassword-0004", "FRAME CONTENT"):
            assert leak not in wire, leak
        labels = [e["label_masked"] for e in obs["elements"]]
        assert "Download invoice" in labels
        assert any(l.startswith("(unnamed button") for l in labels)
        assert sum(1 for e in obs["elements"] if e["role"] == "link") == 3
        shot = res[4]
        assert shot["id"] and shot["files"] == [f"{shot['id']}.png"]   # only the final PNG
        png = base64.b64decode(shot["png"])
        assert KEY.encode() not in png and b"CanaryPassword" not in png
        px = png_pixels(png)
        assert all_black(px, res[5]), "password field not masked"
        # res[6], the secret text: text the page renders stays visible in the operator's own
        # screenshot (operator ruling 2026-10-20, a documented residual); the model never sees it.
        assert all_black(px, res[8]), "frame not masked"
        assert not all_black(px, res[7]), "control: the heading is visible"


def test_the_models_picture_blacks_secret_fields_and_frames():
    """The picture a model gets follows the operator screenshot's one rule, and leaves no file."""
    with ChromeSites([SITE, OTHER]) as sites:
        sites.pages["/invoices"] = ("text/html", PAGE, {})
        sites.pages["/frame"] = ("text/html", b"<html><body>FRAME CONTENT</body></html>", {})
        url = f"https://{SITE}/invoices"
        policy = _policy(url)
        from casa_browser.proxy_client import decode_policy
        sites.install(decode_policy(policy))
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": url}, {"wait": 1},
                            {"model_shot": policy}, {"box": "#pw"}, {"box": "h1"}, {"box": "iframe"}])
        shot = res[3]
        assert shot["png"] and shot["files"] == []
        px = png_pixels(base64.b64decode(shot["png"]))
        assert all_black(px, res[4]), "password field not masked"
        assert all_black(px, res[6]), "frame not masked"
        assert not all_black(px, res[5]), "control: the heading is visible"


def test_pushstate_fragment_and_iframe_gate():
    """The page-view gate runs on the page's full current address (route and fragment
    included) and admits exactly the session's own pages."""
    with ChromeSites([SITE, OTHER]) as sites:
        sites.pages["/app"] = ("text/html", b"<html><body><p>app home</p></body></html>", {})
        url = f"https://{SITE}/app"
        policy = _policy(url)
        from casa_browser.proxy_client import decode_policy
        sites.install(decode_policy(policy))
        res = run_scenario([
            {"open": {"site_id": "sa"}}, {"goto": url}, {"observe": policy},
            {"eval": "() => history.pushState({}, '', '/app/secret-route')"}, {"observe": policy},
            {"screenshot": policy},
            {"eval": "() => history.back()"}, {"wait": 1}, {"observe": policy},
            {"eval": "() => { location.hash = 'reset-code-123'; }"}, {"wait": 1}, {"observe": policy},
            # the page leaves the site: a top-level document off it (refused at the proxy) is
            # not this session's page, whatever the browser now shows
            {"eval": f"() => {{ location.href = 'https://{OTHER}/elsewhere'; }}"}, {"wait": 2},
            {"observe": policy}, {"screenshot": policy},
        ])
        assert isinstance(res[2], dict) and "app home" in res[2]["text"]
        # the gate reads the full current address: a client-side route and a fragment on the
        # site's own origin belong to the session (each a new generation)
        assert isinstance(res[4], dict) and "app home" in res[4]["text"], res[4]
        assert res[5]["id"] is not None
        assert isinstance(res[8], dict) and res[8]["generation"] > res[2]["generation"]
        assert isinstance(res[11], dict) and "app home" in res[11]["text"], res[11]
        assert res[14] == "refused:page_off_session" and res[15]["id"] is None, res[14:]
        assert not [r for r in sites.receipts.requests if r[1].split(":")[0] == OTHER]
        assert [e.reason for e in sites.events() if not e.allow and e.origin == f"https://{OTHER}"] \
            == ["site_lock"]


def test_stale_generation_after_navigation():
    with ChromeSites([SITE]) as sites:
        sites.pages["/a"] = ("text/html", b"<html><body><a href=/b>next</a></body></html>", {})
        sites.pages["/b"] = ("text/html", b"<html><body>b</body></html>", {})
        policy = _policy(f"https://{SITE}/a", f"https://{SITE}/b")
        from casa_browser.proxy_client import decode_policy
        sites.install(decode_policy(policy))
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": f"https://{SITE}/a"},
                            {"observe": policy}, {"click": "a"}, {"wait": 1}, {"observe": policy}])
        assert res[5]["generation"] > res[2]["generation"]
        assert {e["handle"] for e in res[2]["elements"]}.isdisjoint({e["handle"] for e in res[5]["elements"]})


AKIA = "".join(["AKIA", "IOSFODNN7EXAMPLE"])


def _shot_and_boxes(sites, path, html, boxes, extra_steps=()):
    sites.pages[path] = ("text/html", html.encode(), {})
    url = f"https://{SITE}{path}"
    policy = _policy(url)
    from casa_browser.proxy_client import decode_policy
    sites.install(decode_policy(policy))
    steps = [{"open": {"site_id": "sa"}}, {"goto": url}, {"wait": 1}, {"observe": policy},
             *extra_steps, {"screenshot": policy}] + [{"box": b} for b in boxes]
    res = run_scenario(steps)
    return res[3], res[4 + len(extra_steps)], res[5 + len(extra_steps):]


def test_model_text_has_no_editable_content_values_or_raw_roles():
    with ChromeSites([SITE, OTHER]) as sites:
        html = """<html><body><div contenteditable=true>Private edited text</div>
          <input type=submit value="Private submit value">
          <button role="/reset?code=private-code">Continue</button>
          <a href=/x role="https://site-a.example/acct/991?q=7">r</a></body></html>"""
        obs, shot, _ = _shot_and_boxes(sites, "/model", html, [])
        # slice 5j: an editable element's text is its value, never page text or a name
        assert [e.get("value_masked") for e in obs["elements"] if e["role"] == "textbox"] == \
            ["Private edited text"]
        wire = repr({**obs, "elements": [{k: v for k, v in e.items() if k != "value_masked"}
                                         for e in obs["elements"]]})
        for leak in ("Private edited text", "private-code", "/acct/991"):
            assert leak not in wire, leak
        # A submit button's value is what it shows: its name, unlike a field's value.
        assert "Private submit value" in [e["label_masked"] for e in obs["elements"]]
        assert {e["role"] for e in obs["elements"]} <= {"button", "link", "textbox"}


PIXEL = "data:image/gif;base64,R0lGODlhAQABAAAAACw="
SQUARE = "style='display:inline-block;width:20px;height:20px'"
NAMES_PAGE = f"""<html><body><form action=/a>
  <input type=submit value=Save><input type=submit value=Delete><input type=reset value=Clear>
  <input type=button value=Show><input type=image alt=Find src="{PIXEL}" {SQUARE}>
  <input value=Typed-Value-0301><input value=Typed-Value-0302 aria-label="Your name">
  <input type=search title="Search box"><textarea>Typed-Value-0303</textarea>
  <input type=password value=Secret-0304 aria-label=Secret-Label-0304 title=Secret-Title-0304>
  <input autocomplete=one-time-code value=Secret-0305 aria-label=Secret-Label-0305>
  <input autocomplete=cc-number value=Secret-0306 title=Secret-Title-0306>
  <button aria-label=Secret-Label-0307 {SQUARE}><input type=password></button>
  <input type=submit></form>
  <button aria-label=Close {SQUARE}></button><button title=Help {SQUARE}></button>
  <button aria-label=Hidden-Claim-0308>Visible wins</button>
  <input type=button value="Value wins" aria-label=Hidden-Claim-0309>
  <a href=/x><img alt="A picture" src="{PIXEL}" {SQUARE}></a>
  <a href=/y aria-label="Go to y" {SQUARE}></a>
  <div contenteditable=true aria-label=Editor>Typed-Value-0310</div>
  <select aria-label=Pick><option>One</option></select></body></html>"""


def test_an_element_with_no_visible_name_takes_what_the_page_calls_it():
    """Both readings, as real Chrome gives them: a button input's value and the page's naming
    attributes name an element with no visible name; visible text wins over them; a field's value
    never names it, and nothing on or holding a secret does."""
    with ChromeSites([SITE, OTHER]) as sites:
        sites.pages["/names"] = ("text/html", NAMES_PAGE.encode(), {})
        url = f"https://{SITE}/names"
        policy = _policy(url)
        from casa_browser.proxy_client import decode_policy
        sites.install(decode_policy(policy))
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": url}, {"wait": 1},
                            {"observe": policy}, {"gate_read": None}])
        labels = [e["label_masked"] for e in res[3]["elements"]]
        names = [c["name"] for c in res[4]["controls"]]
        for got in (labels, names):
            for want in ("Save", "Delete", "Clear", "Show", "Find", "Your name", "Search box",
                         "Close", "Help", "Visible wins", "Value wins", "A picture", "Go to y",
                         "Editor", "Pick"):
                assert want in got, (want, got)
            for leak in ("Typed-Value", "Secret-", "Hidden-Claim"):
                assert not [n for n in got if leak in n], (leak, got)


@pytest.mark.parametrize("path,html,boxes", [
    ("/shadow", """<html><body><x-card id=host></x-card><script>
        const r = document.getElementById('host').attachShadow({mode: 'open'});
        r.innerHTML = '<p id=k style="font-size:28px">key %s</p><input id=f type=password value="NL91ABNA0417164300" style="width:300px;height:40px"><iframe srcdoc="WITHHELD FRAME" width=200 height=80></iframe>';
        </script></body></html>""" % AKIA, ["x-card|#f", "x-card|iframe"]),
    ("/many", "<html><body>" + "<a href=/x style='font-size:4px'>l</a>" * 400 +
              "<input id=f type=password value='NL91ABNA0417164300' style='position:fixed;top:0;left:0;width:300px;height:40px'></body></html>", ["#f"]),
])
def test_screenshot_masks_every_secret_field_and_frame_or_is_not_taken(path, html, boxes):
    with ChromeSites([SITE, OTHER]) as sites:
        obs, shot, rects = _shot_and_boxes(sites, path, html, boxes)
        if shot["id"] is None:
            return  # not taken: allowed when the mask cannot be complete
        px = png_pixels(base64.b64decode(shot["png"]))
        for box, sel in zip(rects, boxes):
            assert isinstance(box, list) and all_black(px, box), (path, sel)


def test_a_field_added_during_capture_drops_the_screenshot():
    """A password field added while the marks are on was not covered: no screenshot."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = """<html><body><input id=f type=password value=x><script>
          new MutationObserver(() => { if (!document.getElementById('late')) {
              const i = document.createElement('input'); i.id = 'late'; i.type = 'password'; i.value = 'NL91ABNA0417164300';
              document.body.appendChild(i); } })
            .observe(document.body, {attributes: true, subtree: true, attributeFilter: ['data-casa-mask']});
        </script></body></html>"""
        obs, shot, _ = _shot_and_boxes(sites, "/late", html, [])
        assert shot["id"] is None


def test_a_field_replaced_during_capture_drops_the_screenshot():
    """A page that swaps a marked field for an unmarked clone keeps the mask's shape; the
    capture checks that every marked element is still there, marked."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = """<html><body><input id=f type=password value="PrivateFieldValue" style="width:300px;height:40px"><script>
          new MutationObserver((records) => { for (const m of records) {
              const e = m.target; if (e.hasAttribute('data-casa-mask') && !e.dataset.swapped) {
                const c = e.cloneNode(true); c.removeAttribute('data-casa-mask'); c.dataset.swapped = 1;
                e.replaceWith(c); } } })
            .observe(document.body, {attributes: true, subtree: true, attributeFilter: ['data-casa-mask']});
        </script></body></html>"""
        obs, shot, _ = _shot_and_boxes(sites, "/swap", html, [])
        assert shot["id"] is None


def test_a_field_hidden_and_cloned_during_capture_drops_the_screenshot():
    """The marked field stays connected and marked but is hidden, and an unmarked visible clone
    takes its place: every field on the page after the capture must carry the mark."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = """<html><body><input id=f autocomplete=one-time-code value="PrivateFieldValue" style="width:300px;height:40px"><script>
          new MutationObserver((records) => { for (const m of records) {
              const e = m.target; if (e.hasAttribute('data-casa-mask') && !e.dataset.swapped) {
                e.dataset.swapped = 1; const c = e.cloneNode(true); c.removeAttribute('data-casa-mask');
                c.dataset.swapped = 1; e.type = 'hidden'; e.after(c); } } })
            .observe(document.body, {attributes: true, subtree: true, attributeFilter: ['data-casa-mask']});
        </script></body></html>"""
        obs, shot, _ = _shot_and_boxes(sites, "/hideclone", html, [])
        assert shot["id"] is None


def test_a_block_past_its_limit_takes_no_more_text():
    """The rest of an address after a block's limit never reaches the model on its own."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = ("<html><body><p><span>https://site.example/" + "a" * 200001 +
                "</span><span>/private-invoice?account=7781</span></p></body></html>")
        obs, shot, _ = _shot_and_boxes(sites, "/overlong", html, [])
        assert "private-invoice" not in repr(obs) and "account=7781" not in repr(obs)


def test_a_closed_shadow_root_attached_during_capture_drops_the_screenshot():
    with ChromeSites([SITE, OTHER]) as sites:
        html = """<html><body><input id=f type=password value=x><div id=host></div><script>
          new MutationObserver(() => { const h = document.getElementById('host');
              if (!h.dataset.done) { h.dataset.done = 1;
                h.attachShadow({mode: 'closed'}).innerHTML = '<input value="PrivateFieldValue">'; } })
            .observe(document.body, {attributes: true, subtree: true, attributeFilter: ['data-casa-mask']});
        </script></body></html>"""
        obs, shot, _ = _shot_and_boxes(sites, "/lateclosed", html, [])
        assert shot["id"] is None


def test_model_text_keeps_split_values_whole_and_skips_hidden_and_editable():
    """A value rendered as one run but split across nodes (spans, <wbr>, inline-blocks) is
    masked whole; hidden ancestors, closed <details>, every spelling of editability and
    attribute-only names never reach the model."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = f"""<html><body>
          <p>Reset link: https://bank.example/<wbr>reset/<wbr>acct-7781/<wbr>robin?<wbr>code=991</p>
          <p>AWS: <span>{AKIA[:4]}</span><span>{AKIA[4:]}</span></p>
          <p><span style="display:inline-block">{AKIA[:8]}</span><span style="display:inline-block">{AKIA[8:]}</span></p>
          <div style="display:none"><p>HIDDEN-NESTED Hunter2pw</p></div>
          <details><summary>Show password</summary><p>DETAILS-CLOSED Hunter3pw</p></details>
          <div contenteditable="TRUE">CE-UPPER-NOTE</div>
          <div style="-webkit-user-modify: read-write">CE-CSS-NOTE</div>
          <div contenteditable=true><x-note id=xn></x-note></div>
          <div id=lab contenteditable=true>EDITOR-LABEL</div><button aria-labelledby=lab>Go</button>
          <button aria-label="ATTR-ONLY-NAME"></button>
          <span id=hid style="display:none">HIDDEN-LABEL</span><button aria-labelledby=hid></button>
          <p>Open_https://site.example/reset?code=GLUED-CODE</p>
          <a href="https:\\\\site.example\\BACKSLASH-PATH?account=7781">https:\\\\site.example\\BACKSLASH-PATH?account=7781</a>
          <p>https:site.example/NO-SLASH-PATH?account=7781</p>
          <p>file:///FILE-PATH/account-7781</p>
          <a href="https:/ONE-SLASH-PATH?account=7781">https:/ONE-SLASH-PATH?account=7781</a>
          <div style="display:contents">CONTENTS-SHOWN {KEY}</div>
          <div style="display:contents;visibility:hidden">CONTENTS-INVISIBLE</div>
          <div style="display:none"><span style="display:contents">CONTENTS-UNDER-NONE</span></div>
          <button>{"x " * 145}{KEY}</button>
          <script>document.getElementById('xn').attachShadow({{mode: 'open'}}).innerHTML = '<p>SHADOW-EDIT</p>';</script>
          </body></html>"""
        obs, shot, _ = _shot_and_boxes(sites, "/text", html, [])
        # slice 5j: an editable element shows its text as its value only (never page text or a
        # name), as an editor Fido typed into does
        values = [e.get("value_masked") for e in obs["elements"] if "value_masked" in e]
        assert "CE-UPPER-NOTE" in values and "EDITOR-LABEL" in values, values
        wire = repr({**obs, "elements": [{k: v for k, v in e.items() if k != "value_masked"}
                                         for e in obs["elements"]]})
        assert "SHADOW-EDIT" not in repr(values) and "CE-CSS-NOTE" not in repr(values)
        for leak in ("/reset", "acct-7781", "code=991", AKIA, AKIA[:8], AKIA[8:], "IOSFODNN7EXAMPLE",
                     "HIDDEN-NESTED", "DETAILS-CLOSED", "CE-UPPER-NOTE", "CE-CSS-NOTE", "SHADOW-EDIT",
                     "EDITOR-LABEL", "HIDDEN-LABEL", "GLUED-CODE", "/reset?code", "BACKSLASH-PATH", "NO-SLASH-PATH", "FILE-PATH", "ONE-SLASH-PATH", "account=7781",
                     KEY[:10], "CONTENTS-INVISIBLE", "CONTENTS-UNDER-NONE"):
            assert leak not in wire, leak
        assert "ATTR-ONLY-NAME" not in obs["text"]             # a name, never page text
        assert "ATTR-ONLY-NAME" in [e["label_masked"] for e in obs["elements"]]
        assert "https://bank.example" in obs["text"] and "Show password" in obs["text"]
        assert "CONTENTS-SHOWN [masked]" in obs["text"]


@pytest.mark.parametrize("path,html,boxes", [
    ("/cecontents", "<html><body><div contenteditable=true style='display:contents'><p id=k style='font-size:28px'>CorrectHorseBatteryStaple</p></div></body></html>", ["#k"]),
    ("/ceupper", "<html><body><div id=k contenteditable='TRUE' style='font-size:28px'>CorrectHorseBatteryStaple</div></body></html>", ["#k"]),
])
def test_screenshot_shows_editable_regions(path, html, boxes):
    """Slice 5h: an editable region that is not a secret field shows what it holds."""
    with ChromeSites([SITE, OTHER]) as sites:
        obs, shot, rects = _shot_and_boxes(sites, path, html, boxes)
        assert shot["id"], path
        px = png_pixels(base64.b64decode(shot["png"]))
        for box, sel in zip(rects, boxes):
            assert not all_black(px, box), (path, sel)


@pytest.mark.parametrize("path,html", [
    ("/cedirect", "<html><body><p><span contenteditable=true style='display:contents'>CE-DIRECT-NOTE NL91ABNA0417164300</span></p></body></html>"),
    ("/ceoverflow", "<html><body><div contenteditable=true style='height:4px;width:600px;line-height:30px'>CE-OVERFLOW-NOTE<br>second line</div></body></html>"),
])
def test_editable_content_the_mask_cannot_cover_suppresses_the_screenshot(path, html):
    """The mask covers an element's border box: editable content rendered outside it (no box
    of its own, or visible overflow) means no screenshot."""
    with ChromeSites([SITE, OTHER]) as sites:
        obs, shot, _ = _shot_and_boxes(sites, path, html, [])
        assert shot["id"] is None, path
        assert "CE-DIRECT-NOTE" not in obs["text"] and "CE-OVERFLOW-NOTE" not in obs["text"]
        assert "NL91ABNA0417164300" not in repr(obs)          # a value is masked page text


def test_a_scrolling_editor_is_still_captured_and_shown():
    """Control: an editor whose overflow scrolls inside its box is captured, and (not a secret
    field) shown."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = ("<html><body><div id=k contenteditable=true style='height:60px;width:600px;"
                "overflow:auto;font-size:28px'>" + "line<br>" * 20 + "</div></body></html>")
        obs, shot, rects = _shot_and_boxes(sites, "/scroll", html, ["#k"])
        assert shot["id"]
        assert not all_black(png_pixels(base64.b64decode(shot["png"])), rects[0])


def test_closed_shadow_root_suppresses_the_screenshot():
    with ChromeSites([SITE, OTHER]) as sites:
        html = f"""<html><body><div id=ch></div><script>
          document.getElementById('ch').attachShadow({{mode: 'closed'}}).innerHTML =
            '<span>key {AKIA}</span> <input value="NL91ABNA0417164300">';</script></body></html>"""
        obs, shot, _ = _shot_and_boxes(sites, "/closed", html, [])
        assert shot["id"] is None
        assert AKIA not in repr(obs)


PIZZA = """<html><head><style>body{margin:40px;font:20px Arial}input,textarea{width:300px;
  height:40px;font:20px Arial}input[type=radio],input[type=checkbox]{width:24px;height:24px}
  </style></head><body><h1>Pizza order</h1><form>
  <p>Name <input id=name value="Jan Jansen"></p>
  <p>Phone <input id=tel type=tel value="0612345678"></p>
  <p>Email <input id=email type=email value="jan@example.org"></p>
  <p><input id=large type=radio name=size checked> Large <input type=radio name=size> Small</p>
  <p><input id=extra type=checkbox checked> Extra cheese</p>
  <p>Time <input id=time type=time value="19:30"></p>
  <p>Comments <textarea id=notes>Ring twice</textarea></p>
  <p>Password <input id=pw type=password value="CanaryPassword-0005"></p>
  <p>Code <input id=otp autocomplete=one-time-code value="482913"></p>
  <p>Card <input id=cc autocomplete=cc-number value="4111111111111111"></p>
  <p><button id=b type=button>Submit order</button></p></form></body></html>"""
SHOWN = ["#name", "#tel", "#email", "#large", "#extra", "#time", "#notes"]
MASKED = ["#pw", "#otp", "#cc"]


@pytest.mark.parametrize("step", ["screenshot", "question_shot"])
def test_a_form_shows_its_fields_and_masks_only_secret_ones(step):
    """Slice 5h (phone round 7): the operator's own screenshot and a question's picture follow
    one rule — a name, phone, email, radio, checkbox, time and comments field shows what it
    holds; the password, one-time-code and payment-card fields are black."""
    with ChromeSites([SITE, OTHER]) as sites:
        sites.pages["/pizza"] = ("text/html", PIZZA.encode(), {})
        url = f"https://{SITE}/pizza"
        policy = _policy(url)
        from casa_browser.proxy_client import decode_policy
        sites.install(decode_policy(policy))
        arg = policy if step == "screenshot" else {"policy": policy, "target": "#b"}
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": url}, {"wait": 1},
                            {"observe": policy}, {step: arg}]
                           + [{"box": b} for b in SHOWN + MASKED])
        shot = res[4]
        assert shot["id"], shot
        px = png_pixels(base64.b64decode(shot["png"]))
        rects = dict(zip(SHOWN + MASKED, res[5:]))
        for sel in SHOWN:
            assert not all_black(px, rects[sel]), f"{sel} is blacked out"
        for sel in MASKED:
            assert all_black(px, rects[sel]), f"{sel} is not masked"


VALUES = """<html><head><style>body{margin:40px;font:20px Arial}input,textarea{width:300px;
  height:40px;font:20px Arial}input[type=radio],input[type=checkbox]{width:24px;height:24px}
  </style></head><body><h1>Booking</h1><form>
  <p><label>Name <input value="Jan Jansen"></label></p>
  <p><label>Date <input type=date value="2026-11-15"></label></p>
  <p><label>Guests <input type=number value="4"></label></p>
  <p><label>Size <select><option>Small</option><option selected>Large</option></select></label></p>
  <p><label><input type=radio name=seat checked> Window</label>
     <label><input type=radio name=seat> Aisle</label></p>
  <p><label><input type=checkbox checked> Extra cheese</label></p>
  <p><label>Comments <textarea>Ring twice
Third floor</textarea></label></p>
  <p><label>Link <input value="%s"></label></p>
  <p><label>Password <input type=password value="Withheld-0101"></label></p>
  <p><label>Code <input autocomplete=one-time-code value="Withheld-0102"></label></p>
  <p><label>Card <input autocomplete=cc-number value="Withheld-0103"></label></p>
  <p><label>Pin <input autocomplete="section-x current-password" value="Withheld-0104"></label></p>
  <p><button type=button>Book</button></p></form></body></html>""" % BEARER
WITHHELD = ("Withheld-0101", "Withheld-0102", "Withheld-0103", "Withheld-0104")


def _values(obs):
    return {e["label_masked"]: {k: e[k] for k in ("kind", "value_masked", "checked") if k in e}
            for e in obs["elements"]}


def test_an_observation_shows_what_ordinary_fields_hold_and_never_a_secret_one():
    """Slice 5j (operator ruling 2026-10-31, phone round 8): the model sees what an ordinary field
    holds — text, a date, a number, a select's chosen option, a radio's and a checkbox's state, a
    multi-line text — masked like page text; a password, one-time-code or payment-card field
    shows nothing, and its value reaches no part of the observation."""
    with ChromeSites([SITE, OTHER]) as sites:
        sites.pages["/book"] = ("text/html", VALUES.encode(), {})
        url = f"https://{SITE}/book"
        policy = _policy(url)
        from casa_browser.proxy_client import decode_policy
        sites.install(decode_policy(policy))
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": url}, {"wait": 1},
                            {"observe": policy}])
        obs = res[3]
        got = _values(obs)
        assert got["Name"] == {"value_masked": "Jan Jansen"}, got
        assert got["Date"] == {"kind": "date", "value_masked": "2026-11-15"}, got
        assert got["Guests"] == {"kind": "number", "value_masked": "4"}, got
        assert got["Size"] == {"value_masked": "Large"}, got
        assert got["Window"] == {"checked": True} and got["Aisle"] == {"checked": False}, got
        assert got["Extra cheese"] == {"checked": True}, got
        assert got["Comments"] == {"kind": "multi-line",
                                   "value_masked": "Ring twice\nThird floor"}, got
        assert TOKEN not in got["Link"]["value_masked"] and "/invoices/in_" not in repr(obs)
        for label in ("Password", "Code", "Card", "Pin"):
            assert got[label] == {}, (label, got[label])
        for leak in WITHHELD:
            assert leak not in repr(obs), leak


def test_a_field_is_secret_as_it_is_when_read_and_a_page_that_reveals_it_shows_it():
    """Slice 5j (operator ruling 2026-10-31, option (b)): a field is secret as it is when it is
    read. A password field shows no value in the observation, "[not shown]" in the check's
    reading and black in the operator's screenshot; a field inside an element the rule recognises
    (a wrapper whose autocomplete names a code) is secret too. Residual, ruled: once the page
    itself turns the password field into a plain text field (a "show password" toggle that also
    drops its autocomplete), it is an ordinary field and its value shows (ruling 2026-10-20: a
    page can show its own value as text)."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = ("<html><body style='margin:40px;font:20px Arial'><p><label>Name "
                "<input value='Jan'></label></p><p><label>Password <input id=pw type=password "
                "autocomplete=current-password value='Shown-Later-0201' style='width:300px;"
                "height:40px'></label> <button id=eye type=button onclick=\"pw.type='text';"
                "pw.removeAttribute('autocomplete')\">Show</button></p>"
                "<label for=c>Code</label><div autocomplete=one-time-code><input id=c "
                "value='Withheld-0202'></div></body></html>")
        sites.pages["/reveal"] = ("text/html", html.encode(), {})
        url = f"https://{SITE}/reveal"
        policy = _policy(url)
        from casa_browser.proxy_client import decode_policy
        sites.install(decode_policy(policy))
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": url}, {"wait": 1},
                            {"observe": policy}, {"gate_read": None}, {"screenshot": policy},
                            {"box": "#pw"}, {"click": "#eye"}, {"observe": policy}])
        before, reading, shot, after = res[3], res[4], res[5], res[8]
        got = _values(before)
        assert got["Name"] == {"value_masked": "Jan"}, got
        assert got["Password"] == {} and got["Code"] == {}, got
        assert "Shown-Later-0201" not in repr(before) + repr(reading)
        assert "Withheld-0202" not in repr(before) + repr(reading) + repr(after)
        field = next(c for c in reading["controls"] if c["name"] == "Password")
        assert field["state"]["value"] == "[not shown]" and field["secret"] is True, field
        assert shot["id"] and all_black(png_pixels(base64.b64decode(shot["png"])), res[6])
        # the residual: the page revealed its own field, which is now ordinary
        assert _values(after)["Password"] == {"value_masked": "Shown-Later-0201"}, _values(after)


def test_a_secret_controls_state_is_not_shown_and_an_option_shows_its_label():
    """r5 Astra and Terra (slice 5j): a checkbox inside an element the rule recognises (a wrapper
    whose autocomplete names a card) is a secret control at read time: the check's reading shows
    its state as "[not shown]" (native) or not at all (aria), and the observation shows none; an
    ordinary checkbox beside it shows its state. A select shows its chosen option's label as the
    page shows it (the label attribute, else the text), and so do its listed options."""
    with ChromeSites([SITE, OTHER]) as sites:
        # the names sit outside the wrapper: text inside a secret element is never read
        html = ("<html><body><form><label for=sv>Save card</label><span id=uc>Use card</span>"
                "<div autocomplete=cc-number><input id=sv type=checkbox checked><div "
                "role=checkbox aria-checked=true aria-labelledby=uc tabindex=0 style='display:inline-block;"
                "width:20px;height:20px'></div></div>"
                "<label><input type=checkbox checked> Newsletter</label><label>"
                "City <select><option>Rome</option><option label=Amsterdam selected>AMS</option>"
                "</select></label><button>Continue</button></form></body></html>")
        sites.pages["/state"] = ("text/html", html.encode(), {})
        url = f"https://{SITE}/state"
        policy = _policy(url)
        from casa_browser.proxy_client import decode_policy
        sites.install(decode_policy(policy))
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": url}, {"wait": 1},
                            {"gate_read": None}, {"observe": policy}])
        states = {c["name"]: c["state"] for c in res[3]["controls"]}
        assert states["Save card"] == {"checked": "[not shown]"}, states
        assert states["Use card"] == {}, states
        assert states["Newsletter"] == {"checked": True}, states
        got = _values(res[4])
        assert got["Save card"] == {} and got["Use card"] == {}, got
        assert got["Newsletter"] == {"checked": True}, got
        assert got["City"] == {"value_masked": "Amsterdam"}, got
        assert [e["label_masked"] for e in res[4]["elements"] if e["role"] == "option"] == \
            ["Rome", "Amsterdam"]


def test_a_small_page_is_cropped_to_its_content():
    """Slice 5f: a small page sits top-left in the 1920×1080 window; the operator's screenshot
    is cropped to its content with a margin (legible on a phone), and the crop keeps the mask:
    the password is still black and the heading is still there."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = ("<html><body><h1 id=h>Pizza order</h1><p>Name <input id=f value='Jan'></p>"
                "<p>PIN <input id=p type=password value='4711'></p>"
                "<p><button>Submit order</button></p></body></html>")
        obs, shot, rects = _shot_and_boxes(sites, "/small", html, ["#p", "#h"])
        assert shot["id"]
        px = png_pixels(base64.b64decode(shot["png"]))
        assert px[0] < 900 and px[1] < 500, px[:2]            # not the 1920×1080 window
        assert all_black(px, rects[0]), "password not masked in the crop"
        assert not all_black(px, rects[1]), "the heading is in the crop"


def test_content_across_the_window_is_not_cropped_away():
    """Control: content at the window's far corner keeps the whole width in the picture."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = ("<html><body><p>Order</p><p id=far style='position:absolute;right:4px;"
                "top:600px'>Total EUR 12</p></body></html>")
        obs, shot, rects = _shot_and_boxes(sites, "/wide", html, ["#far"])
        assert shot["id"]
        px = png_pixels(base64.b64decode(shot["png"]))
        assert px[0] > 1800 and px[1] > 600, px[:2]
        assert not all_black(px, rects[0])


def test_text_inside_an_open_shadow_root_stays_in_the_crop():
    """Astra r1 (slice 5f): renewal terms rendered in an open shadow root whose host has no box
    (display: contents) in a second column are part of the picture."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = ("<html><head><style>body{margin:40px;font:20px Arial}main{display:grid;"
                "grid-template-columns:360px 520px;gap:200px}x-terms{display:contents}</style>"
                "</head><body><main><section><p>Premium plan</p><button>Start</button></section>"
                "<x-terms></x-terms></main><script>document.querySelector('x-terms')"
                ".attachShadow({mode:'open'}).innerHTML='<section style=\"grid-column:2;"
                "grid-row:1\"><p id=t>Renews at EUR 99 per month</p></section>';</script>"
                "</body></html>")
        obs, shot, rects = _shot_and_boxes(sites, "/shadowterms", html, ["x-terms|#t"])
        assert shot["id"]
        px = png_pixels(base64.b64decode(shot["png"]))
        x, y, w, h = rects[0]
        # the text (not its wider block box) is in the picture
        assert x + 100 <= px[0] and y + h <= px[1], (rects[0], px[:2])
        assert not all_black(px, rects[0])


def test_an_animated_panel_is_in_the_crop_where_the_capture_put_it():
    """Astra r2/r3 (slice 5f): the capture finishes animations, and the crop is measured after
    it, so a panel still sliding in (in the document or an open shadow root) is in the picture
    where the picture shows it."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = ("<html><head><style>body{margin:40px;font:20px Arial}aside{position:absolute;"
                "left:800px;top:80px;width:450px;animation:enter .4s ease-out 30s both}"
                "@keyframes enter{from{transform:translateX(1200px)}to{transform:none}}"
                "</style></head><body><h1>Premium plan</h1><button>Start</button>"
                "<aside id=terms>Renews at EUR 99 per month.</aside><x-s></x-s><script>"
                "document.querySelector('x-s').attachShadow({mode:'open'}).innerHTML="
                "'<style>p{position:absolute;left:900px;top:300px;animation:e .4s 30s both}"
                "@keyframes e{from{transform:translateX(1500px)}to{transform:none}}</style>"
                "<p id=s>Minimum 12 months</p>';</script></body></html>")
        obs, shot, rects = _shot_and_boxes(sites, "/anim", html, ["#terms", "x-s|#s"])
        assert shot["id"]
        px = png_pixels(base64.b64decode(shot["png"]))
        for x, y, w, h in rects:      # the text (not its wider block box) is in the picture
            assert 0 <= x and 0 <= y and x + 150 <= px[0] and y + h <= px[1], (rects, px[:2])
        assert not all_black(px, rects[0]) and not all_black(px, rects[1])


def test_an_open_shadow_root_on_body_is_in_the_picture():
    """Terra r3 (slice 5f): content in a shadow root attached to <body> itself."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = ("<html><body><script>document.body.attachShadow({mode:'open'}).innerHTML="
                "'<p id=r style=\"position:absolute;left:1200px;top:724px\">Order accepted: "
                "1234</p>';</script></body></html>")
        obs, shot, rects = _shot_and_boxes(sites, "/bodyshadow", html, ["body|#r"])
        assert shot["id"]
        px = png_pixels(base64.b64decode(shot["png"]))
        x, y, w, h = rects[0]
        assert x + w <= px[0] and y + h <= px[1], (rects, px[:2])


def test_a_looping_animation_is_measured_as_the_capture_showed_it():
    """Astra r4 (slice 5f): the capture cancels an infinite animation and resumes it after; the
    crop is measured in that same state, so looping text is where the picture shows it."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = ("<html><head><style>body{margin:40px;font:20px Arial}p{position:absolute;"
                "left:900px;top:320px;animation:m 3s linear infinite}@keyframes m{from"
                "{transform:translateX(-800px)}to{transform:translateX(-799px)}}</style></head>"
                "<body><h1>Plan</h1><button>Subscribe</button><p id=t>Renews at EUR 99 per "
                "month</p></body></html>")
        sites.pages["/loop"] = ("text/html", html.encode(), {})
        url = f"https://{SITE}/loop"
        policy = _policy(url)
        from casa_browser.proxy_client import decode_policy
        sites.install(decode_policy(policy))
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": url}, {"wait": 1},
                            {"observe": policy}, {"screenshot": policy}])
        shot = res[4]
        assert shot["id"]
        px = png_pixels(base64.b64decode(shot["png"]))
        assert px[0] > 900 + 150 - shot["clip"]["x"], (px[:2], shot["clip"])


def test_the_question_picture_keeps_a_layout_that_depends_on_last_child():
    """Astra r5 (slice 5f): the target's outline is its own inline style, not a stylesheet
    added for the capture, so a shadow component laid out by :last-child looks the same when
    captured and when measured: its (blue) terms are in the picture, and so is the outline."""
    with ChromeSites([SITE, OTHER]) as sites:
        html = ("<html><head><style>body{margin:40px;font:20px Arial}main{display:grid;"
                "grid-template-columns:360px 520px;gap:200px}x-plan{display:contents}</style>"
                "</head><body><h1>Premium plan</h1><button id=b>Subscribe</button><main>"
                "<x-plan></x-plan></main><script>document.querySelector('x-plan').attachShadow("
                "{mode:'open'}).innerHTML='<style>section:last-child{grid-column:1/-1}</style>"
                "<section>Premium subscription</section><section style=\"color:#0000ff\">"
                "Renews at EUR 99 per month</section>';</script></body></html>")
        sites.pages["/lastchild"] = ("text/html", html.encode(), {})
        url = f"https://{SITE}/lastchild"
        policy = _policy(url)
        from casa_browser.proxy_client import decode_policy
        sites.install(decode_policy(policy))
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": url}, {"wait": 1},
                            {"observe": policy},
                            {"question_shot": {"policy": policy, "target": "#b"}}])
        shot = res[4]
        assert shot["id"], shot
        width, height, bpp, rows = png_pixels(base64.b64decode(shot["png"]))
        colours = {bytes(rows[yy][xx * bpp:xx * bpp + 3])
                   for yy in range(height) for xx in range(width)}
        assert bytes([0, 0, 0xff]) in colours, "the terms are in the picture"
        assert bytes([0xd3, 0x2f, 0x2f]) in colours, "the outline is drawn"


# Slice 5m (phone round 9): a frame with no web address of its own — a document the page wrote
# (W3Schools' ``iframeResult``: Playwright's address about:blank, its location the parent's), a
# srcdoc — is shown to Fido and to the check under the one frame rule; a sandboxed one (opaque
# origin), an off-site frame and the written child of an off-site frame stay withheld.
FRAMES = f"""<html><head><title>Try it</title></head><body><h1>Try it</h1>
<iframe id=res name=iframeResult style="width:600px;height:200px"></iframe>
<iframe id=sd srcdoc="<p>Srcdoc text</p><label>Note <input value='hello'></label>"></iframe>
<iframe id=sb sandbox srcdoc="<p>Sandboxed text</p><label>Boxed <input value='Withheld-0201'></label>"></iframe>
<iframe id=off src="https://{OTHER}/off"></iframe>
<iframe id=sbw sandbox src="https://{SITE}/boxed"></iframe>
<template id=inner><p>Inner demo</p><form><label>Pickup <input type=time id=t value="07:45"></label>
<label>Pass <input type=password value="Withheld-0202"></label>
<label>Card <input autocomplete=cc-number value="Withheld-0203"></label></form></template>
<script>
const d = document.getElementById('res').contentWindow.document;
d.open(); d.write(document.getElementById('inner').innerHTML); d.close();
</script></body></html>"""
OFF = """<html><body><p>Off-site text</p><label>Off <input value='Withheld-0204'></label>
<iframe id=n></iframe><iframe src="https://site-a.example/inside"></iframe><script>const d = document.getElementById('n').contentWindow.document;
d.open(); d.write("<p>Off child</p><input aria-label=OffChild value='Withheld-0205'>"); d.close();
</script></body></html>"""


def test_a_written_frame_is_shown_and_a_withheld_one_stays_withheld():
    with ChromeSites([SITE, OTHER]) as sites:
        sites.pages["/try"] = ("text/html", FRAMES.encode(), {})
        sites.pages["/off"] = ("text/html", OFF.encode(), {})
        sites.pages["/boxed"] = ("text/html", b"<p>Boxed page</p><input aria-label=BoxedWeb "
                                 b"value='Withheld-0206'>", {})
        sites.pages["/inside"] = ("text/html", b"<p>Inside page</p><input aria-label=InsideWeb "
                                  b"value='Withheld-0207'>", {})
        url = f"https://{SITE}/try"
        policy = _policy(url)
        from casa_browser.proxy_client import decode_policy
        sites.install(decode_policy(policy))
        res = run_scenario([{"open": {"site_id": "sa"}}, {"goto": url}, {"wait": 1.5},
                            {"frame_urls": None}, {"observe": policy}, {"gate_read_page": policy}])
        urls, obs, reading = res[3], res[4], res[5]
        assert "about:blank" in urls and "about:srcdoc" in urls, urls   # what Playwright reports
        got = _values(obs)
        assert got["Pickup"] == {"kind": "time", "value_masked": "07:45"}, got
        assert got["Pass"] == {} and got["Card"] == {}, got
        assert got["Note"] == {"value_masked": "hello"}, got
        assert "Inner demo" in obs["text"] and "Srcdoc text" in obs["text"], obs["text"]
        for gone in ("Boxed", "Off", "OffChild", "BoxedWeb", "InsideWeb"):
            assert gone not in got, got
        for gone in ("Sandboxed text", "Off-site text", "Off child", "Boxed page", "Inside page"):
            assert gone not in obs["text"], obs["text"]
        # the sandboxed frames (srcdoc and same-site), the off-site one and both its children
        assert obs["text"].count("[a frame on this page is not shown]") == 5, obs["text"]
        for leak in ("Withheld-0201", "Withheld-0202", "Withheld-0203", "Withheld-0204",
                     "Withheld-0205", "Withheld-0206", "Withheld-0207"):
            assert leak not in repr(obs) and leak not in repr(reading), leak
        # the check reads the frames under the same rule
        names = {c["name"]: c for c in reading["controls"]}
        assert names["Pickup"]["state"] == {"value": "07:45"}, names["Pickup"]
        assert names["Pass"]["state"] == {"value": "[not shown]"}, names["Pass"]
        assert "Note" in names, names
        assert not {"Boxed", "Off", "OffChild", "BoxedWeb", "InsideWeb"} & set(names), names
        assert len(reading["frames_withheld"]) == 5, reading.get("frames_withheld")
        texts = [b["text"] for b in reading["blocks"]]
        assert "Inner demo" in texts and texts.count("[a frame on this page is not shown]") == 5
