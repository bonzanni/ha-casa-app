"""The final read past the list's cap, on real pages: the gate's own ``READ`` and frame assembly in
real Chromium, compared by the gate's own functions. No fakes; the pages are local, served by the
browser context's router, so nothing leaves the machine. The whole path through the production
controller is in ``test_action_gate.py``.

Runs in the image like the other ``docker`` tests, and on a developer's machine by naming this
file (it needs only Patchright's Chromium)."""
from __future__ import annotations

import json
import os

import pytest
from patchright.async_api import async_playwright

from casa_browser import gate
from casa_browser.policy import RunPolicy, SessionRef, SiteGrant
from casa_browser.protocol import Site

pytestmark = pytest.mark.docker

HOST = "https://shop.example"
OTHER = "https://elsewhere.example"
SITE = Site("s", "Shop", (HOST,), (), None, (), None)
POLICY = RunPolicy("run", "account", (SiteGrant("s", "account"),), (SITE,), None, None, 1,
                   SessionRef("account", "s", 1))
OPS = gate.PageOps()
IMAGE_CHROME = "/opt/google/chrome/chrome"
MAX_EL = 400


def filler(count: int = 420) -> str:
    return "".join(f'<button class="fill">Item {n}</button><br>' for n in range(count))


def html(body: str, head: str = "") -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>T</title>{head}</head>'
            f"<body>{body}</body></html>")


@pytest.fixture
async def browser_page():
    async with async_playwright() as p:
        kw = {"executable_path": IMAGE_CHROME} if os.path.exists(IMAGE_CHROME) else {}
        browser = await p.chromium.launch(headless=True, **kw)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        pages = {}

        async def serve(route):
            await route.fulfill(status=200, content_type="text/html",
                                body=pages.get(route.request.url, html("<p>elsewhere</p>")))
        await context.route("**/*", serve)
        page = await context.new_page()

        async def show(body: str, *, head: str = "", frames: dict | None = None):
            pages.update(frames or {})
            pages[f"{HOST}/"] = html(body, head)
            await page.goto(f"{HOST}/", wait_until="load")
            return page
        yield show
        await browser.close()


async def whole(page, target=None) -> dict:
    """A whole page's reading, as the controller takes it, as JSON would keep it."""
    return json.loads(json.dumps((await OPS.read_page(page, target, policy=POLICY)).data))


def listed(reading: dict) -> list:
    return [c["name"] for c in reading["controls"]]


def copied(reading: dict) -> dict:
    return json.loads(json.dumps(reading))


# -- a stable long page ------------------------------------------------------------------------

async def test_repeat_read_and_scroll_keep_a_stable_long_page_equal(browser_page):
    page = await browser_page(filler())
    a, b = await whole(page), await whole(page)
    await page.evaluate("() => scrollTo(0, document.body.scrollHeight)")
    c = await whole(page)
    assert len(a["controls"]) == len(c["controls"]) == MAX_EL
    assert a["complete"] is False and "blind" not in a
    assert len(a["all_controls"]) == 1
    assert a["all_controls"] == b["all_controls"] == c["all_controls"]
    # the list really shifted: "Item 419" is listed only once the page is scrolled
    assert "Item 419" not in listed(a) and "Item 419" in listed(c)
    assert gate.same_reading(a, b) and gate.same_reading(a, c)
    assert gate.difference(a, c) == {}


@pytest.mark.parametrize("count,capped", [(MAX_EL, False), (MAX_EL + 1, True)])
async def test_the_digest_starts_where_the_controls_stop_fitting(browser_page, count, capped):
    page = await browser_page(filler(count))
    a = await whole(page)
    assert ("all_controls" in a) is capped and a["complete"] is (not capped)
    assert len(a["controls"]) == MAX_EL


async def test_a_select_that_changes_who_fits_does_not_change_the_digest(browser_page):
    """The room a long select takes depends on what is in view, so scrolling can leave a semantic
    control out or not. The digest does not follow it."""
    options = "".join(f"<option>Choice {n}</option>" for n in range(200))
    page = await browser_page(filler(350) + f'<select id="s">{options}</select>')
    a = await whole(page)
    await page.evaluate("() => scrollTo(0, document.body.scrollHeight)")
    b = await whole(page)
    assert (a["complete"], b["complete"]) == (True, False)     # the cap's answer flipped
    assert len(a["all_controls"]) == 1 and a["all_controls"] == b["all_controls"]
    assert gate.same_reading(a, b)


# -- what the digest binds ---------------------------------------------------------------------

BEYOND = """<a id="l" href="/one">A link</a>
<select id="s"><option>One</option><option>Two</option></select>
<button id="d">Later</button>
<input id="cb" type="checkbox">
<input id="ty" value="x">
<button id="tg">Tagged</button>
<div id="ro" role="button">Roled</div>
<a id="wrap" href="/in" style="display:contents"><button>Inside a link</button></a>
<form id="f1" action="/a" method="post"><button>Send it</button></form>
<form id="f2" action="/a" method="post"><input id="t2" name="q"></form>
<div id="tb" tabindex="0" aria-checked="false">A row</div>
<div id="oc" onclick="void 0" aria-pressed="false">A tile</div>"""
BY_ID = "document.getElementById"
CHANGES = {
    "href": f"{BY_ID}('l').href = '/two'",
    "select_value": f"{BY_ID}('s').value = 'Two'",
    "disabled": f"{BY_ID}('d').disabled = true",
    "checked": f"{BY_ID}('cb').checked = true",
    "input_type": f"{BY_ID}('ty').type = 'tel'",
    "tag": f"{BY_ID}('tg').outerHTML = '<div role=button>Tagged</div>'",
    "role": f"{BY_ID}('ro').setAttribute('role', 'link')",
    "name": f"{BY_ID}('d').textContent = 'Sooner'",
    "activator_href": f"{BY_ID}('wrap').href = '/out'",
    "submit_action": f"{BY_ID}('f1').action = '/b'",
    "submit_method": f"{BY_ID}('f1').method = 'get'",
    "submit_target": f"{BY_ID}('f1').target = '_blank'",
    "enter_action": f"{BY_ID}('f2').action = '/b'",
    "enter_method": f"{BY_ID}('f2').method = 'get'",
    "enter_target": f"{BY_ID}('f2').target = '_blank'",
    "tabindex_state": f"{BY_ID}('tb').setAttribute('aria-checked', 'true')",
    "onclick_state": f"{BY_ID}('oc').setAttribute('aria-pressed', 'true')",
    "order": f"{BY_ID}('d').before({BY_ID}('tb'))",
}


@pytest.mark.parametrize("change", sorted(CHANGES))
async def test_a_change_in_an_unlisted_control_changes_the_digest(browser_page, change):
    page = await browser_page(filler() + BEYOND)
    a = await whole(page)
    assert not {"A link", "Later", "Tagged", "Roled", "A row", "A tile"} & set(listed(a))
    await page.evaluate("() => { " + CHANGES[change] + "; }")
    b = await whole(page)
    assert len(a["all_controls"]) == len(b["all_controls"]) == 1
    assert a["all_controls"] != b["all_controls"]
    assert not gate.same_reading(a, b)


@pytest.mark.parametrize("change", ["href", "select_value", "disabled"])
async def test_an_unlisted_change_tells_the_check_and_nothing_else(browser_page, change):
    page = await browser_page(filler() + BEYOND)
    a = await whole(page)
    await page.evaluate("() => { " + CHANGES[change] + "; }")
    assert gate.difference(a, await whole(page)) == {"controls_beyond_list_changed": True}


async def test_a_secret_value_stays_out_of_the_digest(browser_page):
    page = await browser_page(filler() + '<input id="p" type="password" value="itsecret-one">')
    a = await whole(page)
    await page.evaluate(f"() => {{ {BY_ID}('p').value = 'itsecret-two'; }}")
    b = await whole(page)
    assert a["all_controls"] == b["all_controls"]         # a secret's value is [not shown] here
    assert "itsecret" not in json.dumps(a)


# Named only by what the page calls them: a button input's value, aria-label, title, alt, a held
# image's alt. Past the fold, so scrolling is what shows them.
PIC = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")
SIZED = 'style="display:block;width:60px;height:60px"'
NAMED = (f'<input id="v" type="button" value="Named by value">'
         f'<button id="l" aria-label="Named by label" {SIZED}></button>'
         f'<button id="t" title="Named by title" {SIZED}></button>'
         f'<input id="i" type="image" alt="Named by alt" src="/pic.gif" {SIZED}>'
         f'<a id="h" href="/h" {SIZED}><img loading="lazy" alt="Named by a picture" src="/lazy.gif"></a>')


async def test_a_name_the_page_gives_keeps_a_stable_long_page_equal(browser_page):
    """Scrolling loads the lazy picture, which then has a size for the first time: nothing about
    what the control is or does changed, so a Yes must still be able to press."""
    page = await browser_page("")

    async def picture(route):
        await route.fulfill(status=200, content_type="image/gif", body=PIC)
    await page.route("**/*.gif", picture)
    await browser_page(filler() + NAMED)
    a = await whole(page)
    assert await page.evaluate("() => document.querySelector('#h img').width") == 0
    await page.evaluate("() => scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_function("() => document.querySelector('#h img').width > 0")
    b = await whole(page)
    assert {"Named by value", "Named by label", "Named by title", "Named by alt",
            "Named by a picture"} <= set(listed(b))
    assert a["all_controls"] == b["all_controls"]
    assert gate.same_reading(a, b)


@pytest.mark.parametrize("change", [
    "document.getElementById('v').value = 'Other'",
    "document.getElementById('l').setAttribute('aria-label', 'Other')",
    "document.getElementById('t').title = 'Other'",
    "document.getElementById('i').alt = 'Other'",
    "document.querySelector('#h img').alt = 'Other'",
])
async def test_a_changed_name_the_page_gives_changes_the_digest(browser_page, change):
    page = await browser_page(filler() + NAMED)
    a = await whole(page)
    await page.evaluate("() => { " + change + "; }")
    assert a["all_controls"] != (await whole(page))["all_controls"]


async def test_no_secret_or_field_value_names_a_control_in_the_digest(browser_page):
    """A password field's own attributes, and a button holding one, name nothing, so changing them
    changes nothing; an ordinary field's value is its state and never its name."""
    page = await browser_page(filler()
                              + '<input id="p" type="password" aria-label="itsecret-one" value="itsecret-one">'
                                '<button id="b" aria-label="itsecret-one"><input type="password"></button>'
                                '<input id="f" value="itvalue-one">')
    a = await whole(page)
    await page.evaluate("""() => { document.getElementById('p').setAttribute('aria-label', 'itsecret-two');
      document.getElementById('p').value = 'itsecret-two';
      document.getElementById('b').setAttribute('aria-label', 'itsecret-two'); }""")
    b = await whole(page)
    assert a["all_controls"] == b["all_controls"]
    await page.evaluate("() => scrollTo(0, document.body.scrollHeight)")
    c = await whole(page)
    assert "itsecret" not in json.dumps(c) and "itvalue-one" not in listed(c)


async def test_the_digest_is_salted(browser_page, monkeypatch):
    page = await browser_page(filler())
    a = await whole(page)
    monkeypatch.setattr(gate, "_SALT", "another-salt")
    assert a["all_controls"] != (await whole(page))["all_controls"]


# -- a button moved to another row -------------------------------------------------------------

ROWS = ('<table><tr><td>Alice</td><td><button class="rm">Remove</button></td></tr>'
        '<tr><td>Bob</td><td><button class="rm">Remove</button></td></tr></table>')
SWAP = """() => { const [x, y] = document.querySelectorAll('.rm');
  const px = x.parentNode, py = y.parentNode; px.append(y); py.append(x); }"""


@pytest.mark.parametrize("long", [True, False])
async def test_a_button_node_moved_to_another_row_is_not_the_same_reading(browser_page, long):
    page = await browser_page(ROWS + (filler() if long else ""))
    target = await page.query_selector(".rm")
    a = await whole(page, target)
    assert gate.target_record(a) == ["Alice", "Remove"]
    assert ("all_controls" in a) is long
    await page.evaluate(SWAP)
    b = await whole(page, target)
    assert gate.target_record(b) == ["Bob", "Remove"]
    # All else the comparison holds is equal, so the record alone answers.
    assert gate.target_facts(a["target"]) == gate.target_facts(b["target"])
    assert [x["text"] for x in a["blocks"]] == [x["text"] for x in b["blocks"]]
    assert gate._submissions(a) == gate._submissions(b)
    assert a.get("all_controls") == b.get("all_controls")
    assert not gate.same_reading(a, b)
    assert gate.difference(a, b) == {"target_record_changed": {"before": ["Alice", "Remove"],
                                                               "after": ["Bob", "Remove"]}}
    assert gate.same_reading(a, copied(a))


# -- a selection held in tabindex rows ---------------------------------------------------------

CHOICE = ('<div id="a" tabindex="0" aria-checked="true">Alice</div>'
          '<div id="b" tabindex="0" aria-checked="false">Bob</div>'
          '<button id="go">Remove selected</button>')
MOVE_CHOICE = """() => { document.getElementById('a').setAttribute('aria-checked', 'false');
  document.getElementById('b').setAttribute('aria-checked', 'true'); }"""


@pytest.mark.parametrize("rows_listed", [True, False])
async def test_a_selection_held_in_tabindex_rows_is_seen(browser_page, rows_listed):
    page = await browser_page(CHOICE + filler() if rows_listed else filler() + CHOICE)
    a = await whole(page)
    assert ("Alice" in listed(a)) is rows_listed
    await page.evaluate(MOVE_CHOICE)
    b = await whole(page)
    assert a["all_controls"] != b["all_controls"]
    assert not gate.same_reading(a, b)
    d = gate.difference(a, b)
    assert d["controls_beyond_list_changed"] is True
    assert len(d.get("controls_changed", [])) == (2 if rows_listed else 0)
    assert "controls_added" not in d and "controls_removed" not in d


# -- frames ------------------------------------------------------------------------------------

SETTING = '<label><input id="c" type="checkbox"> Share profile publicly</label>'
SHOWN_FRAME = f'<iframe src="{HOST}/small" style="width:300px;height:100px"></iframe>'


async def test_a_checkbox_in_a_small_shown_frame_beside_a_capped_parent_is_seen(browser_page):
    page = await browser_page(SHOWN_FRAME + '<button>Save preferences</button>' + filler(),
                              frames={f"{HOST}/small": html(SETTING)})
    a = await whole(page)
    assert len(a["controls"]) == MAX_EL + 1 and len(a["all_controls"]) == 2
    assert gate.same_reading(a, await whole(page))
    await page.frames[1].evaluate("() => { document.getElementById('c').checked = true; }")
    b = await whole(page)
    assert a["all_controls"][0] == b["all_controls"][0]      # the capped parent is unchanged
    assert a["all_controls"][1] != b["all_controls"][1]
    assert not gate.same_reading(a, b)
    d = gate.difference(a, b)
    assert d["controls_beyond_list_changed"] is True and len(d["controls_changed"]) == 1
    assert '"checked": true' in d["controls_changed"][0]["after"]


async def test_a_capped_child_frame_beside_a_small_parent_is_bound_too(browser_page):
    page = await browser_page('<button id="own">Own</button>' + SHOWN_FRAME,
                              frames={f"{HOST}/small": html(filler())})
    a = await whole(page)
    assert len(a["all_controls"]) == 2
    await page.evaluate(f"() => {{ {BY_ID}('own').disabled = true; }}")
    b = await whole(page)
    assert a["all_controls"][0] != b["all_controls"][0]
    assert a["all_controls"][1] == b["all_controls"][1]


async def test_a_target_in_a_child_frame_keeps_its_record_and_the_frames_order(browser_page):
    """The target's frame is the reading's primary one: its record ids are its own, unshifted, and
    the other frames' never collide with them; ``all_controls`` stays in frame order."""
    row = '<ul><li>{name} <button class="rm">Remove</button></li></ul>'
    page = await browser_page(row.format(name="Parent row") + SHOWN_FRAME + filler(),
                              frames={f"{HOST}/small": html(row.format(name="Frame row"))})
    frame = page.frames[1]
    target = await frame.query_selector(".rm")
    plain = await whole(page)
    a = await whole(page, target)
    assert a["target"]["name"] == "Remove" and a["target_idx"] == 0
    assert a["href"] == f"{HOST}/small"                      # the primary reading is the frame's
    assert gate.target_record(a) == ["Frame row Remove"]
    assert a["all_controls"] == plain["all_controls"] and len(a["all_controls"]) == 2
    assert gate.same_reading(a, await whole(page, target))


async def test_a_withheld_frame_stays_under_frames_withheld(browser_page):
    frame = f'<iframe src="{OTHER}/x" style="width:300px;height:100px"></iframe>'
    page = await browser_page(frame + filler(),
                              frames={f"{OTHER}/x": html('<button id="w">Not ours</button>')})
    a = await whole(page)
    assert len(a["all_controls"]) == 1 and len(a["frames_withheld"]) == 1
    assert gate.same_reading(a, await whole(page))
    await page.frames[1].evaluate("() => { document.getElementById('w').disabled = true; }")
    b = await whole(page)
    assert a["all_controls"] == b["all_controls"]
    assert a["frames_withheld"] != b["frames_withheld"] and not gate.same_reading(a, b)
    assert gate.difference(a, b) == {"frames_not_shown_changed": [1]}


async def test_a_capped_withheld_frame_alone_is_not_blind(browser_page):
    frame = f'<iframe src="{OTHER}/x" style="width:300px;height:100px"></iframe>'
    page = await browser_page('<button>Own</button>' + frame,
                              frames={f"{OTHER}/x": html(filler())})
    a = await whole(page)
    assert a["complete"] is False and "blind" not in a
    assert len(a["all_controls"]) == 1 and len(a["frames_withheld"]) == 1
    assert gate.same_reading(a, await whole(page))


# -- blind readings ----------------------------------------------------------------------------

CLOSED_FIELD = """<div id="h"></div><script>
  document.getElementById('h').attachShadow({mode: 'closed'}).innerHTML = '<input>';</script>"""
ROOTS = """<div id="r"></div><script>
  const r = document.getElementById('r');
  for (let i = 0; i < 5000; i++) r.appendChild(document.createElement('div')).attachShadow({mode: 'open'});
</script>"""
BLIND = {"closed_shadow_field": CLOSED_FIELD,
         "block_over_the_limit": "<p>" + "word " * 41_000 + "</p>",
         "too_many_blocks": "<p>x</p>" * 20_001,
         "too_many_shadow_roots": ROOTS}


@pytest.mark.parametrize("cause", sorted(BLIND))
async def test_a_blind_reading_has_no_final_read(browser_page, cause):
    page = await browser_page(BLIND[cause])
    a = await whole(page)
    assert a["blind"] is True and a["complete"] is False
    assert gate.blind(a) and not gate.same_reading(a, copied(a))


async def test_a_blind_capped_reading_is_blind_with_its_digest(browser_page):
    page = await browser_page(CLOSED_FIELD + filler())
    a = await whole(page)
    assert a["blind"] is True and len(a["all_controls"]) == 1
    assert gate.blind(a) and not gate.same_reading(a, copied(a))


async def test_a_blind_child_frame_makes_the_page_blind(browser_page):
    page = await browser_page(SHOWN_FRAME + filler(),
                              frames={f"{HOST}/small": html(BLIND["block_over_the_limit"])})
    a = await whole(page)
    assert a["blind"] is True and gate.blind(a)


# -- a page that fits --------------------------------------------------------------------------

NORMAL = ('<table><tr><td>Alice</td><td><button id="x" class="rm">Remove</button></td></tr></table>'
          '<p>Total 10</p><a href="/more">More</a><select id="s"><option>One</option>'
          '<option>Two</option></select>')
READING_KEYS = {"href", "origin", "title", "complete", "blocks", "controls", "target",
                "target_idx", "has_cc"}


async def test_a_page_that_fits_is_read_and_compared_as_before(browser_page):
    page = await browser_page(NORMAL)
    target = await page.query_selector("#x")
    a = await whole(page, target)
    assert set(a) == READING_KEYS and a["complete"] is True
    assert not gate.blind(a)
    b = await whole(page, target)
    assert gate.same_reading(a, b) and gate._same_view(a, b) and gate.difference(a, b) == {}
    await page.evaluate(f"() => {{ {BY_ID}('s').value = 'Two'; }}")
    c = await whole(page, target)
    assert not gate.same_reading(a, c) and not gate._same_view(a, c)
    assert list(gate.difference(a, c)) == ["controls_changed"]


# -- the pointer -------------------------------------------------------------------------------

# The row inherits the pointer from what holds the target: it has its own text, so it is listed once
# hovered, and what is inside it is then part of it.
HOVERED = ('<style>.wrap:hover { cursor: pointer; }</style><div class="wrap"><button id="x">Do it'
           '</button><div>Row text <span tabindex="0">Inner</span></div></div>')


async def test_moving_the_pointer_to_the_target_does_not_change_the_digest(browser_page):
    """A ``:hover`` cursor rule adds a control to the list as the pointer arrives, and drops what
    it now holds. Neither reaches the digest, or a Yes could never press there."""
    page = await browser_page(HOVERED + filler())
    target = await page.query_selector("#x")
    a = await whole(page, target)
    assert "Inner" in listed(a) and not any(c["name"].startswith("Row text") for c in a["controls"])
    await target.hover()
    b = await whole(page, target)
    assert any(c["name"].startswith("Row text") for c in b["controls"])    # the rule did fire
    assert "Inner" not in listed(b)
    assert a["all_controls"] == b["all_controls"]
    assert gate.same_reading(a, b)
    assert gate.difference(a, b) == {}
