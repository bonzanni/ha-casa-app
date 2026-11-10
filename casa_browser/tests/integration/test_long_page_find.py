"""Reaching any control of a page with more controls than an observation lists: the search and the
scroll in real Chromium, through the real ``EXTRACT``, ``READ``, ``Observer``, ``Pacer`` and the
controller's own ``_scroll``. No fakes of the page; the pages are local, served by the browser
context's router, so nothing leaves the machine. The whole path through the production controller
and its check is in ``test_long_live_page.py``.

Runs in the image like the other ``docker`` tests, and on a developer's machine by naming this
file (it needs only Patchright's Chromium)."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from patchright.async_api import async_playwright

from casa_browser import gate
from casa_browser.controller import RunController
from casa_browser.handles import HandleRegistry
from casa_browser.observe import EXTRACT, MAX_ELEMENTS, Observer, closed_shadow_any, extract
from casa_browser.pacing import Pacer
from casa_browser.policy import RunPolicy, SessionRef, SiteGrant
from casa_browser.protocol import Action, ActionResult, Site

pytestmark = pytest.mark.docker

HOST = "https://shop.example"
OTHER = "https://elsewhere.example"
SITE = Site("s", "Shop", (HOST,), (), None, (), None)
POLICY = RunPolicy("run", "account", (SiteGrant("s", "account"),), (SITE,), None, None, 1,
                   SessionRef("account", "s", 1))
IMAGE_CHROME = "/opt/google/chrome/chrome"
PEOPLE = "".join(f"<option>Person {n}</option>" for n in range(6))
OUT = '<div id="out"></div>'
PRESSED = "document.getElementById('out').textContent = this.textContent"


def rows(count: int = 240) -> str:
    """Three semantic controls and six options a row: the cap holds about a quarter of it."""
    return "".join(f'<div style="height:40px">Day {n} <select aria-label="Who on day {n}">{PEOPLE}'
                   f'</select><label><input type="checkbox"> Done {n}</label> <a href="/d/{n}">Details {n}</a></div>'
                   for n in range(count))


def html(body: str) -> str:
    return f'<!doctype html><html><head><meta charset="utf-8"><title>T</title></head><body>{body}</body></html>'


LONG = rows() + f'<button id="go" onclick="{PRESSED}">Publish</button>' + OUT


@pytest.fixture
async def show():
    async with async_playwright() as p:
        kw = {"executable_path": IMAGE_CHROME} if os.path.exists(IMAGE_CHROME) else {}
        # As in production, a frame of another site is a process of its own, which Playwright's own
        # launch turns off.
        browser = await p.chromium.launch(headless=True, args=["--site-per-process"],
                                          ignore_default_args=["--disable-site-isolation-trials"], **kw)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        pages = {}

        async def serve(route):
            await route.fulfill(status=200, content_type="text/html",
                                body=pages.get(route.request.url, html("<p>elsewhere</p>")))
        await context.route("**/*", serve)
        page = await context.new_page()

        async def go(body: str, *, frames: dict | None = None):
            pages.update(frames or {})
            pages[f"{HOST}/"] = html(body)
            await page.goto(f"{HOST}/", wait_until="load")
            return page
        yield go
        await browser.close()


class Rig:
    """The real observer over a real page, and what a handle it issued resolves to."""

    def __init__(self, page, tmp_path):
        self.page = page
        self.registry = HandleRegistry()
        self.observer = Observer(self.registry, tmp_path)
        self.run = SimpleNamespace(run_id="run", state="running")
        self.session = SimpleNamespace(page=page, view_generation=1, site_id="s", login_active=False)
        self.policy = POLICY

    async def observe(self, find=None):
        self.registry.drop_run("run")
        obs, _url, _title = await self.observer.observe_page(self.run, self.session, self.policy, find)
        return obs

    def target(self, handle):
        return self.registry.resolve(handle, "run", 1)

    async def press(self, handle):
        await Pacer(self.page).click(self.target(handle).element)

    async def scroll(self, handle, active, direction="down", amount="page"):
        """The controller's own ``_scroll``, with the real pacer, as ``POST …/action`` runs it."""
        async def done(_active, _kind):
            return ActionResult("done", 1)
        ctl = SimpleNamespace(c=SimpleNamespace(pacer=Pacer), _action_done=done)
        result = await RunController._scroll(ctl, active, self.session, self.target(handle),
                                             Action("scroll", handle, 1, direction=direction, amount=amount))
        return result.to_wire()["position"]


@pytest.fixture
def rig(tmp_path):
    return lambda page: Rig(page, tmp_path)


def found(obs) -> list[str]:
    return [e.label_masked for e in obs.elements if e.found]


def by_label(obs, label):
    return next(e for e in obs.elements if e.label_masked == label)


def in_view(page, selector):
    return page.evaluate("""(s) => { const r = document.querySelector(s).getBoundingClientRect();
      return r.bottom > 0 && r.top < innerHeight; }""", selector)


# -- find --------------------------------------------------------------------------------------

async def test_a_control_past_the_cap_is_found_from_the_top_and_its_handle_presses_it(show, rig):
    page = await show(LONG)
    r = rig(page)
    plain = await r.observe()
    assert len(plain.elements) == MAX_ELEMENTS and "Publish" not in [e.label_masked for e in plain.elements]
    assert await page.evaluate("() => scrollY") == 0
    obs = await r.observe("PUBLISH")                          # any case
    assert found(obs) == ["Publish"] and obs.find_whole is True and obs.find_left == 0
    assert len(obs.elements) == MAX_ELEMENTS
    assert await page.evaluate("() => scrollY") == 0          # the search moved nothing
    await r.press(by_label(obs, "Publish").handle)
    assert await page.evaluate("() => document.getElementById('out').textContent") == "Publish"


async def test_all_controls_counts_what_exists_and_what_is_listed(show, rig):
    page = await show(LONG)
    obs = await rig(page).observe()
    # 240 rows of a select, its 6 options, a checkbox and a link, and the one button
    assert obs.controls_left + len(obs.elements) == 240 * 9 + 1
    wire = obs.to_wire()
    assert wire["controls_left"] == obs.controls_left and "find" not in wire
    assert f"{obs.controls_left} more controls" in obs.text and "find" in obs.text.split("\n")[0]


async def test_the_words_are_plain_and_a_term_that_matches_nothing_says_the_whole_page_was_searched(show, rig):
    page = await show(LONG)
    r = rig(page)
    for term in ("nothing holds this", "Pub.*sh", "^Publish$", "["):
        obs = await r.observe(term)
        assert found(obs) == [] and obs.find_whole is True, term
        assert obs.to_wire()["find"] == {"whole": True, "left": 0}


async def test_what_holds_the_words_in_a_field_or_an_option_past_the_cap_is_found(show, rig):
    options = "".join(f"<option>Place {n}</option>" for n in range(500))
    page = await show(rows() + '<input id="v" value="held in the field">'
                      f'<select aria-label="Where">{options}</select>')
    r = rig(page)
    assert found(await r.observe("held IN the")) == ["(unnamed textbox 1)"]
    obs = await r.observe("Place 450")
    assert found(obs) == ["Place 450"], "an option past the room its select was given"
    labels = [e.label_masked for e in obs.elements]
    where = labels[labels.index("Place 450") - 1]                # it follows its select,
    assert where.startswith("Where (") and where.endswith(" of 500 options listed)")   # cut short
    assert [w.split(" (")[0] for w in found(await r.observe("Where"))] == ["Where"]


async def test_more_matches_than_fit_are_counted(show, rig):
    page = await show(rows(500))
    obs = await rig(page).observe("Details")
    assert len(found(obs)) + obs.find_left == 500 and obs.find_left > 0 and obs.find_whole is True


async def test_options_found_that_do_not_fit_are_counted(show, rig):
    """One found option in each of 500 selects: listed or not, every one is accounted for."""
    page = await show(rows(500))
    obs = await rig(page).observe("Person 3")
    assert len(found(obs)) + obs.find_left == 500 and obs.find_left > 0
    assert set(found(obs)) == {"Person 3"}


@pytest.mark.parametrize("hit,count", [("<button>Hit</button>", 300),
                                       ("<select><option>Miss</option><option>Hit</option></select>", 100)],
                         ids=["control", "option"])
async def test_what_is_found_goes_before_its_neighbours(show, rig, hit, count):
    """Four neighbours for each: taken in page order the cap would end at half of what was found."""
    page = await show("".join(hit + "<button>Filler</button>" * 4 for _ in range(count)))
    obs = await rig(page).observe("hit")
    assert len(found(obs)) == count and obs.find_left == 0


async def test_a_neighbour_of_a_control_found_is_its_neighbour_on_the_page(show, rig):
    """The unnamed button beside what is found: a search's list keeps it, and in its place."""
    page = await show(rows() + '<button>Before it</button><a href="/t">The row</a><button aria-hidden="true" title=""></button>')
    obs = await rig(page).observe("The row")
    labels = [e.label_masked for e in obs.elements]
    at = labels.index("The row")
    assert labels[at - 1] == "Before it" and labels[at + 1].startswith("(unnamed button")


@pytest.mark.parametrize("what,body,frames", [
    ("a frame not shown", rows(5) + f'<iframe src="{OTHER}/x" style="width:300px;height:100px"></iframe>',
     {f"{OTHER}/x": html("<button>Not ours</button>")}),
    ("a closed root", rows(5) + """<div id="c"></div><script>
       document.getElementById('c').attachShadow({mode: 'closed'}).innerHTML = '<button>Inside</button>';</script>""", {}),
    ("too many shadow roots", """<div id="r"></div><script>const r = document.getElementById('r');
       for (let i = 0; i < 5000; i++) r.appendChild(document.createElement('div')).attachShadow({mode: 'open'});
       </script>""", {}),
])
async def test_a_search_that_could_not_be_whole_says_so(show, rig, what, body, frames):
    page = await show(body, frames=frames)
    obs = await rig(page).observe("Inside")
    assert found(obs) == [] and obs.find_whole is False, what
    assert obs.to_wire()["find"]["whole"] is False


CLOSED_ROOT = """<div id="c"></div><script>
  document.getElementById('c').attachShadow({mode: 'closed'}).innerHTML = '<button>Inside</button>';</script>"""
SECOND = "https://second.example"
TWO_SITES = RunPolicy("run", "account", (SiteGrant("s", "account"),),
                      (Site("s", "Shop", (HOST, SECOND), (), None, (), None),), None, None, 1,
                      SessionRef("account", "s", 1))


@pytest.mark.parametrize("address", [f"{HOST}/small", f"{SECOND}/small"], ids=["same-origin", "other-origin"])
async def test_a_closed_root_in_a_shown_frame_means_the_search_was_not_whole(show, rig, address):
    frame = f'<iframe src="{address}" style="width:300px;height:100px"></iframe>'
    page = await show("<button>Own</button>" + frame, frames={address: html(CLOSED_ROOT)})
    r = rig(page)
    r.policy = TWO_SITES
    obs = await r.observe("Inside")
    assert page.frames[1].url == address and found(obs) == [] and obs.find_whole is False


async def test_a_closed_root_below_an_open_one_in_a_frame_means_the_search_was_not_whole(show, rig):
    nested = """<div id="o"></div><script>const o = document.getElementById('o').attachShadow({mode: 'open'});
      o.innerHTML = '<div id="c"></div>'; o.getElementById('c').attachShadow({mode: 'closed'});</script>"""
    frame = f'<iframe src="{HOST}/small" style="width:300px;height:100px"></iframe>'
    page = await show("<button>Own</button>" + frame, frames={f"{HOST}/small": html(nested)})
    assert (await rig(page).observe("Inside")).find_whole is False


@pytest.mark.parametrize("address", [f"{HOST}/small", f"{SECOND}/small"], ids=["same-origin", "other-origin"])
async def test_a_shown_frame_with_no_closed_root_leaves_the_search_whole(show, rig, address):
    frame = f'<iframe src="{address}" style="width:300px;height:100px"></iframe>'
    page = await show("<button>Own</button>" + frame, frames={address: html("<button>In it</button>")})
    r = rig(page)
    r.policy = TWO_SITES
    obs = await r.observe("nothing holds this")
    assert found(obs) == [] and obs.find_whole is True
    assert found(await r.observe("in it")) == ["In it"]


@pytest.mark.parametrize("address,whole", [(f"{SECOND}/small", False), (f"{HOST}/small", True)],
                         ids=["own-process", "parents-process"])
async def test_a_frame_that_cannot_be_inspected_means_the_search_was_not_whole(show, monkeypatch, address, whole):
    """The browser answers for the page and refuses the frame its own session: a frame of another
    site is then not reached, not searched; one in its parent's process is reached there anyway."""
    frame = f'<iframe src="{address}" style="width:300px;height:100px"></iframe>'
    page = await show(frame, frames={address: html("<button>In it</button>")})
    assert await closed_shadow_any(page) is False
    real = page.context.new_cdp_session

    async def only_the_page(target):
        if target is not page:
            raise RuntimeError("no session")
        return await real(target)
    monkeypatch.setattr(page.context, "new_cdp_session", only_the_page)
    assert await closed_shadow_any(page) is (not whole)


async def test_a_control_in_a_shown_frame_is_found_there(show, rig):
    frame = f'<iframe src="{HOST}/small" style="width:300px;height:100px"></iframe>'
    page = await show(frame + rows(), frames={f"{HOST}/small": html(
        f'{rows(60)}<button onclick="{PRESSED}">In the frame</button>' + OUT)})
    r = rig(page)
    obs = await r.observe("in the frame")
    assert found(obs) == ["In the frame"] and obs.find_whole is True
    assert r.target(by_label(obs, "In the frame").handle).frame_url == f"{HOST}/small"


SECRET = "itsecret-Hunter2Canary"  # gitleaks:allow - synthetic fixture; this test exists to prove masking works


@pytest.mark.parametrize("field", [
    f'<input type="password" value="{SECRET}">',
    f'<input autocomplete="one-time-code" value="{SECRET}">',
    f'<input autocomplete="cc-number" value="{SECRET}">',
    f'<div autocomplete="current-password"><input value="{SECRET}"></div>',
])
async def test_a_secret_fields_value_is_neither_matched_nor_shown(show, rig, field):
    page = await show(rows() + field)
    r = rig(page)
    plain, obs = await r.observe(), await r.observe("Hunter2")
    assert found(obs) == [] and obs.find_whole is True and obs.find_left == 0
    assert SECRET not in json.dumps(obs.to_wire())
    # no oracle: the field did not move up the list for holding the words
    assert [e.label_masked for e in obs.elements] == [e.label_masked for e in plain.elements]


TOKEN = "".join(["ghp_", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"])
WRONG = "".join(["ghp_", "z9Y8x7W6v5U4t3S2r1Q0p9O8n7M6l5K4j3I2"])


def reply(obs) -> str:
    """The whole reply, its random handles aside."""
    wire = obs.to_wire()
    wire.pop("page_handle")
    for element in wire["elements"]:
        element.pop("handle")
    return json.dumps(wire, sort_keys=True)


async def test_a_masked_label_is_not_matched_by_what_masking_hides(show, rig):
    page = await show(rows() + f"<button>Key {TOKEN}</button>")
    r = rig(page)
    obs = await r.observe(TOKEN[10:20])
    assert found(obs) == [] and TOKEN[10:20] not in json.dumps(obs.to_wire())
    assert found(await r.observe("Key")) == ["Key [masked]"]


# The guess is a substring of the token on one page and not on the other, which differ in nothing
# else: a character-by-character search of a hidden token would read the difference.
@pytest.mark.parametrize("build", [
    # a full list: a match that does not fit would be counted
    lambda t: "".join(f"<button>a1B2 public {n}</button>" for n in range(400)) + f"<button>Key {t}</button>",
    # past the cap: a match would come into the list
    lambda t: rows() + f"<button>Key {t}</button>",
    # as a field's value
    lambda t: rows() + f'<input aria-label="Field" value="{t}">',
    # as an option
    lambda t: rows() + f'<select aria-label="Pick"><option>One</option><option>{t}</option></select>',
    # in an address, whose path masking takes away
    lambda t: rows() + f"<button>https://shop.example/key/{t}</button>",
], ids=["full-list", "past-the-cap", "value", "option", "address"])
async def test_changing_only_a_masked_token_leaves_the_whole_reply_byte_identical(show, rig, build):
    guess = TOKEN[4:8]
    assert guess in TOKEN and guess not in WRONG
    replies = []
    for token in (TOKEN, WRONG):
        page = await show(build(token))
        replies.append(reply(await rig(page).observe(guess)))
    assert replies[0] == replies[1]


class Watched:
    """A real frame, keeping every argument its page script receives."""

    def __init__(self, frame, between=None):
        self.frame, self.args, self.between = frame, [], between

    async def evaluate_handle(self, script, arg=None):
        if self.args and self.between:
            await self.between()
        self.args.append(arg)
        return await self.frame.evaluate_handle(script, arg)


async def test_the_words_never_enter_the_page(show):
    page = await show(LONG)
    words = "Publish"
    watched = Watched(page.main_frame)
    got = await extract(watched, words)
    assert [e["name"] for e in got.elements if e.get("found")] == ["Publish"]
    assert len(watched.args) == 2 and watched.args[0] == {"searchable": True}
    assert words.casefold() not in json.dumps(watched.args).casefold()
    # the second holds ordinals and nothing else
    assert set(watched.args[1]) == {"found", "options", "near"}
    assert all(type(n) is int for n in watched.args[1]["found"] + watched.args[1]["near"])
    assert words not in EXTRACT


async def test_a_page_that_changes_between_the_readings_gets_a_stale_list_and_no_failure(show):
    page = await show(LONG)

    async def shrink():
        await page.evaluate("() => { document.body.innerHTML = '<button>Only this</button>'; }")
    got = await extract(Watched(page.main_frame, shrink), "Publish")
    assert [e["name"] for e in got.elements] == ["Only this"] and got.found_left == 0


async def test_a_secret_selects_options_take_no_room_and_are_not_counted(show, rig):
    options = "".join(f"<option>Code {n}</option>" for n in range(450))
    page = await show(f'<button>Go</button><label>Code <select autocomplete="one-time-code">{options}</select></label>')
    obs = await rig(page).observe()
    assert [e.label_masked for e in obs.elements] == ["Go", "Code"] and obs.controls_left == 0


@pytest.mark.parametrize("hint", ["one-time-code", "cc-exp-month"])
async def test_a_secret_selects_options_are_never_matched_or_shown(show, rig, hint):
    secret = "Secr3t-OTP-7391"  # gitleaks:allow - synthetic fixture; this test exists to prove masking works
    field = (f'<label>Code <select autocomplete="{hint}"><option>Other</option>'
             f"<option>{secret}</option></select></label>")
    for body in (field, rows() + field):
        page = await show(body)
        r = rig(page)
        for term in (secret, "Other", None):
            obs = await r.observe(term)
            assert found(obs) == [] and obs.find_left == 0 and secret not in json.dumps(obs.to_wire())
            assert "option" not in [e.role for e in obs.elements if e.label_masked in ("Other", secret)]
        plain = await r.observe()
        assert body is not field or plain.controls_left == 0
        # no oracle: guessing its option moves nothing up the list
        assert ([e.label_masked for e in (await r.observe(secret)).elements]
                == [e.label_masked for e in plain.elements])
        # its own name is what any field's is, and the count holds only what could be listed
        assert found(await r.observe("Code")) == ["Code"]
    assert (await r.observe()).controls_left + MAX_ELEMENTS == 240 * 9 + 1


# -- the check's reading ---------------------------------------------------------------------------

async def test_a_search_leaves_the_checks_reading_as_it_is(show):
    """``all_controls``' digest and the list of ``READ``: same before and after a search, and
    ``READ`` never ranks by a term, so a search cannot make a final read differ."""
    page = await show(LONG)
    ops = gate.PageOps()

    async def whole():
        return json.loads(json.dumps((await ops.read_page(page, None, policy=POLICY)).data))
    before = await whole()
    assert await extract(page.main_frame, "Publish")
    after = await whole()
    assert len(before["all_controls"]) == 1 and before["all_controls"] == after["all_controls"]
    assert before["controls"] == after["controls"] and gate.same_reading(before, after)
    assert "Publish" not in [c["name"] for c in after["controls"]]
    assert "(MAX_EL, (e) => {" in gate.READ and "}, ahead)" not in gate.READ


async def test_a_control_found_goes_through_the_checks_reading_like_any_other(show, rig):
    """The reading finds the target past its own list, with the facts of any other control."""
    page = await show(LONG)
    r = rig(page)
    obs = await r.observe("Publish")
    reading = await gate.PageOps().read_page(page, r.target(by_label(obs, "Publish").handle).element,
                                             policy=POLICY)
    assert reading.data["target"]["name"] == "Publish" and reading.data["target"]["connected"] is True
    assert reading.data["target_idx"] == -1


# -- scrolling ---------------------------------------------------------------------------------

def scrolling():
    return SimpleNamespace(scroll_anchor=None)


async def test_a_repeated_scroll_advances_and_says_where_it_is(show, rig):
    page = await show(LONG)
    r = rig(page)
    obs = await r.observe()
    handle = obs.elements[0].handle                  # in view, so the first scroll wheels too
    active, tops = scrolling(), []
    end = await page.evaluate("() => document.documentElement.scrollHeight - innerHeight")
    for _ in range(6):
        where = await r.scroll(handle, active)
        assert where["moved"] is True and where["end"] == end
        assert where["top"] == await page.evaluate("() => Math.round(scrollY)")
        tops.append(where["top"])
    assert tops == sorted(set(tops)) and tops[-1] - tops[0] > 5 * 720 * 0.8, tops


async def test_one_small_step_is_said_to_have_moved(show, rig):
    """The wheel's own animation must not make a real step read as standing still."""
    page = await show(LONG)
    r = rig(page)
    obs = await r.observe()
    for _ in range(5):
        where = await r.scroll(obs.page_handle, scrolling(), amount="step")
        assert where["moved"] is True and where["top"] == await page.evaluate("() => Math.round(scrollY)")


async def test_the_first_scroll_to_a_handle_brings_it_into_view_and_the_next_goes_on(show, rig):
    page = await show(LONG)
    r = rig(page)
    obs = await r.observe("Publish")
    far = by_label(obs, "Publish").handle
    assert not await in_view(page, "#go")
    active = scrolling()
    # up: a wheel up would take it straight back out of view; the first step only brings it in
    first = await r.scroll(far, active, direction="up")
    assert first["moved"] is True and await in_view(page, "#go")
    assert first["top"] == await page.evaluate("() => Math.round(scrollY)") > 5000
    second = await r.scroll(far, active, direction="up")
    third = await r.scroll(far, active, direction="up")
    assert third["top"] < second["top"] < first["top"]
    assert not await in_view(page, "#go")            # it stays behind: no jump back to it


async def test_another_handle_or_another_step_goes_to_its_element_again(show, rig):
    page = await show(LONG)
    r = rig(page)
    obs = await r.observe("Publish")
    far, near = by_label(obs, "Publish").handle, obs.elements[0].handle
    active = scrolling()
    await r.scroll(far, active)
    assert await in_view(page, "#go")
    position = await r.scroll(near, active)
    assert position["top"] < 720 and position["moved"] is True
    assert not await in_view(page, "#go")


async def test_a_scroll_at_the_end_of_the_page_says_it_did_not_move(show, rig):
    page = await show(LONG)
    r = rig(page)
    obs = await r.observe()
    await page.evaluate("() => scrollTo(0, document.documentElement.scrollHeight)")
    where = await r.scroll(obs.page_handle, scrolling())
    assert where["moved"] is False and where["top"] == where["end"] > 5000
    await page.evaluate("() => scrollTo(0, 0)")
    where = await r.scroll(obs.page_handle, scrolling(), direction="up")
    assert where == {"top": 0, "end": where["end"], "moved": False}


async def test_a_page_that_scrolls_inside_a_box_reports_the_box(show, rig):
    """The document itself cannot scroll: "did not move" would be false of what the wheel moved."""
    inner = ('<style>html,body{height:100%;margin:0;overflow:hidden}</style>'
             '<div id="b" style="height:100%;overflow-y:auto">' + rows() + '</div>')
    page = await show(inner)
    r = rig(page)
    obs = await r.observe()
    size = await page.evaluate("() => { const b = document.getElementById('b'); return b.scrollHeight - b.clientHeight; }")
    where = await r.scroll(obs.page_handle, scrolling())
    assert where["moved"] is True and where["end"] == size
    assert where["top"] == await page.evaluate("() => Math.round(document.getElementById('b').scrollTop)")


# -- a dropdown whose list was cut short ---------------------------------------------------------

async def test_a_dropdown_whose_options_did_not_all_fit_says_how_many_are_listed(show, rig):
    places = "".join(f"<option>Place {n}</option>" for n in range(500))
    page = await show(f'<select aria-label="Where">{places}</select>' + rows())
    obs = await rig(page).observe()
    where = next(e for e in obs.elements if e.label_masked.startswith("Where"))
    listed = sum(1 for e in obs.elements if e.role == "option" and e.label_masked.startswith("Place "))
    assert 0 < listed < 500
    assert where.label_masked == f"Where ({listed} of 500 options listed)"
    whole = next(e for e in obs.elements if e.label_masked == "Who on day 0")   # all 6 listed
    assert whole.role == "combobox"


async def _choose(page, rig, select_label, option_handle):
    """The controller's own ``_own_choice`` and the real ``PageOps`` over the real page: what the
    step would select, or None when it is refused."""
    r = rig(page)
    ops = gate.PageOps()
    ctl = SimpleNamespace(ops=ops)
    obs = await r.observe()
    select = r.target(next(e.handle for e in obs.elements if e.label_masked == select_label))
    choice = await RunController._own_choice(ctl, select, r.target(option_handle(obs)))
    facts = await ops.option_facts(select.element, choice.element)
    if not facts["belongs"]:
        return None
    assert await ops.select_one(select.element, choice.element, facts["label"], facts["value"]) == "ok"
    return facts


@pytest.mark.parametrize("own,chosen", [
    ("<option>Travel</option><option value='m'>Meals</option><option value='p'>Parking</option>", "p"),
    ("<option>Travel</option><option>Meals</option>", None),
    ("<option>Parking</option><option>Travel</option><option> Parking </option>", None)],
    ids=["one_namesake", "none", "two"])
async def test_an_option_of_another_row_chooses_its_namesake_in_the_named_dropdown(show, rig, own, chosen):
    page = await show('<select aria-label="Row"><option>Travel</option><option value="rp">Parking</option></select>'
                      f'<select aria-label="New">{own}</select>')

    def row_parking(obs):
        labels = [e.label_masked for e in obs.elements]
        return obs.elements[labels.index("Row") + 2].handle            # the row's own "Parking"
    facts = await _choose(page, rig, "New", row_parking)
    values = await page.evaluate("() => [...document.querySelectorAll('select')].map(s => s.value)")
    if chosen is None:
        assert facts is None and values[0] == "Travel"
    else:
        assert facts == {"belongs": True, "label": "Parking", "value": chosen}
        assert values == ["Travel", chosen]


async def test_an_option_is_matched_by_the_name_it_shows(show, rig):
    """An option's label attribute is the name the dropdown shows and the observation lists: the
    match, the option's facts and the selection all go by it, never by the option's own text."""
    page = await show('<select aria-label="Row"><option>Travel</option>'
                      '<option label="Parking" value="rp">internal</option></select>'
                      '<select aria-label="New"><option>Travel</option>'
                      '<option label="Meals" value="m">internal</option>'
                      '<option label="Parking" value="p">other</option></select>')

    def row_parking(obs):
        labels = [e.label_masked for e in obs.elements]
        return obs.elements[labels.index("Row") + 2].handle
    facts = await _choose(page, rig, "New", row_parking)
    assert facts == {"belongs": True, "label": "Parking", "value": "p"}
    assert await page.evaluate("() => [...document.querySelectorAll('select')].map(s => s.value)") == ["Travel", "p"]
