"""A long, live, signed-in page in real Chrome with the production controller and broker: more
controls than an observation lists, a price that keeps changing, a menu the page keeps at the end
of its document, a native select, a password manager's button in a closed shadow root beside a
field and a CAPTCHA's frame in another. Everything a person could reach there Fido can reach."""
from __future__ import annotations

import base64

import pytest

from lifecycleprobe import controller_home, run_lifecycle
from sitefixture import ChromeSites
from test_observation import all_black, png_pixels
from test_run_lifecycle import _controller_uid, fixture_config

pytestmark = pytest.mark.docker

A, B = "site-a.example", "pub-b.example"
ROWS = "".join(f'<tr><td><input value=1 size=2></td><td><a href="/card/{n}">Card {n}</a></td>'
               f'<td><button>Edit {n}</button><button>Remove {n}</button></td></tr>'
               for n in range(300))
# A password manager's button as measured in Chrome with 1Password: a custom element with a closed
# root holding a button, a stylesheet link and a box, no field.
PASSWORD_MANAGER = """const pm = document.createElement('com-pm-button'); document.body.append(pm);
  pm.attachShadow({mode: 'closed'}).innerHTML = '<button contenteditable=false>Fill</button><link rel=stylesheet href="data:text/css,"><div>menu</div>';"""
CAPTCHA_FRAME = """const w = document.getElementById('w');
  const f = document.createElement('iframe');
  f.src = 'https://%s/widget'; f.style.cssText = 'width:300px;height:100px;border:0';
  w.attachShadow({mode: 'closed'}).append(f);""" % B
DECK = f"""<!doctype html><title>A deck</title><body style="margin:0">
<div style="height:40px"><button id=more>More</button>
<label>View <select id=view><option>Text</option><option>Table</option></select></label>
<input id=search placeholder=Search> Price <span id=price>0</span></div>
<p style="position:absolute;left:1500px;top:0">Wide</p>
<div id=w style="position:absolute;left:600px;top:100px;width:300px;height:100px"></div>
<table id=list>{ROWS}</table><div id=out></div>
<script>
let n = 0; setInterval(() => {{ document.getElementById('price').textContent = '€ 0.' + (++n); }}, 150);
{CAPTCHA_FRAME}
{PASSWORD_MANAGER}
document.getElementById('view').onchange = (e) => {{
  document.getElementById('out').textContent = 'The view is now ' + e.target.value; }};
document.getElementById('more').onclick = () => {{
  const old = document.getElementById('menu');
  if (old) {{ old.remove(); return; }}
  const m = document.createElement('div'); m.id = 'menu';
  m.style.cssText = 'position:absolute;left:10px;top:40px;background:#fff;z-index:5';
  for (const name of ['Import', 'Export']) {{
    const a = document.createElement('a');   // no href, no role: only its pointer says it acts
    a.textContent = name; a.style.cssText = 'display:block;cursor:pointer';
    a.onclick = () => {{ document.getElementById('out').textContent = 'exported: 1 Card 0 (SET) 123'; m.remove(); }};
    m.append(a);
  }}
  document.body.append(m);
}};
</script>""".encode()


def run_deck(tmp_path, *steps):
    config = fixture_config(tmp_path)
    with ChromeSites([A, B]) as sites:
        _controller_uid(sites)
        sites.pages["/deck"] = ("text/html", DECK, {})
        sites.pages["/widget"] = ("text/html", b"<body style='background:#0f0'>widget</body>", {})
        return run_lifecycle([
            {"site": {"site_id": "s1", "name": "Example", "origins": [f"https://{A}"],
                      "login": [f"https://{A}"], "item": "item", "start": f"https://{A}/deck"}},
            {"broker": {"op": "register_item", "site_id": "s1", "item_id": "item",
                        "login_origins": [f"https://{A}"], "operator_ref": "op"}},
            {"run": {"name": "r", "request_id": "it-deck", "sites": [["s1", "account"]]}},
            {"wait_state": {"name": "r", "state": "running", "timeout": 150}},
            {"sleep": 2},
            *steps,
            {"events": {"name": "r"}},
            {"finish": {"name": "r"}},
        ], home=controller_home(tmp_path), broker_fixture=config)[5:]


def test_a_long_live_page_can_be_worked_on(tmp_path):
    """Its menu and its select's options have handles though they come last in a document whose
    controls fill the list; the model is told more exist; the steps run under the changing price;
    the page has its picture, the CAPTCHA's frame black; nothing says a sign-in went unchecked."""
    r = "r"
    out = run_deck(
        tmp_path,
        {"look": {"name": r}},
        {"act": {"name": r, "kind": "click", "label": "More"}},
        {"look": {"name": r}},
        {"run_look": {"name": r}},
        {"act": {"name": r, "kind": "click", "label": "Export"}},
        {"js": {"name": r, "code": "document.getElementById('out').textContent"}},
        {"look": {"name": r}},
        {"act": {"name": r, "kind": "select", "label": "View", "choice": "Table"}},
        {"js": {"name": r, "code": "document.getElementById('view').value"}},
    )
    first, clicked, opened, picture, exported, shown, again, selected, chosen, events, end = out
    assert first["observation"] is True, first
    assert "Table" in first["labels"] and "Text" in first["labels"], first["labels"][:12]
    assert "more controls on this page are not listed" in first["text"]
    assert clicked["outcome"] == "done", clicked
    assert {"Import", "Export"} <= set(opened["labels"]), opened["labels"][-8:]
    assert len(opened["labels"]) <= 401               # the cap, and the app's own "Open site"
    assert exported["outcome"] == "done", exported
    assert shown == "exported: 1 Card 0 (SET) 123"
    assert selected["outcome"] == "done", selected
    assert chosen == "Table"

    assert isinstance(picture, str), picture
    px = png_pixels(base64.b64decode(picture))
    assert all_black(px, (600, 100, 300, 100)), "frame not masked"
    assert not all_black(px, (0, 0, 300, 30)), "control: page shown"

    assert ["sign_in_nothing", "not_checked"] not in events, events
    assert end == "completed"


NOISE = b"""<!doctype html><title>Pictures</title><body style="margin:0"><canvas id=c></canvas>
<p style="position:absolute;top:0">Pictures</p><script>
const c = document.getElementById('c'); c.width = innerWidth; c.height = innerHeight;
const g = c.getContext('2d'), im = g.createImageData(c.width, c.height);
for (let i = 0; i < im.data.length; i++) im.data[i] = i % 4 === 3 ? 255 : Math.random() * 256;
g.putImageData(im, 0, 0);</script>"""


def test_a_window_full_of_pictures_has_its_picture(tmp_path):
    """Art and photos at full size are far over the picture's limit: it comes smaller, not never."""
    config = fixture_config(tmp_path)
    with ChromeSites([A, B]) as sites:
        _controller_uid(sites)
        sites.pages["/pics"] = ("text/html", NOISE, {})
        out = run_lifecycle([
            {"site": {"site_id": "s1", "name": "Example", "origins": [f"https://{A}"],
                      "start": f"https://{A}/pics"}},
            {"run": {"name": "r", "request_id": "it-pics", "sites": [["s1", "account"]]}},
            {"wait_state": {"name": "r", "state": "running", "timeout": 150}},
            {"sleep": 1},
            {"run_look": {"name": "r"}},
            {"finish": {"name": "r"}},
        ], home=controller_home(tmp_path), broker_fixture=config)
    picture = out[4]
    assert isinstance(picture, str), picture
    png = base64.b64decode(picture)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and 100_000 < len(png) <= 2 * 1024 * 1024


def test_a_select_with_more_options_than_the_list_keeps_its_handle(tmp_path):
    """It stays with the options that fit, beside the controls that fit, and the rest are counted."""
    options = "".join(f"<option>Place {n}</option>" for n in range(450))
    html = (f"<!doctype html><title>Places</title><button>First</button><label>Where <select id=s>{options}"
            "</select></label><label>Size <select><option>Small</option><option>Large</option></select>"
            "</label><button>Last</button>").encode()
    config = fixture_config(tmp_path)
    with ChromeSites([A, B]) as sites:
        _controller_uid(sites)
        sites.pages["/places"] = ("text/html", html, {})
        out = run_lifecycle([
            {"site": {"site_id": "s1", "name": "Example", "origins": [f"https://{A}"],
                      "start": f"https://{A}/places"}},
            {"run": {"name": "r", "request_id": "it-places", "sites": [["s1", "account"]]}},
            {"wait_state": {"name": "r", "state": "running", "timeout": 150}},
            {"sleep": 1},
            {"observe": {"name": "r"}},
            {"act": {"name": "r", "kind": "select", "label": "Where", "choice": "Place 7"}},
            {"js": {"name": "r", "code": "document.getElementById('s').value"}},
            {"finish": {"name": "r"}},
        ], home=controller_home(tmp_path), broker_fixture=config)
    seen = out[4]
    places = [label for label in seen["labels"] if label.startswith("Place ")]
    assert {"First", "Last"} <= set(seen["labels"]) and "Where" in seen["labels"], seen["labels"][:4]
    assert {"Small", "Large"} <= set(seen["labels"]), "the small select stays whole"
    assert len(places) == 394, len(places)          # the cap, less 4 controls and the 2 small options
    assert "56 more controls on this page are not listed" in seen["text"], seen["text"][:120]
    assert out[5]["outcome"] == "done" and out[6] == "Place 7", out[5:7]


LONG_PAGE = ("<!doctype html><title>Rota</title><body style='margin:0'>"
             + "".join(f"<div style='height:40px'>Day {n} <select aria-label='Who {n}'><option>Ann<option>Bo"
                       f"<option>Cy<option>Di<option>Ed<option>Flo</select><label><input type=checkbox> Done {n}"
                       f"</label> <a href='/d/{n}'>Details {n}</a></div>" for n in range(240))
             + "<button id=go onclick=\"fetch('/published',{method:'POST'});"
               "document.getElementById('out').textContent='pressed'\">Publish</button>"
               "<input type=password id=pw value='itsecret" + "0123456789abcdef0123456789abcdef" + "'>"
               "<div id=out></div>").encode()


def test_any_control_of_a_long_page_is_a_search_away_and_goes_through_the_check(tmp_path):
    """Through the production controller and the real pacer, from the top of a page of over 700
    semantic controls: what the list leaves out is found, its handle goes through the check and
    presses; a word that holds nothing says so; repeated scrolls advance and say where they are."""
    r = "r"
    config = fixture_config(tmp_path)
    with ChromeSites([A, B]) as sites:
        _controller_uid(sites)
        sites.pages["/rota"] = ("text/html", LONG_PAGE, {})
        sites.pages["/published"] = ("text/html", b"ok", {})
        out = run_lifecycle([
            {"site": {"site_id": "s1", "name": "Example", "origins": [f"https://{A}"],
                      "start": f"https://{A}/rota"}},
            {"run": {"name": r, "request_id": "it-rota", "sites": [["s1", "account"]]}},
            {"wait_state": {"name": r, "state": "running", "timeout": 150}},
            {"sleep": 1},
            {"observe": {"name": r}},
            {"observe": {"name": r, "find": "nothing holds this"}},
            {"observe": {"name": r, "find": "itsecret"}},
            {"act": {"name": r, "kind": "scroll", "label": "Who 0", "direction": "down", "amount": "page"}},
            {"act": {"name": r, "kind": "scroll", "label": "Who 0", "direction": "down", "amount": "page"}},
            {"act": {"name": r, "kind": "scroll", "label": "Who 0", "direction": "down", "amount": "page"}},
            {"act": {"name": r, "kind": "scroll", "label": "__page__", "direction": "down", "amount": "step"}},
            {"observe": {"name": r, "find": "PUBLISH", "sign_in": True}},
            {"js": {"name": r, "code": "scrollY"}},
            {"act": {"name": r, "kind": "click", "label": "Publish"}},
            {"js": {"name": r, "code": "document.getElementById('out').textContent"}},
            {"finish": {"name": r}},
        ], home=controller_home(tmp_path), broker_fixture=config)
        requests = [(m, path) for m, _host, path, _b in sites.receipts.requests]
    plain, none, secret, s1, s2, s3, step, found, _scrolled, pressed, shown, end = out[4:]
    assert "Publish" not in plain["labels"] and len(plain["labels"]) <= 401
    assert "more controls on this page are not listed" in plain["text"]
    assert none["found"] == [] and none["find"] == {"whole": True, "left": 0}
    assert secret["found"] == [] and secret["find"]["whole"] is True
    tops = [s["position"]["top"] for s in (s1, s2, s3)]
    assert all(s["position"]["moved"] for s in (s1, s2, s3)) and tops == sorted(set(tops)), tops
    assert tops[-1] > 2000 and s3["position"]["end"] > 5000
    assert step["position"]["moved"] is True and step["position"]["top"] > tops[-1]
    assert found["found"] == ["Publish"] and found["find"] == {"whole": True, "left": 0}
    assert pressed["outcome"] == "done", pressed
    assert shown == "pressed" and ("POST", "/published") in requests
    assert end == "completed"
