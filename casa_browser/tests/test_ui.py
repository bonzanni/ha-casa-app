"""Task 13: the ingress pages — every mutation's check, home, pairing, screenshots, the Done
screen, setup — on the real controller over Task 12's doubles."""
from __future__ import annotations

import json
import logging
import re
import sqlite3

import pytest

from casa_browser.ui import Nonces, make_ui
from controllerfakes import spec
from uihelpers import PREFIX, Operator, operator, rig, waiting_run  # noqa: F401


def post_routes(app) -> list[str]:
    out = []
    for route in app.router.routes():
        if route.method == "POST":
            out.append(route.resource.canonical)
    return out


def concrete(path: str, ids: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: ids[m.group(1)], path)


async def test_csrf_required_for_every_mutation(rig, operator):
    """Every POST route refuses (403, nothing done) without a nonce, with a forged one, with a
    nonce issued for another form, with one already spent, and from another origin. The route
    list is read from the app, so a new mutation is covered or this test fails."""
    run_id = await waiting_run(rig)
    app = make_ui(rig.ctl)
    routes = post_routes(app)
    expected = {"/runs/{run}/open", "/live/{grant}", "/view/{run}/done", "/sites",
                "/sites/{site}/forget", "/pair"}
    assert set(routes) == expected
    ids = {"run": run_id, "grant": rig.live.grants[run_id].grant_id, "site": "sa"}
    before = (rig.store.get_pairing(), rig.state(run_id))
    refused = 0
    for path in routes:
        target = concrete(path, ids)
        for nonce, origin in [(None, True), ("n-forged", True), ("other", True), ("good", False)]:
            if nonce == "other":
                n = _issue(operator, "/somewhere-else")
            elif nonce == "good":
                n = _issue(operator, target)
            else:
                n = nonce
            headers = {"Origin": "https://evil.example"} if not origin else None
            status, _, _ = await operator.post(target, {"confirm": "yes"}, nonce=n,
                                               headers=headers)
            assert status == 403, (path, nonce, origin, status)
            refused += 1
    assert refused == 4 * len(expected)
    after = (rig.store.get_pairing(), rig.state(run_id))
    assert after == before
    assert not any(e[:2] == ("broker", "forget_site") for e in rig.log)
    # A spent nonce is refused the second time; a cross-site fetch without Origin is refused.
    await operator.get("/setup")
    nonce = operator.nonce("/pair")
    status, _, _ = await operator.post("/pair", nonce=nonce)
    assert status == 303
    status, _, _ = await operator.post("/pair", nonce=nonce)
    assert status == 403
    await operator.get("/setup")
    status, _, _ = await operator.post("/pair", headers={"Sec-Fetch-Site": "cross-site"},
                                       origin=False)
    assert status == 403


def _issue(operator, path):
    from casa_browser.ui import UI_KEY
    return operator.client.server.app[UI_KEY].nonces.issue(path)


def test_nonces_single_use_bound_and_expiring():
    t = [0.0]
    n = Nonces(clock=lambda: t[0])
    a = n.issue("/pair")
    assert not n.take(a, "/sites")[0] and not n.take(a, "/pair")[0]     # bound; and a miss spends it
    b = n.issue("/pair")
    t[0] = 3601
    assert not n.take(b, "/pair")[0]
    c = n.issue("/pair")
    assert n.take(c, "/pair")[0] and not n.take(c, "/pair")[0]


async def test_home_lists_live_waits_first(rig, operator):
    queued = []
    run_id = await waiting_run(rig)
    for i in range(2):
        run, _ = await rig.ctl.start_run(spec(request_id=f"q{i}"))
        queued.append(run.run_id)
    status, html, _ = await operator.get("/")
    assert status == 200
    opens = re.findall(r'action="' + re.escape(PREFIX) + r'/runs/([^/"]+)/open"', html)
    assert opens == [run_id]
    assert html.index("Needs you") < html.index("<h2>Runs</h2>")
    for q in queued:
        assert f"/runs/{q}\"" in html and f"/runs/{q}/open" not in html


async def test_pair_ui_never_shows_secret(rig, operator):
    await operator.get("/setup")
    status, _, resp = await operator.post("/pair")
    assert status == 303
    status, html, _ = await operator.follow(resp)
    assert "first caller within two minutes" in html
    secret = rig.ctl.pairing.claim('<script>alert(1)</script>', "peer-address")
    status, html, _ = await operator.get("/setup")
    state = rig.store.get_pairing()
    assert secret not in html and state.secret_sha256 not in html
    assert "&lt;script&gt;" in html and "<script>alert" not in html
    assert "name reported by the caller" in html and "peer-address" in html
    # a window that closes unclaimed says so, and what pairing needs
    await operator.post("/pair", nonce=operator.nonce("/pair"))
    rig.clock.advance(121)
    status, html, _ = await operator.get("/pair")
    assert "nobody claimed it" in html and "Fido" in html


async def test_screenshot_route_operator_only(rig, operator, tmp_path):
    """A screenshot is served through ingress by its run and id only (and through the trusted
    /v1 route, Task 3); another run's id is refused; no model route carries it."""
    run_id = await waiting_run(rig)
    path = tmp_path / "shot.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    rig.store.put_screenshot("shot1", run_id, str(path))
    status, _, resp = await operator.get(f"/runs/{run_id}/screenshots/shot1")
    assert status == 200 and resp.content_type == "image/png"
    other, _ = await rig.ctl.start_run(spec(request_id="other"))
    status, _, _ = await operator.get(f"/runs/{other.run_id}/screenshots/shot1")
    assert status == 404
    status, _, _ = await operator.get(f"/runs/{run_id}/screenshots/..%2Fstate.db")
    assert status == 404


async def _done(rig, operator, purpose="login"):
    run_id = await waiting_run(rig, purpose=purpose)
    await operator.open_view(run_id)
    status, html, resp = await operator.post(f"/view/{run_id}/done")
    return run_id, status, html, resp


@pytest.mark.parametrize("purpose", ["assist", "login"])
async def test_done_ends_the_live_view_in_one_tap(rig, operator, purpose):
    """Slice 5g (operator 2026-10-31, "Is it necessary?"): Done alone ends the view — the window
    back, the run going on, a closing page that says Fido is continuing; no review, no Finish,
    nothing asked of the broker about what was typed, and no "Did you sign in?" (ruling
    2026-10-31)."""
    run_id, status, html, _ = await _done(rig, operator, purpose)
    assert status == 200 and "Fido is continuing" in html and "Telegram" in html, html
    assert "Did you sign in?" not in html and "/finish" not in html
    assert ("restore", True) in rig.log
    assert rig.state(run_id) == "running"
    # the viewer's own navigation after Done (another device pressed it) lands on the same page
    status, html, _ = await operator.get(f"/view/{run_id}/review")
    assert status == 200 and "Fido is continuing" in html


async def test_ingress_header_required(rig, operator):
    """Without a well-formed X-Ingress-Path (direct access, a forged or odd prefix) nothing is
    served and nothing is done."""
    for value in (None, "", "/api/hassio_ingress/", "/api/hassio_ingress/x/../y",
                  "/elsewhere/abc", "/api/hassio_ingress/a b"):
        headers = {"X-Forwarded-Host": "ha.test"}
        if value is not None:
            headers["X-Ingress-Path"] = value
        resp = await operator.client.get("/", headers=headers)
        assert resp.status == 403, value
        resp = await operator.client.post("/pair", headers=headers, data={"nonce": "x"})
        assert resp.status == 403
    assert rig.store.get_pairing().armed_until is None


async def test_pages_use_the_ingress_prefix_and_headers(rig, operator):
    run_id = await waiting_run(rig)
    status, html, resp = await operator.get("/")
    hrefs = re.findall(r'(?:href|action|src)="([^"]+)"', html)
    assert hrefs and all(h.startswith(PREFIX + "/") for h in hrefs)
    assert resp.headers["Cache-Control"] == "no-store"
    csp = resp.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'self'" in csp and "wss://ha.test" in csp
    status, _, resp = await operator.post(f"/runs/{run_id}/open")
    cookie = operator.cookie_attrs
    assert cookie["path"] == PREFIX + "/" and cookie["httponly"] and cookie["samesite"] == "Strict"
    assert cookie["secure"]
    status, html, _ = await operator.follow(resp)
    assert f'data-prefix="{PREFIX}"' in html and f"{PREFIX}/static/live.js" in html


WARNING = ("The browser's 1Password guest account can edit items. Casa Browser only needs to "
           "read them; consider making it read-only.")


@pytest.mark.parametrize("status_reply, shown, warned", [
    ({"configured": True, "sdk": "ok", "guest_role": "not_readonly", "readonly_users": 0,
      "editable_users": 2}, "can edit items", True),
    ({"configured": True, "sdk": "ok", "guest_role": "readonly", "readonly_users": 1,
      "editable_users": 2}, "read-only", False),
    ({"configured": True, "sdk": "unavailable", "guest_role": "unknown", "readonly_users": 0,
      "editable_users": 0}, "unknown (the credential service did not answer)", False),
    ({"error": "unavailable"}, "unknown (the credential service did not answer)", False),
])
async def test_setup_warns_when_the_guest_can_edit_and_asks_nothing(rig, operator, status_reply,
                                                                  shown, warned):
    """Design §5.2 (slice 7): the guest confirmation is a warning. Setup shows the vault's
    accessor counts and, when no read-only user exists, says the guest can edit items; there
    is nothing to tick and no form."""
    real = rig.broker.call

    async def call(op, **args):
        if op == "status":
            return dict(status_reply)
        return await real(op, **args)
    rig.broker.call = call
    status, html, _ = await operator.get("/setup")
    assert status == 200 and f"Guest account: {shown}" in html, html
    from html import escape
    assert html.count(escape(WARNING)) == (1 if warned else 0), html
    if "readonly_users" in status_reply and "error" not in status_reply:
        assert (f"{status_reply['readonly_users']} read-only, "
                f"{status_reply['editable_users']} able to edit") in html
    assert "can see every item in the browsing vault" in html
    assert 'name="confirm"' not in html and "/setup/guest" not in html


async def test_register_and_forget_from_the_sites_page(rig, operator):
    real = rig.broker.call

    async def call(op, **args):
        if op == "register_item":
            rig.log.append(("broker", "register_item", args["item_id"], tuple(args["login_origins"])))
            return {"site_id": args["site_id"], "roles_available": ["username", "password"],
                    "extra_field_names": ["recovery codes"]}
        return await real(op, **args)
    rig.broker.call = call
    await operator.get("/sites/new")
    status, _, resp = await operator.post("/sites", {
        "display_name": "Shop", "origins": "https://shop.example\n", "login_origins":
        "https://login.shop.example", "vault_item_id": "item_shop", "start_page": "",
        "confirm": "yes"})
    assert status == 303, operator.html
    assert ("broker", "register_item", "item_shop", ("https://login.shop.example",)) in rig.log
    status, html, _ = await operator.follow(resp)
    assert "recovery codes" in html and "whole browsing vault" in html
    site = next(s for s in rig.store.list_sites() if s.display_name == "Shop")
    assert site.origins == ("https://shop.example",) and site.vault_item_id == "item_shop"
    # without the confirmation nothing is registered
    await operator.get("/sites/new")
    status, _, _ = await operator.post("/sites", {"display_name": "Other", "origins":
                                                  "https://other.example"})
    assert status == 400 and not any(s.display_name == "Other" for s in rig.store.list_sites())
    # forget: from the site's own page, asked first (app.js's dialog: data-confirm), through
    # the controller, never a vault call
    status, page, _ = await operator.get(f"/sites/{site.site_id}")
    assert status == 200 and re.search(r'/sites/' + re.escape(site.site_id) + r'/forget"[^>]*data-confirm=', page)
    status, _, _ = await operator.post(f"/sites/{site.site_id}/forget", {"confirm": "yes"})
    assert status == 303
    assert ("forget_profile", site.site_id) in rig.log
    assert any(e[:2] == ("broker", "forget_site") for e in rig.log)


async def test_replaced_pages_done_form_is_refused(rig, operator):
    """Ruling E-T13-2 = V1 + fix (r5 Astra S2): Done's nonce is bound to its page's viewer id;
    after another page became the viewer, the old page's Done form ends nothing."""
    run_id = await waiting_run(rig)
    await operator.open_view(run_id)
    old_nonce = operator.nonce(f"/view/{run_id}/done")
    status, html, _ = await operator.get(f"/view/{run_id}")      # a reload: a new viewer page
    assert status == 200
    current = operator.nonce(f"/view/{run_id}/done", html)
    status, text, _ = await operator.post(f"/view/{run_id}/done", nonce=old_nonce)
    assert status == 403 and "no longer the live view" in text
    assert rig.live.sessions[run_id].phase == "viewing" and ("restore", True) not in rig.log
    status, html, _ = await operator.post(f"/view/{run_id}/done", nonce=current)   # the current page
    assert status == 200 and "Fido is continuing" in html and rig.state(run_id) == "running"


async def test_every_page_states_its_own_referrer_policy(rig, operator):
    """Home Assistant's ingress replaces the app's Referrer-Policy header with no-referrer (a
    browser then posts forms with "Origin: null", which the mutation check refuses): every page
    and the viewer say same-origin themselves, first in their head."""
    head = '<head><meta name="referrer" content="same-origin">'
    run_id = await waiting_run(rig)
    pages = []
    for path in ("/", f"/runs/{run_id}", "/sites", "/setup", "/pair",
                 "/live/unknown-grant"):
        _, html, _ = await operator.get(path)
        pages.append((path, html))
    await operator.open_view(run_id)
    pages.append(("viewer", operator.html))
    status, closing, _ = await operator.post(f"/view/{run_id}/done")
    pages.append(("closing", closing))
    for path, html in pages:
        assert html.startswith("<!doctype html>") and head in html[:200], (path, html[:200])


async def test_times_show_in_the_effective_time_zone_and_runs_name_their_task(rig, operator):
    """casa-test F8: the pairing time and run times are in the effective time zone with its
    name; a run is listed with its task; a live-view wait alone does not read "No runs yet"."""
    from datetime import datetime, timezone
    from casa_browser.ui import _when
    at = datetime(2026, 10, 22, 12, 53, tzinfo=timezone.utc)
    assert _when(at, "Europe/Amsterdam") == "2026-10-22 14:53 CEST"
    assert _when(at, None) == "2026-10-22 12:53 UTC"
    run_id = await waiting_run(rig)
    _, html, _ = await operator.get("/")
    assert "No runs yet." not in html and "No other runs." in html
    assert f"/runs/{run_id}/open" in html
    rig.ctl.pairing.arm()
    rig.ctl.pairing.claim("caller", "peer-address")
    _, html, _ = await operator.get("/setup")
    assert re.search(r", on \d{4}-\d\d-\d\d \d\d:\d\d CES?T", html), html[-600:]
    await rig.ctl.start_run(spec(request_id="q-task", text="Look up the opening hours"))
    _, html, _ = await operator.get("/")
    assert "Look up the opening hours" in html


async def test_addresses_and_codes_copy_with_one_tap(rig, operator):
    """casa-test F17: each address a site registered is shown as a one-tap copy (``code.copy``
    with the exact text to copy), and app.js wires every such code to the clipboard."""
    from pathlib import Path
    import casa_browser
    for site in rig.store.list_sites():
        _, html, _ = await operator.get(f"/sites/{site.site_id}")
        for address in (*site.origins, *site.login_origins):
            assert f'data-copy="{address}"' in html, address
        assert html.count('class="copy"') >= 1
    js = (Path(casa_browser.__file__).parent / "static/app.js").read_text()
    assert "querySelectorAll('code.copy')" in js and "clipboard.writeText" in js
    assert "execCommand('copy')" in js          # plain http (no clipboard API)


async def test_home_reloads_itself_when_a_view_waits(rig, operator):
    """casa-test F40: Home carries a stamp of what changes by itself (a live view waiting, a
    question) and app.js reloads Home when /home/stamp differs; the stamp changes when a view
    starts waiting and holds nothing of the runs but a hash."""
    import json as _json
    from conftest import APP_DIR
    _, page, _ = await operator.get("/")
    before = re.search(r'data-stamp="([0-9a-f]{16})"', page).group(1)
    _, stamp, _ = await operator.get("/home/stamp")
    assert _json.loads(stamp) == {"stamp": before}
    run_id = await waiting_run(rig)
    after = _json.loads((await operator.get("/home/stamp"))[1])["stamp"]
    assert after != before and run_id not in after
    _, page, _ = await operator.get("/")
    assert f'data-stamp="{after}"' in page
    # one question answered and another asked between two looks: the stamp changes (Terra K r6)
    from types import SimpleNamespace
    assert rig.store.transition(run_id, "waiting_live", "waiting_approval")
    real = rig.store.open_question
    stamps = []
    for qid, revision in (("q-one", 1), ("q-two", 1), ("q-two", 2)):
        rig.store.open_question = lambda rid, q=SimpleNamespace(
            question_id=qid, revision=revision, state="open", view={"text": "Pay?"}): q
        stamps.append(_json.loads((await operator.get("/home/stamp"))[1])["stamp"])
    rig.store.open_question = real
    assert len(set(stamps)) == 3 and after not in stamps and "q-one" not in "".join(stamps), stamps
    js = (APP_DIR / "rootfs/opt/casa_browser/static/app.js").read_text()
    assert "location.reload()" in js and "leaving" in js     # never over a form being sent


# -- slice 5f: the question's live view opens from the app's page ------------------------------

def _question(run_id, kind=None):
    from types import SimpleNamespace
    return SimpleNamespace(question_id="q1", run_id=run_id, revision=3, digest="d" * 16,
                           state="open", view={"kind": kind} if kind else {"text": "Pay?"})


async def _asking_run(rig, monkeypatch, kind=None):
    run = await rig.run()
    assert rig.store.transition(run.run_id, "running", "waiting_approval")
    q = _question(run.run_id, kind)
    monkeypatch.setattr(rig.store, "open_question", lambda rid: q if rid == run.run_id else None)
    return run.run_id, q


async def test_home_offers_the_live_view_of_a_waiting_question(rig, operator, monkeypatch):
    """[Open live view] on the card is a link to this page; the page offers Open for the run
    whose question waits, and Open asks the question's own live view, then opens it."""
    run_id, q = await _asking_run(rig, monkeypatch)
    status, html, _ = await operator.get("/")
    assert re.findall(r'action="' + re.escape(PREFIX) + r'/runs/([^/"]+)/open"', html) == [run_id]
    asked, opened = [], []

    async def live_from_question(rid, qid, body):
        asked.append((rid, qid, body))
        return {"outcome": "live"}

    async def open_from_home(rid):
        opened.append(rid)
        return "tok"
    monkeypatch.setattr(rig.ctl, "live_from_question", live_from_question)
    monkeypatch.setattr(rig.live, "open_from_home", open_from_home)
    status, _, resp = await operator.post(f"/runs/{run_id}/open")
    assert status == 303 and resp.headers["Location"].endswith(f"/view/{run_id}")
    ((rid, qid, body),) = asked
    assert (rid, qid, body["revision"], body["digest"]) == (run_id, "q1", 3, "d" * 16)
    assert body["operator_ref"].startswith("ingress:") and 1 <= body["n"] < 2**31
    assert opened == [run_id]


async def test_a_login_question_offers_no_live_view(rig, operator, monkeypatch):
    run_id, _ = await _asking_run(rig, monkeypatch, kind="login_item")
    _, html, _ = await operator.get("/")
    assert f"/runs/{run_id}/open" not in html


async def test_a_question_no_longer_current_opens_nothing(rig, operator, monkeypatch):
    run_id, _ = await _asking_run(rig, monkeypatch)

    async def live_from_question(rid, qid, body):
        return {"outcome": "not_current", "generation": 0}
    monkeypatch.setattr(rig.ctl, "live_from_question", live_from_question)
    await operator.get("/")
    status, _, _ = await operator.post(f"/runs/{run_id}/open")
    assert status == 409


async def test_open_says_it_is_opening_and_presses_once(rig, operator):
    """Both Open forms name what the button says while the view opens (app.js disables it on
    submit, so a second tap cannot spend the form)."""
    run_id = await waiting_run(rig)
    _status, html, _ = await operator.get("/")
    assert re.search(r'/runs/' + re.escape(run_id) + r'/open".*?<button class="primary s" data-busy="Opening…">',
                     html, re.S)
    grant = rig.live.grants[run_id].grant_id
    _status, html, _ = await operator.get(f"/live/{grant}")
    assert '<button class="primary s" data-busy="Opening…">' in html


async def test_pages_carry_no_inline_style_and_name_their_tab(rig, operator):
    """The CSP admits only this app's stylesheet ('self', no 'unsafe-inline'): a style="…"
    attribute on a page would be dropped by the browser, so the look lives in style.css alone.
    Every page loads theme.js (Home Assistant's resolved theme) before it paints, and marks
    the tab it belongs to; a site's page and the Add site page are sub-pages of Sites."""
    run_id = await waiting_run(rig)
    site = rig.store.list_sites()[0]
    expect = {"/": "/", f"/runs/{run_id}": "/", "/sites": "/sites", f"/sites/{site.site_id}": "/sites",
              "/sites/new": "/sites", "/setup": "/setup", "/pair": "/setup"}
    for path, tab in expect.items():
        status, html, _ = await operator.get(path)
        assert status == 200, path
        assert " style=" not in html and "<style" not in html, path
        head = html[:html.index("</head>")]
        assert f"{PREFIX}/static/theme.js" in head, path
        assert re.findall(r'<a href="([^"]+)" aria-current="page">', html) == [PREFIX + tab], path
    status, _, _ = await operator.get("/sites/no-such-site")
    assert status == 404


# --------------------------------------------------------------------------- the app's own store


@pytest.fixture
async def builtin(tmp_path, aiohttp_client):
    """The pages of an app with no 1Password token: the logins are in its own store."""
    from controllerfakes import Rig
    r = Rig(tmp_path, login_store="builtin")
    await r.start()
    yield r, Operator(await aiohttp_client(make_ui(r.ctl)))
    await r.live.end_all()
    await r.ctl.shutdown()
    r.close()


def _shown(page: str) -> str:
    """A page's text and attribute values as a person reads them."""
    from html import unescape
    return unescape(page)


def _fixed_part(template: str) -> str:
    """A catalog text up to its first value: what a page built from it always holds."""
    return template.split("{")[0].strip()


async def test_builtin_setup_page_has_one_store_row_and_the_risk_line(builtin):
    """Setup with the app's own store: one row saying where the logins are kept, the language
    row, and one card with the risk of saved logins; nothing about 1Password or a guest."""
    from casa_browser import ui
    from casa_browser.i18n import catalog
    rig, operator = builtin
    status, page, _ = await operator.get("/setup")
    assert status == 200
    en = catalog("en")
    store_row = ui.row(ui.lead("ok", "ok"), ui.e(en["setup.store_builtin"]),
                       href=PREFIX + "/logins/", wrap=True)
    risk = f'<div class="card pad"><p class="small">{ui.e(en["setup.store_builtin_scope"])}</p></div>'
    assert page.count(store_row) == 1 and page.count(risk) == 1
    assert (page.index(store_row) < page.index(_fixed_part(en["setup.language"]))
            < page.index(risk) < page.index(ui.e(en["pair.heading"])))
    shown = _shown(page)
    assert en["setup.store_builtin_unavailable"] not in shown
    others = [key for key in en if key.startswith(("setup.guest_", "setup.onepassword_"))]
    assert len(others) == 10
    for key in others:
        assert _fixed_part(en[key]) and _fixed_part(en[key]) not in shown, key


@pytest.mark.parametrize("status_reply", [
    {"configured": True, "store": "builtin", "available": False},
    {"error": "unavailable"},
    None,
], ids=["not_available", "an_error", "no_answer"])
async def test_builtin_setup_page_says_when_the_store_cannot_be_opened(builtin, status_reply):
    from casa_browser import ui
    from casa_browser.i18n import catalog
    rig, operator = builtin
    real = rig.broker.call

    async def call(op, **args):
        if op == "status":
            if status_reply is None:
                raise OSError("no broker")
            return dict(status_reply)
        return await real(op, **args)
    rig.broker.call = call
    status, page, _ = await operator.get("/setup")
    assert status == 200
    en = catalog("en")
    alert = ui.row(ui.lead("alert", "err"), ui.e(en["setup.store_builtin_unavailable"]), wrap=True)
    assert page.count(alert) == 1
    assert en["setup.store_builtin"] not in _shown(page)
    assert en["setup.store_builtin_scope"] in _shown(page)
    assert "1Password" not in page


async def test_builtin_site_pages_use_the_saved_login_texts(builtin):
    """The new-site form has no login id field (nobody can know a saved login's id: a site's
    first sign-in asks which saved login it uses) and a posted one is ignored; a site's forget
    dialog and the extra-fields banner each use the text for a saved login, and none of them
    the 1Password one."""
    from casa_browser import ui
    from casa_browser.i18n import Text, catalog
    rig, operator = builtin
    t = Text("en")
    status, form, _ = await operator.get("/sites/new")
    assert status == 200
    assert 'name="vault_item_id"' not in form
    assert t("sites.item") not in _shown(form)
    status, _, resp = await operator.post("/sites", {
        "display_name": "Shop", "origins": "https://shop.example", "login_origins":
        "https://login.shop.example", "vault_item_id": "saved_shop", "start_page": "",
        "confirm": "yes"})
    assert status == 303, operator.html
    assert rig.broker.registered == []          # registered as a site with no login yet
    assert [c[1] for c in rig.broker.captured] == [("https://login.shop.example",)]
    site = next(s for s in rig.store.list_sites() if s.display_name == "Shop")
    assert site.vault_item_id is None
    status, listing, _ = await operator.get("/sites?note=registered&fields=recovery%20codes")
    assert status == 200
    banner = t("sites.extra_fields.builtin", fields="recovery codes")
    assert f"<p>{ui.e(banner)}</p>" in listing
    assert _fixed_part(catalog("en")["sites.extra_fields"]) not in _shown(listing)
    status, page, _ = await operator.get(f"/sites/{site.site_id}")
    assert status == 200
    assert f'data-confirm-body="{ui.e(t("sites.forget_body.builtin"))}"' in page
    assert t("sites.forget_body") not in _shown(page)
    for html in (form, listing, page):
        assert "1Password" not in html and "extension" not in html


def _broker_links(rig, links):
    rig.broker.status_reply = {"configured": True, "store": "builtin", "available": True,
                               "links": dict(links)}


async def test_builtin_pages_lead_to_the_saved_logins(builtin):
    """Setup's store row, and the page of a site that signs in, lead to the saved logins;
    a public site's page does not."""
    from casa_browser import ui
    from casa_browser.i18n import Text
    rig, operator = builtin
    t = Text("en")
    _broker_links(rig, {"sa": "item-a"})
    link = ui.row(ui.lead("key", "mute"), ui.e(t("sites.saved_logins")),
                  href=PREFIX + "/logins/")
    status, page, _ = await operator.get("/sites/sa")
    assert status == 200 and page.count(link) == 1
    status, page, _ = await operator.get("/sites/sp")
    assert status == 200 and "/logins/" not in page
    status, page, _ = await operator.get("/setup")
    assert status == 200 and page.count(f'href="{PREFIX}/logins/"') == 1


async def test_the_sites_list_refreshes_before_it_draws(builtin):
    from casa_browser.i18n import Text
    rig, operator = builtin
    t = Text("en")
    _broker_links(rig, {})
    status, page, _ = await operator.get("/sites")
    assert status == 200
    assert t("sites.item_set.builtin") not in _shown(page)
    assert t("sites.first_login") in _shown(page)
    assert rig.store.get_site("sa").vault_item_id is None


async def test_a_site_s_page_refreshes_before_it_draws(builtin):
    from casa_browser.i18n import Text
    rig, operator = builtin
    t = Text("en")
    _broker_links(rig, {})
    status, page, _ = await operator.get("/sites/sa")
    assert status == 200
    assert t("sites.item_set.builtin") not in _shown(page)
    assert t("sites.first_login") in _shown(page)
    assert rig.store.get_site("sa").vault_item_id is None


async def test_with_1password_there_is_no_saved_logins_page(rig, operator):
    """With a 1Password token the pages lead nowhere new: no link to saved logins, the new-site
    form keeps its item id field, a set item reads as today, and the path is not found."""
    from casa_browser.i18n import Text
    t = Text("en")
    pages = {}
    for path in ("/setup", "/sites", "/sites/sa", "/sites/new"):
        status, page, _ = await operator.get(path)
        assert status == 200, path
        pages[path] = page
        assert "/logins/" not in page, path
    assert 'name="vault_item_id"' in pages["/sites/new"]
    assert t("sites.item_set") in _shown(pages["/sites/sa"])
    assert t("sites.saved_logins") not in _shown(pages["/sites/sa"])
    status, _, _ = await operator.get("/logins/")
    assert status == 404
