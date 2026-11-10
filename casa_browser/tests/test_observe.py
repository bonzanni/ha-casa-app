"""Task 7: the page-view gate and masked observations (pure: fake page and extractor; the
real extraction code runs in the image test ``integration/test_observation.py``)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

import proxyharness as h
from casa_browser.handles import HandleRegistry
from casa_browser.observe import (Extraction, ObservationRefused, Observer, holds_words,
                                 page_view_allowed, rows_text, search_ask)
from casa_browser.policy import Lease

BEARER = "https://files.site.example/invoices/in_1PqR9sT2uV3wX4yZ/download?token=tok_Q7w8E9r0T1y2U3i4"  # gitleaks:allow - synthetic fixture; this test exists to prove masking works


@dataclass
class FakeFrame:
    url: str
    detached: bool = False
    parent_frame: object = field(default=None, repr=False, compare=False)

    def is_detached(self):
        return self.detached


@dataclass
class FakePage:
    url: str
    frames: list = field(default_factory=list)
    shots: int = 0

    def __post_init__(self):
        self.main_frame = FakeFrame(self.url)
        for f in self.frames:
            if f.parent_frame is None:
                f.parent_frame = self.main_frame
        self.frames = [self.main_frame] + self.frames

    async def screenshot(self, **kw):
        self.shots += 1
        self.mask = kw.get("mask")
        return b"\x89PNG\r\n\x1a\nfake"

    def locator(self, selector):
        return ("locator", selector)


class FakeEl:
    def __init__(self, log, connected=True, secret=False):
        self.log = log
        self.mark = None
        self.connected = connected
        self.secret = secret       # what the gate's one secret rule (``hidesSecret``) answers
        self.ever_marked = False

    async def evaluate(self, js, *args):
        if "hidesSecret" in js:
            return self.secret
        if "isConnected" in js:
            return self.connected and self.mark == args[0]
        self.log.append(("mark" if "setAttribute" in js else "unmark", args))
        self.ever_marked = self.ever_marked or "setAttribute" in js
        self.mark = args[0] if "setAttribute" in js else None


def ext(href, text="", elements=(), frames=0, log=None):
    log = log if log is not None else []
    fields = [e for e in elements if e.get("field")]
    return Extraction(href=href, title="T", text=text, elements=list(elements),
                      element_handles=[FakeEl(log, secret=bool(e.get("now_secret")))
                                       for e in elements],
                      field_handles=[FakeEl(log, secret=bool(e.get("secret"))) for e in fields],
                      frame_handles=[FakeEl(log) for _ in range(frames)], complete=True)


def session(page, gen=5, **kw):
    s = SimpleNamespace(page=page, view_generation=gen, site_id="s1", login_active=False)
    s.__dict__.update(kw)
    return s


RUN = SimpleNamespace(run_id="r1", state="running")


async def _no_closed_roots(_page):
    return False


def observer(extractions, tmp_path=None, on_extract=None):
    async def extractor(frame):
        if on_extract:
            on_extract(frame)
        return extractions[frame.url]
    return Observer(HandleRegistry(), tmp_path or __import__("pathlib").Path("/nonexistent"),
                    extractor=extractor, closed_shadow=_no_closed_roots)


def run(c):
    return asyncio.run(c)


URL = "https://site.example/account"


def test_observation_no_raw_targets():
    """A bearer address in an href, an accessible name, visible text, alt, title and an error
    string: none of its path or token reaches the observation."""
    policy = h.account_policy()
    page = FakePage(URL)
    elements = [
        {"role": "link", "name": "Download invoice", "href": BEARER, "field": False},
        {"role": "link", "name": BEARER, "href": BEARER, "field": False},      # name is the URL
        {"role": "button", "name": "", "href": None, "field": False},         # unnamed
        {"role": "button", "name": "", "href": None, "field": False},
        {"role": "textbox", "name": "Email", "href": None, "field": True},
    ]
    text = f"Invoice ready: {BEARER}\nalt: {BEARER}\nError: failed to load {BEARER}"
    obs = run(observer({URL: ext(URL, text, elements)}).observe(RUN, session(page), policy))
    wire = repr(obs.to_wire())
    for leak in ("/invoices", "in_1PqR9sT2uV3wX4yZ", "tok_Q7w8E9r0T1y2U3i4", "token=", "download?"):
        assert leak not in wire, leak
    assert "https://files.site.example" in obs.text           # origin + opaque reference only
    labels = [e.label_masked for e in obs.elements]
    assert labels[0] == "Download invoice"
    assert labels[2] == "(unnamed button 1)" and labels[3] == "(unnamed button 2)"
    assert len({e.handle for e in obs.elements}) == 5          # duplicates stay distinct
    assert obs.generation == 5


@pytest.mark.parametrize("current", [
    "https://other.example/account#/settings",  # off the session's site, route included
    "https://site.example.other.example/account?tab=2",
    "http://site.example/other",                # the site's https origin only
    "chrome://settings/", "chrome-extension://aeblfdkhhhdcdjpifhhbdiojplfjncoa/popup/index.html",
    "about:blank",
])
def test_pushstate_fragment_and_iframe_gate(current, tmp_path):
    """Design §4 (S4-4): a page is viewable when it belongs to the session — in an account
    session, an address on the site's origins or login origins, fragment and client route
    included; anything else is never read or captured."""
    policy = h.account_policy()
    for own in (URL + "#/settings", "https://site.example/other?tab=2",
                "https://login.example/u/login"):
        assert page_view_allowed(own, policy), own
    page = FakePage(current)
    obs = observer({current: ext(current, "secret page text")}, tmp_path)
    with pytest.raises(ObservationRefused) as err:
        run(obs.observe(RUN, session(page), policy))
    assert err.value.code == "page_off_session"
    assert run(obs.capture_operator(RUN, session(page), policy)) is None
    assert page.shots == 0


def test_route_change_after_url_check_refused():
    """The extraction's own reading of location (a client-side route taken just now) must be
    approved too, not only the address Playwright last reported."""
    policy = h.account_policy()
    page = FakePage(URL)
    with pytest.raises(ObservationRefused) as err:
        run(observer({URL: ext(URL + "#/secret", "x")}).observe(RUN, session(page), policy))
    assert err.value.code == "page_off_session"


def test_unapproved_frame_withheld():
    policy = h.account_policy()
    page = FakePage(URL, frames=[FakeFrame("https://ads.example/frame"), FakeFrame(URL + "#inner")])
    exts = {URL: ext(URL, "main text"), "https://ads.example/frame": ext("https://ads.example/frame", "AD SECRET"),
            URL + "#inner": ext(URL + "#inner", "inner text")}
    obs = run(observer(exts).observe(RUN, session(page), policy))
    assert "main text" in obs.text and "AD SECRET" not in obs.text
    assert "not shown" in obs.text


@pytest.mark.parametrize("state", ["live", "waiting_live"])
def test_login_live_extension_never_observed(state, tmp_path):
    policy = h.account_policy()
    page = FakePage(URL)
    obs = observer({URL: ext(URL, "x")}, tmp_path)
    for run_, sess in [(SimpleNamespace(run_id="r1", state=state), session(page)),
                       (RUN, session(page, login_active=True))]:
        with pytest.raises(ObservationRefused) as err:
            run(obs.observe(run_, sess, policy))
        assert err.value.code in ("live_view", "login_in_progress")
        with pytest.raises(ObservationRefused):
            run(obs.capture_operator(run_, sess, policy))
    assert page.shots == 0


def test_leases_do_not_make_a_page_viewable():
    """A login or manual lease admits traffic, never a model's view of the page: a page only a
    lease admits (another granted site's page in the live view, the provider's page during a
    login) is not viewable; the session's own pages stay viewable whatever lease exists."""
    lease = Lease("L", "r1", "s1", 1, "login")
    other = h.site("s2", origins=("https://other.example",), login=())
    policy = h.account_policy(sites=(h.site(), other), login_lease=lease,
                              manual_lease=Lease("M", "r1", "s1", 1, "manual"))
    assert not page_view_allowed("https://other.example/x", policy)
    assert not page_view_allowed("https://provider.example/authorize", policy)
    assert page_view_allowed(URL, policy)
    assert page_view_allowed("https://login.example/u/login", policy)


def test_infrastructure_hosts_are_not_viewable():
    assert not page_view_allowed("https://api.hcaptcha.com/checksiteconfig?v=1", h.account_policy())


def test_open_web_and_public_grant_pages_viewable():
    assert page_view_allowed("https://news.example/a#b", h.open_web_policy())
    assert not page_view_allowed("http://192.0.2.1/", h.open_web_policy())


def test_navigation_during_capture_discards_result(tmp_path):
    policy = h.account_policy()
    page = FakePage(URL)
    sess = session(page)

    def moves(frame):
        sess.view_generation += 1          # the page navigated while we read it

    obs = observer({URL: ext(URL, "x", [{"role": "button", "name": "Go", "href": None, "field": False}])},
                   tmp_path, on_extract=moves)
    with pytest.raises(ObservationRefused) as err:
        run(obs.observe(RUN, sess, policy))
    assert err.value.code == "stale"
    assert run(obs.capture_operator(RUN, sess, policy)) is None
    assert not (tmp_path / "screenshots").exists() or not list((tmp_path / "screenshots").iterdir())


def test_operator_screenshot_masks_secret_fields_and_frames_only(tmp_path):
    """Slice 5h: the operator's own screenshot masks what a question's picture masks — the
    password, one-time-code and payment-card fields (the gate's one secret rule) and every child
    frame; an ordinary field (a name, an email) stays visible, as does text the page renders
    (operator ruling 2026-10-29, slice 5: simple redaction of recognised secret fields). The
    stored file is only the final PNG."""
    policy = h.account_policy()
    page = FakePage(URL)
    log = []
    e = ext(URL, "x", [{"role": "textbox", "name": "Password", "href": None, "field": True,
                        "secret": True},
                       {"role": "textbox", "name": "Email", "href": None, "field": True},
                       {"role": "button", "name": "Save", "href": None, "field": False}],
            frames=1, log=log)
    o = observer({URL: e}, tmp_path)
    sid = run(o.capture_operator(RUN, session(page), policy))
    assert sid
    marks = [x for x in log if x[0] == "mark"]
    assert len(marks) == 2                     # the password field and the frame
    secret, plain = e.field_handles
    assert secret.ever_marked and e.frame_handles[0].ever_marked and not plain.ever_marked
    assert len([x for x in log if x[0] == "unmark"]) == 2
    assert page.mask[0][1].startswith('[data-casa-mask="')
    files = list((tmp_path / "screenshots").iterdir())
    assert [f.name for f in files] == [f"{sid}.png"]


def test_operator_screenshot_never_marks_an_ordinary_field(tmp_path):
    """The pizza form of phone round 7: name, phone, email, a time and comments, no secret
    field — nothing is marked and the picture is kept."""
    policy = h.account_policy()
    page = FakePage(URL)
    log = []
    fields = [{"role": "textbox", "name": n, "href": None, "field": True}
              for n in ("Name", "Phone", "Email", "Time", "Comments")]
    e = ext(URL, "x", fields, log=log)
    sid = run(observer({URL: e}, tmp_path).capture_operator(RUN, session(page), policy))
    assert sid and page.shots == 1
    assert not [x for x in log if x[0] == "mark"]


def test_browser_errors_become_typed_codes(tmp_path):
    """A browser-side failure — whose text may name an address or a selector — surfaces only
    as a typed code; a screenshot is simply not taken."""
    policy = h.account_policy()
    page = FakePage(URL)

    async def failing(frame):
        raise RuntimeError(f"Timeout waiting for locator('#pw') on {BEARER}")
    o = Observer(HandleRegistry(), tmp_path, extractor=failing)
    with pytest.raises(ObservationRefused) as err:
        run(o.observe(RUN, session(page), policy))
    assert err.value.code == "unavailable" and BEARER not in str(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__
    assert run(o.capture_operator(RUN, session(page), policy)) is None



def test_roles_are_an_allowlist():
    policy = h.account_policy()
    elements = [{"role": "/reset?code=private-code", "name": "Continue", "href": None, "field": False,
                 "tag_role": "button"},
                {"role": "https://evil.example/secret-path", "name": "Go", "href": None, "field": False,
                 "tag_role": "button"},
                {"role": "checkbox", "name": "Remember", "href": None, "field": True, "tag_role": "checkbox"}]
    obs = run(observer({URL: ext(URL, "x", elements)}).observe(RUN, session(FakePage(URL)), policy))
    assert [e.role for e in obs.elements] == ["button", "button", "checkbox"]
    assert "private-code" not in repr(obs.to_wire()) and "secret-path" not in repr(obs.to_wire())


def test_incomplete_mask_coverage_suppresses_the_screenshot(tmp_path):
    policy = h.account_policy()
    page = FakePage(URL)
    e = ext(URL, "x")
    e.complete = False                        # the page had more than masking could inventory
    assert run(observer({URL: e}, tmp_path).capture_operator(RUN, session(page), policy)) is None
    assert page.shots == 0


def test_a_closed_shadow_root_suppresses_the_screenshot(tmp_path):
    """Content in a closed shadow root can be neither read nor masked: no capture."""
    policy = h.account_policy()
    page = FakePage(URL)

    async def extractor(frame):
        return ext(URL, "x")

    async def closed(_page):
        return True
    o = Observer(HandleRegistry(), tmp_path, extractor=extractor, closed_shadow=closed)
    assert run(o.capture_operator(RUN, session(page), policy)) is None
    assert page.shots == 0


@pytest.mark.parametrize("secret", [True, False])
def test_a_field_added_during_capture_drops_the_screenshot(tmp_path, secret):
    """A secret field added during the capture was not covered: the image is not kept. An
    ordinary field added meanwhile is shown anyway, so the image stays."""
    policy = h.account_policy()
    page = FakePage(URL)
    field = {"role": "textbox", "name": "Code", "href": None, "field": True, "secret": secret}
    reads = [ext(URL, "x"), ext(URL, "x", [field])]

    async def extractor(frame):
        return reads.pop(0)
    o = Observer(HandleRegistry(), tmp_path, extractor=extractor, closed_shadow=_no_closed_roots)
    got = run(o.capture_operator(RUN, session(page), policy))
    assert page.shots == 1 and not reads          # captured, then re-read
    if secret:
        assert got is None
        assert not (tmp_path / "screenshots").exists() or not list((tmp_path / "screenshots").iterdir())
    else:
        assert got and [f.name for f in (tmp_path / "screenshots").iterdir()] == [f"{got}.png"]


def test_a_field_replaced_during_capture_drops_the_screenshot(tmp_path):
    """A marked field that left the page (or lost its mark) during the capture was not
    covered: the image is not kept."""
    policy = h.account_policy()
    page = FakePage(URL)
    field = {"role": "textbox", "name": "Code", "href": None, "field": True, "secret": True}
    e = ext(URL, "x", [field])
    e.field_handles[0].connected = False
    o = Observer(HandleRegistry(), tmp_path, extractor=lambda f: _const(e),
                 closed_shadow=_no_closed_roots)
    assert run(o.capture_operator(RUN, session(page), policy)) is None
    assert page.shots == 1
    assert not (tmp_path / "screenshots").exists() or not list((tmp_path / "screenshots").iterdir())


async def _const(value):
    return value


def test_a_long_label_is_masked_before_it_is_cut():
    """A credential that runs past the label's length is masked whole, never cut into a
    recognisable prefix."""
    policy = h.account_policy()
    key = "".join(["ghp_", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"])
    name = "x " * 145 + key
    elements = [{"role": "button", "name": name, "href": None, "field": False}]
    obs = run(observer({URL: ext(URL, "x", elements)}).observe(RUN, session(FakePage(URL)), policy))
    label = obs.elements[0].label_masked
    assert len(label) <= 300 and "ghp_" not in label and key[:10] not in label


def test_a_frame_shows_only_what_its_own_reading_says_is_the_sessions():
    """r3 Astra 1a-y, generalised: a frame's content is shown only when the gate admits the
    address the extraction itself read. A frame on the site at the check that the extraction
    found already elsewhere is withheld."""
    policy = h.account_policy()
    child = FakeFrame(URL + "#inner")
    page = FakePage(URL, frames=[child])

    async def extractor(frame):
        if frame is child:
            child.url = "https://other.example/private"   # it moved before the extraction
            return ext(child.url, "FOREIGN TEXT")
        return ext(URL, "main text")
    obs = run(Observer(HandleRegistry(), __import__("pathlib").Path("/nonexistent"),
                       extractor=extractor, closed_shadow=_no_closed_roots)
              .observe(RUN, session(page), policy))
    assert "main text" in obs.text and "FOREIGN TEXT" not in obs.text
    assert "not shown" in obs.text


class _CropPage:
    """evaluate answers the content measure, then the cut."""
    def __init__(self, box, cut):
        self.answers = [box, cut]
        self.args = []

    async def evaluate(self, js, *arg):
        self.args.append(arg)
        got = self.answers.pop(0)
        if isinstance(got, Exception):
            raise got
        return got


class _Target:
    def __init__(self, bb):
        self.bb = bb

    async def bounding_box(self):
        return self.bb


PNG = b"\x89PNG\r\n\x1a\nwhole"
SMALL = b"\x89PNG\r\n\x1a\nsmall"


def test_crop_cuts_the_image_to_the_content_with_a_margin():
    """Slice 5f: the content measured after the capture, plus 24 px, is cut out of the image."""
    import base64
    from casa_browser.observe import crop_png
    page = _CropPage([8, 8, 700, 600, 1920, 1080], base64.b64encode(SMALL).decode())
    assert run(crop_png(page, PNG)) == (SMALL, (0, 0))
    assert page.args[1][0][1:] == [0, 0, 724, 624, 1]


def test_crop_keeps_a_target_outside_the_text_and_a_minimum_size():
    import base64
    from casa_browser.observe import content_clip
    clip = run(content_clip(_CropPage([100, 100, 150, 120, 1920, 1080], None),
                            _Target({"x": 1000, "y": 900, "width": 80, "height": 30})))
    assert clip == {"x": 76, "y": 76, "width": 1028, "height": 878}
    small = run(content_clip(_CropPage([1900, 1070, 1910, 1075, 1920, 1080], None)))
    assert small["width"] == 480 and small["height"] == 240          # Telegram's side ratio
    assert small["x"] + small["width"] <= 1920 and small["y"] + small["height"] <= 1080


def test_crop_falls_back_to_the_whole_image():
    import base64
    from casa_browser.observe import crop_png
    box = [8, 8, 700, 600, 1920, 1080]
    for page in (_CropPage(None, None), _CropPage(RuntimeError("gone"), None),
                 _CropPage([0, 0, 1920, 1080, 1920, 1080], None),
                 _CropPage(box, RuntimeError("gone")),
                 _CropPage(box, base64.b64encode(b"not a png").decode())):
        assert run(crop_png(page, PNG)) == (PNG, (0, 0))


# --------------------------------------------------------------------------- field values (5j)

def test_an_observation_shows_what_ordinary_fields_hold():
    """Slice 5j (operator ruling 2026-10-31, phone round 8): an ordinary field's value and a
    checkbox's state reach the observation; a value is masked like any page text and bounded;
    a field the extraction gave no value (a secret one) has none."""
    policy = h.account_policy()
    elements = [
        {"role": "textbox", "name": "Date", "href": None, "field": True, "kind": "date",
         "value": "2026-11-15"},
        {"role": "checkbox", "name": "Extra cheese", "href": None, "field": True, "checked": True},
        {"role": "textbox", "name": "Link", "href": None, "field": True, "value": BEARER},
        {"role": "textbox", "name": "Notes", "href": None, "field": True, "value": "word " * 400},
        {"role": "textbox", "name": "Password", "href": None, "field": True},
        {"role": "button", "name": "Go", "href": None, "field": False},
    ]
    obs = run(observer({URL: ext(URL, "x", elements)}).observe(RUN, session(FakePage(URL)), policy))
    date, box, link, notes, pw, go = obs.elements
    assert date.value_masked == "2026-11-15" and date.kind == "date"
    assert box.checked is True and box.value_masked is None
    assert "tok_Q7w8E9r0T1y2U3i4" not in link.value_masked and "/invoices" not in link.value_masked
    assert len(notes.value_masked) <= 600
    assert pw.value_masked is None and pw.checked is None
    assert go.value_masked is None
    wire = obs.to_wire()["elements"]
    assert wire[0]["value_masked"] == "2026-11-15" and wire[1]["checked"] is True
    assert "value_masked" not in wire[4] and "checked" not in wire[4]


def test_a_field_that_is_secret_when_read_again_shows_no_value():
    """Slice 5j, the capture's rule: right after the reading every field whose value would be
    shown is asked the gate's one secret rule again; one that answers secret now (the page made
    it a password field) shows nothing."""
    policy = h.account_policy()
    elements = [
        {"role": "textbox", "name": "PIN", "href": None, "field": True, "value": "Kept-0007",
         "now_secret": True},
        {"role": "checkbox", "name": "Remember", "href": None, "field": True, "checked": False,
         "now_secret": True},
        {"role": "textbox", "name": "Name", "href": None, "field": True, "value": "Jan"},
    ]
    obs = run(observer({URL: ext(URL, "x", elements)}).observe(RUN, session(FakePage(URL)), policy))
    pin, remember, name = obs.elements
    assert pin.value_masked is None and remember.checked is None
    assert name.value_masked == "Jan"
    assert "Kept-0007" not in repr(obs.to_wire())


def test_a_failed_second_reading_shows_no_value():
    """When the field cannot be asked again (it left the page, the frame went away), its value
    is not shown; the observation itself still comes back."""
    policy = h.account_policy()

    class Gone(FakeEl):
        async def evaluate(self, js, *args):
            raise RuntimeError("Execution context was destroyed")

    e = ext(URL, "x", [{"role": "textbox", "name": "Name", "href": None, "field": True,
                        "value": "Kept-0008"}])
    e.element_handles[0] = Gone([])
    obs = run(observer({URL: e}).observe(RUN, session(FakePage(URL)), policy))
    assert obs.elements[0].value_masked is None
    assert "Kept-0008" not in repr(obs.to_wire())


# Slice 5m: the one frame rule (``frame_shown``), shared by the observation and the check.
_O = "https://site.example"


@pytest.mark.parametrize("address,href,origin,parent,shown", [
    # a frame with a web address: its reading reports it and the gate admits it (unchanged)
    (URL, URL, _O, (True, _O), True),
    (URL, URL, _O, None, False),                                  # no parent known
    (URL, URL, _O, (False, "https://other.example"), False),      # inside a withheld frame
    (URL, URL, "null", (True, _O), False),                        # sandboxed: opaque origin
    (URL, "https://site.example/other", _O, (True, _O), False),   # moved during the read
    ("https://other.example/x", "https://other.example/x", "https://other.example",
     (True, _O), False),                                          # off the session
    # no address of its own: its origin is its shown parent's
    ("about:blank", URL, _O, (True, _O), True),                   # document.open: parent's href
    ("about:blank", "about:blank", _O, (True, _O), True),
    ("about:srcdoc", "about:srcdoc", _O, (True, _O), True),
    ("about:blank", "about:blank", _O, (False, _O), False),       # its parent is withheld
    ("about:blank", "about:blank", _O, None, False),              # no parent known
    ("about:srcdoc", "about:srcdoc", "null", (True, _O), False),  # sandboxed: opaque origin
    ("about:blank", "about:blank", None, (True, _O), False),
    ("about:blank", "about:blank", "https://other.example", (True, _O), False),
    ("about:blank", "https://other.example/x", _O, (True, _O), False),  # not admitted
    ("about:blank", "about:blank", "null", (True, "null"), False),
])
def test_one_frame_rule(address, href, origin, parent, shown):
    from casa_browser.observe import frame_shown
    assert frame_shown(address, href, origin, parent, h.account_policy()) is shown


def test_a_written_frame_is_observed_and_its_written_child_of_a_withheld_frame_is_not():
    """A frame with no address of its own shows under its shown parent's origin; the same
    kind of frame under a withheld one stays withheld (parents are judged first)."""
    policy = h.account_policy()
    foreign = FakeFrame("https://other.example/f")
    written = FakeFrame("about:blank")
    nested = FakeFrame("about:blank", parent_frame=foreign)
    deep = FakeFrame("about:srcdoc", parent_frame=written)
    child = FakeFrame("https://site.example/child")
    inside = FakeFrame("https://site.example/inside", parent_frame=foreign)
    boxed = FakeFrame("https://site.example/boxed")
    page = FakePage(URL, frames=[deep, nested, inside, written, foreign, child, boxed])
    reads = []
    exts = {URL: ext(URL, "Own page")}
    exts[URL].origin = _O
    obs = observer(exts)

    async def extractor(frame):
        reads.append(frame.url)
        if frame is deep:
            return Extraction("about:srcdoc", "", "Deep demo", [], [], [], [], True, _O)
        if frame in (child, inside, boxed):
            return Extraction(frame.url, "", "Text of " + frame.url.rsplit("/", 1)[1], [], [],
                              [], [], True, "null" if frame is boxed else _O)
        if frame is written:
            return Extraction(URL, "", "Written demo", [{"role": "textbox", "name": "Pickup",
                              "href": None, "field": True}], [object()], [], [], True, _O)
        if frame is nested:
            return Extraction("about:blank", "", "Nested secret", [], [], [], [], True,
                              "https://other.example")
        return exts[frame.url]
    obs.extractor = extractor
    got = run(obs.observe(RUN, session(page), policy))
    assert "Written demo" in got.text and "Deep demo" in got.text
    assert "Nested secret" not in got.text and "Text of child" in got.text
    assert "Text of inside" not in got.text and "Text of boxed" not in got.text
    assert "https://other.example/f" not in reads       # an unapproved web frame is never read
    assert got.text.count("[a frame on this page is not shown]") == 4
    assert [e.label_masked for e in got.elements] == ["Pickup"]


# --------------------------------------------------------------------------- pictures for a model


def test_capture_for_model_keeps_no_file_and_the_operators_capture_still_does(tmp_path):
    policy, page = h.account_policy(), FakePage(URL)
    log = []
    e = ext(URL, "x", [{"role": "textbox", "name": "Password", "href": None, "field": True,
                        "secret": True}], frames=1, log=log)
    store = SimpleNamespace(rows=[], put_screenshot=lambda *a: store.rows.append(a))
    o = observer({URL: e}, tmp_path)
    o.store = store
    png = run(o.capture_for_model(RUN, session(page), policy))
    assert png.startswith(b"\x89PNG") and page.shots == 1
    assert not (tmp_path / "screenshots").exists() and store.rows == []
    # the same rule: the secret field and the frame were marked and the marks removed
    assert len([x for x in log if x[0] == "mark"]) == 2 == len([x for x in log if x[0] == "unmark"])
    assert run(o.capture_operator(RUN, session(page), policy))
    assert len(list((tmp_path / "screenshots").iterdir())) == 1 and len(store.rows) == 1


def test_capture_for_model_follows_the_gate_and_the_state(tmp_path):
    policy = h.account_policy()
    off = FakePage("https://other.example/x")
    o = observer({off.url: ext(off.url, "x")}, tmp_path)
    assert run(o.capture_for_model(RUN, session(off), policy)) is None and off.shots == 0
    page = FakePage(URL)
    o = observer({URL: ext(URL, "x")}, tmp_path)
    for run_, sess in [(SimpleNamespace(run_id="r1", state="live"), session(page)),
                       (RUN, session(page, login_active=True))]:
        with pytest.raises(ObservationRefused):
            run(o.capture_for_model(run_, sess, policy))
    assert page.shots == 0


# --------------------------------------------------------------------------- reading in full


def test_an_observation_says_its_text_was_cut():
    policy = h.account_policy()
    page = FakePage(URL)
    long = "word " * 5000
    obs = run(observer({URL: ext(URL, long)}).observe(RUN, session(page), policy))
    assert obs.text_cut and len(obs.text) == 20_000 and obs.text == long[:20_000]
    assert "text_cut" in obs.to_wire()
    obs = run(observer({URL: ext(URL, "short")}).observe(RUN, session(page), policy))
    assert not obs.text_cut and "text_cut" not in obs.to_wire()
    # the extraction's own cut says so even when what is left would fit
    cut = replace_text_cut(ext(URL, "short"))
    assert run(observer({URL: cut}).observe(RUN, session(page), policy)).text_cut


def replace_text_cut(extraction):
    extraction.text_cut = True
    return extraction


def test_read_text_is_the_whole_masked_text_and_issues_no_handle():
    policy = h.account_policy()
    page = FakePage(URL)
    long = "word " * 20_000 + BEARER
    o = observer({URL: ext(URL, long, [{"role": "button", "name": "Go", "href": None, "field": False}])})
    issued = []
    o.registry.issue = lambda *a: issued.append(a)
    text, whole, masked = run(o.read_text(RUN, session(page), policy))
    assert whole and masked and text.startswith("word word") and len(text) > 100_000
    assert "tok_Q7w8E9r0T1y2U3i4" not in text and "https://files.site.example" in text
    assert issued == []
    assert run(observer({URL: ext(URL, "plain")}).read_text(RUN, session(page), policy)) == (
        "plain", True, False)


@pytest.mark.parametrize("how", ["incomplete", "cut", "too_long"])
def test_read_text_never_calls_a_cut_text_whole(how):
    policy = h.account_policy()
    page = FakePage(URL)
    e = ext(URL, "word " * 90_000 if how == "too_long" else "words")
    e.complete = how != "incomplete"
    e.text_cut = how == "cut"
    assert run(observer({URL: e}).read_text(RUN, session(page), policy))[1] is False


def test_read_text_refused_off_the_session_and_when_the_page_moves():
    policy = h.account_policy()
    off = FakePage("https://other.example/x")
    with pytest.raises(ObservationRefused) as err:
        run(observer({off.url: ext(off.url, "secret")}).read_text(RUN, session(off), policy))
    assert err.value.code == "page_off_session"
    page = FakePage(URL)
    sess = session(page)

    def moves(frame):
        sess.view_generation += 1
    with pytest.raises(ObservationRefused) as err:
        run(observer({URL: ext(URL, "x")}, on_extract=moves).read_text(RUN, sess, policy))
    assert err.value.code == "stale"


def frame_reading(rows):
    """A frame's rows reading (``observe.rows``) holding these rows, one record each."""
    blocks, items = [], []
    for i, r in enumerate(rows):
        if r.get("text"):
            blocks.append([i, r["text"]])
        items += [[i, "field", n, v] for n, v in r.get("fields") or ()]
        items += [[i, "press", p, None] for p in r.get("press") or ()]
    return {"records": len(rows), "blocks": blocks, "items": items}


def rows_observer(found, complete=True, frames=None):
    seen = [] if frames is None else frames

    async def reader(frame):
        seen.append(frame)
        return frame_reading(found), complete
    o = observer({URL: ext(URL, "page")})
    o.rows_reader = reader
    return o


def test_read_rows_is_masked_json_lines_and_issues_no_handle():
    """The design's rows reading: one JSON line a row, each text masked on its own (an address
    never swallows the line's punctuation), a secret value as the page reading left it."""
    found = [{"text": "Line 1 see https://site.example/a?t=SECRETQ", "fields": [["Qty", "3"],
              ["Code", "[not shown]"], ["Done", True]], "press": ["Save"]},
             {"text": "Line 2", "fields": [], "press": []}]
    frames: list = []
    o = rows_observer(found, frames=frames)
    issued = []
    o.registry.issue = lambda *a: issued.append(a)
    text, whole, masked = run(o.read_rows(RUN, session(FakePage(URL)), h.account_policy()))
    lines = [json.loads(line) for line in text.split("\n")]
    assert len(lines) == 2 and whole and masked and issued == []
    assert lines[0]["fields"] == [["Qty", "3"], ["Code", "[not shown]"], ["Done", True]]
    assert lines[0]["press"] == ["Save"] and "SECRETQ" not in text
    assert lines[0]["text"].startswith("Line 1 see https://site.example ")
    assert lines[1] == {"text": "Line 2", "fields": [], "press": []}
    assert len(frames) == 1


def test_rows_mask_a_value_across_records_as_one_text():
    """Blocks are masked together, as an observation's text is: a private key whose delimiters
    sit in a parent record and whose body is in a record nested in it shows no part of it."""
    reading = {"records": 2, "items": [], "blocks": [
        [0, "-----BEGIN PRIVATE KEY-----"], [1, "YWJjZGVmZ2hpamts"], [0, "-----END PRIVATE KEY-----"],
        [1, "Nested note"], [-1, "Loose note"]]}
    text, whole, masked = rows_text([reading])
    lines = [json.loads(line) for line in text.split("\n")]
    assert masked and whole and "YWJjZGVmZ2hpamts" not in text and "PRIVATE KEY" not in text
    assert lines[0]["text"] == "[masked]" and lines[1]["text"] == "Nested note"
    assert lines[2] == {"text": "Loose note", "fields": [], "press": [], "outside": True}


def test_rows_give_a_selects_options_masked_with_its_field():
    reading = {"records": 1, "blocks": [], "items": [
        [0, "field", "Bank", "Choose", ["Choose", "https://bank.example/pay?token=SELQUERY"]]]}
    text, _whole, masked = rows_text([reading])
    field = json.loads(text)["fields"][0]
    assert masked and "SELQUERY" not in text
    assert field[:2] == ["Bank", "Choose"] and field[2][1].startswith("https://bank.example ")


def test_read_rows_is_not_whole_past_its_limit_and_refused_off_the_session():
    policy = h.account_policy()
    assert run(rows_observer([], complete=False).read_rows(
        RUN, session(FakePage(URL)), policy))[1] is False
    off = FakePage("https://other.example/x")
    o = rows_observer([])
    o.extractor = lambda f: _const(ext(off.url, "x"))
    with pytest.raises(ObservationRefused) as err:
        run(o.read_rows(RUN, session(off), policy))
    assert err.value.code == "page_off_session"
    sess = session(FakePage(URL))
    o = rows_observer([])

    async def moves(frame):
        sess.view_generation += 1             # the page moved while its rows were read
        return frame_reading([{"text": "x", "fields": [], "press": []}]), True
    o.rows_reader = moves
    with pytest.raises(ObservationRefused) as err:
        run(o.read_rows(RUN, sess, policy))
    assert err.value.code == "stale"


# -- a search (``find``) --------------------------------------------------------------------------

async def _nothing_closed(_page):
    return False


def searching(extractions, terms=None, closed=_nothing_closed):
    async def extractor(frame, find=None):
        if terms is not None:
            terms.append(find)
        return extractions[frame.url]
    return Observer(HandleRegistry(), __import__("pathlib").Path("/nonexistent"), extractor=extractor,
                    closed_shadow=_no_closed_roots, closed_any=closed)


def test_the_one_match_is_made_on_what_the_observation_would_show():
    """Any case, plain words, on the masked name, the value and an option's name; only ordinals
    leave it, with two neighbours either side."""
    token = "".join(["ghp_", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"])
    items = [{"name": f"Filler {n}", "value": None, "options": []} for n in range(9)]
    items[2] = {"name": "Publish it", "value": None, "options": []}
    items[6] = {"name": "Note", "value": "publish later", "options": []}
    items[8] = {"name": "Where", "value": "Here", "options": ["Here", "Publish point"]}
    assert search_ask(items, "PUBLISH") == {"found": [2, 6], "options": [[8, [1]]],
                                            "near": [0, 1, 2, 3, 4, 5, 6, 7, 8]}
    assert search_ask(items, "Pub.*it") == {"found": [], "options": [], "near": []}
    items = [{"name": f"Key {token}", "value": f"https://site.example/a/{token}", "options": [token]}]
    for part in (token[10:20], "/a/"):
        assert search_ask(items, part) == {"found": [], "options": [], "near": []}, part
    assert search_ask(items, "Key")["found"] == [0]


def test_what_is_found_comes_from_that_one_match_and_a_field_secret_when_read_again_is_not():
    policy = h.account_policy()
    elements = [
        {"role": "button", "name": "Publish it", "href": None, "field": False, "found": True},
        {"role": "textbox", "name": "Note", "href": None, "field": True, "value": "publish later", "found": True},
        {"role": "textbox", "name": "PIN", "href": None, "field": True, "value": "publish",
         "now_secret": True, "found": True},
        {"role": "button", "name": "Publish too", "href": None, "field": False, "found": False},
        {"role": "button", "name": "", "href": None, "field": False},
    ]
    terms = []
    obs = run(searching({URL: ext(URL, "x", elements)}, terms).observe_page(
        RUN, session(FakePage(URL)), policy, "PUBLISH"))[0]
    assert terms == ["PUBLISH"]
    assert [e.found for e in obs.elements] == [True, True, False, False, False]
    assert obs.find_whole is True and obs.to_wire()["find"] == {"whole": True, "left": 0}
    assert [e.get("found") for e in obs.to_wire()["elements"]] == [True, True, None, None, None]


def test_an_observation_with_no_search_says_nothing_of_one():
    policy = h.account_policy()
    obs = run(observer({URL: ext(URL, "x", [{"role": "button", "name": "A", "href": None,
                                             "field": False}])}).observe(RUN, session(FakePage(URL)), policy))
    assert obs.find_whole is None
    assert not {"find", "controls_left"} & set(obs.to_wire()) and "found" not in obs.to_wire()["elements"][0]


def test_the_counts_of_what_the_cap_left_out_reach_the_wire():
    policy = h.account_policy()
    extraction = ext(URL, "x", [{"role": "button", "name": "A", "href": None, "field": False}])
    extraction.left, extraction.found_left = 30, 4
    obs = run(searching({URL: extraction}).observe_page(RUN, session(FakePage(URL)), policy, "A"))[0]
    wire = obs.to_wire()
    assert wire["controls_left"] == 30 and wire["find"] == {"whole": True, "left": 4}
    assert obs.text.startswith("[30 more controls on this page are not listed") and "find" in obs.text


@pytest.mark.parametrize("cut", ["a limit", "a closed root", "a closed root unasked"])
def test_a_search_that_could_not_be_whole_is_not_called_whole(cut):
    policy = h.account_policy()
    extraction = ext(URL, "x", [])
    extraction.whole = cut != "a limit"

    async def closed(_page):
        return cut != "a limit"
    obs = run(searching({URL: extraction}, closed=closed).observe_page(
        RUN, session(FakePage(URL)), policy, "x"))[0]
    assert obs.find_whole is False and obs.to_wire()["find"]["whole"] is False


def test_any_case_means_any_case():
    for words, text in (("STRASSE", "die Straße"), ("straße", "DIE STRASSE"), ("é", "É")):
        assert holds_words(words, text), words
    assert not holds_words("a.c", "abc") and not holds_words("x", None, "")
