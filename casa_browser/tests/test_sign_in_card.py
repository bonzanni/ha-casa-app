"""Slice 5n: signing in to a site that is not set up takes one operator tap.

The model asks (``ask_sign_in``) on the public page it judged needs a sign-in; the app reads the
host from the browser, takes its own screenshot, asks the vault check, and freezes one card. Only
the operator's Yes registers exactly the card's origin and signs in by the existing login flow.
Doubles of the browser, proxy, broker and driver; the real store, registry and observer."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from casa_browser import controller as ctl_mod
from casa_browser.protocol import Site
from controllerfakes import Rig, spec

pytestmark = pytest.mark.asyncio

SIGNIN = "https://Login.Example.test:443/in?next=/cp&state=abc#top"
ORIGIN = "https://login.example.test"
HOST = "login.example.test"
START = "https://login.example.test/in"
ITEMS = [{"item_id": "itm_eb", "title": "Enable Banking", "website_hosts": ["login.example.test"]},
         {"item_id": "itm_other", "title": "Other", "website_hosts": ["other.test"]},
         {"item_id": "itm_eb2", "title": "Enable Banking (work)",
          "website_hosts": ["login.example.test"]}]


@pytest.fixture
async def rig(tmp_path):
    r = Rig(tmp_path)
    await r.start()
    yield r
    await r.ctl.shutdown()
    r.close()


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def on_signin_page(rig, items=(), url=SIGNIN, run_spec=None):
    """An open-web run on the public sign-in page of a site that is not set up."""
    rig.browser.contents[url] = dict(text="Sign in with your email", title="Sign in")
    rig.broker.items_reply = {"items": list(items), "truncated": False}
    run = await rig.run(run_spec or spec(request_id="ow", mode="open_web", sites=(),
                                         start_url=url))
    assert rig.ctl.active.ref.kind == "public"
    return run


async def card(rig, run_id, verdict=None, name="Enable Banking"):
    """``ask_sign_in``, then (when the vault has items) the plugin's relay of the check."""
    step = (await rig.ctl.ask_sign_in(run_id, name)).to_wire()
    if step["outcome"] != "judging":
        return step
    pending = (await rig.ctl.pending_judgements(run_id))["judgements"]
    j = next(p for p in pending if p["kind"] == "login_item")
    body = ({"no_verdict": "check_unavailable"} if verdict is None
            else {"packet_sha256": sha(j["packet"]), "verdict": verdict})
    return await rig.ctl.apply_judgement(run_id, j["id"], body)


async def answer(rig, run_id, qid, choice, ref="tap:1"):
    q = await rig.ctl.get_question(run_id, qid)
    return await rig.ctl.answer_question(run_id, qid, {
        "revision": q["revision"], "digest": q["digest"], "choice": choice, "operator_ref": ref})


def new_sites(rig):
    return [s for s in rig.store.list_sites() if s.site_id.startswith("site_")]


def logins(rig):
    return [e for e in rig.log if e[:2] == ("broker", "login")]


# -- the card ---------------------------------------------------------------------------------

async def test_the_card_host_is_the_browser_address_and_the_name_only_a_label(rig):
    run = await on_signin_page(rig)
    asked = await card(rig, run.run_id, name="mail.google.com")
    assert asked["outcome"] == "asked" and rig.state(run.run_id) == "waiting_approval"
    view = (await rig.ctl.get_question(run.run_id, asked["question_id"]))["view"]
    assert view["kind"] == "site_signin" and view["host"] == HOST and view["item"] is None
    assert view["name"] == "mail.google.com"
    assert view["text"] == ("Sign in to login.example.test (mail.google.com)?\n"
                            "Yes, and next time I'll sign in here without asking. "
                            "You can undo that in Casa Browser's Sites page.")
    frozen = rig.ctl.active.question.proposal
    assert (frozen.origin, frozen.host, frozen.start_page) == (ORIGIN, HOST, START)
    assert "state=abc" not in json.dumps(view) and new_sites(rig) == []


async def test_a_non_default_port_is_shown_and_registered(rig):
    url = "https://login.example.test:8443/in"
    run = await on_signin_page(rig, url=url)
    asked = await card(rig, run.run_id)
    view = (await rig.ctl.get_question(run.run_id, asked["question_id"]))["view"]
    assert view["host"] == "login.example.test:8443"
    await answer(rig, run.run_id, asked["question_id"], "confirm")
    (site,) = new_sites(rig)
    assert site.origins == site.login_origins == ("https://login.example.test:8443",)


async def test_the_card_carries_the_apps_screenshot_of_that_page(rig):
    run = await on_signin_page(rig)
    shots = []

    async def capture(run_, session, policy, target=None):
        shots.append((session.page.url, target))
        return "shot1"
    rig.ctl.observer.capture_question = capture
    asked = await card(rig, run.run_id)
    view = (await rig.ctl.get_question(run.run_id, asked["question_id"]))["view"]
    assert view["screenshot_id"] == "shot1" and len(shots) == 1 and shots[0][1] is None


async def test_a_page_that_moves_during_the_capture_makes_no_card(rig):
    run = await on_signin_page(rig)

    async def capture(run_, session, policy, target=None):
        session.page.show("https://other.test/", "Elsewhere")
        return "shot1"
    rig.ctl.observer.capture_question = capture
    step = await card(rig, run.run_id)
    assert step["outcome"] == "page_changed"
    assert rig.store.open_question(run.run_id) is None and rig.state(run.run_id) == "running"


@pytest.mark.parametrize("case", ["account_page", "not_https", "not_configured", "question_open"])
async def test_refusals_make_no_card(tmp_path, case):
    r = Rig(tmp_path, configured=case != "not_configured")
    await r.start()
    try:
        if case == "account_page":
            run = await r.run()                       # an account run, on its own site's page
        else:
            url = "http://login.example.test/in" if case == "not_https" else SIGNIN
            run = await on_signin_page(r, url=url) if case != "not_configured" else \
                await on_signin_page(r)
        if case == "question_open":
            first = await card(r, run.run_id)
            assert first["outcome"] == "asked"
        if case == "question_open":         # the run waits on the card: not_ready, as any step
            with pytest.raises(ctl_mod.ProtocolError, match="not_ready"):
                await r.ctl.ask_sign_in(run.run_id, "X")
        else:
            step = (await r.ctl.ask_sign_in(run.run_id, "X")).to_wire()
            assert step["outcome"] == case.replace("_page", "_site")
        assert len([q for q in [r.store.open_question(run.run_id)] if q]) == \
            (1 if case == "question_open" else 0)
    finally:
        await r.ctl.shutdown()
        r.close()


async def test_a_broader_registered_site_is_named_never_carded(rig):
    """d1 Astra S1: a registered site that covers the card's origin and reaches more is never
    signed in from a card that showed only this host."""
    rig.store.put_site(Site("sw", "Wide", (ORIGIN, "https://unseen.test"), (ORIGIN,), "item-w",
                            (), "https://unseen.test/start", 1))
    run = await on_signin_page(rig)
    step = await card(rig, run.run_id)
    assert step["outcome"] == "registered_site" and step["site"] == "Wide"
    assert rig.store.open_question(run.run_id) is None and logins(rig) == []


# -- the vault check on the card --------------------------------------------------------------

async def test_one_fitting_item_is_named_and_one_yes_links_it_and_signs_in(rig):
    run = await on_signin_page(rig, ITEMS)
    asked = await card(rig, run.run_id, {"items": [0], "reason": "same host"})
    view = (await rig.ctl.get_question(run.run_id, asked["question_id"]))["view"]
    assert view["item"] == "Enable Banking"
    assert view["text"].startswith('Sign in to login.example.test (Enable Banking) with your '
                                   '1Password item "Enable Banking"?')
    assert "itm_eb" not in json.dumps(view)
    reply = await answer(rig, run.run_id, asked["question_id"], "confirm")
    assert reply["outcome"] == "signing_in" and reply["label"] == HOST
    (site,) = new_sites(rig)
    assert (site.origins, site.login_origins, site.start_page_private, site.vault_item_id) == \
        ((ORIGIN,), (ORIGIN,), START, "itm_eb")
    assert rig.broker.registered == [(site.site_id, "itm_eb", "tap:1")]
    assert rig.broker.register_origins == [(ORIGIN,)]
    assert len(logins(rig)) == 1 and ("goto", START) in rig.log
    assert rig.ctl.active.ref.kind == "account" and rig.ctl.active.ref.site_id == site.site_id
    assert rig.store.open_question(run.run_id) is None


async def test_several_fitting_items_name_none_and_the_pick_follows_the_yes(rig):
    run = await on_signin_page(rig, ITEMS)
    asked = await card(rig, run.run_id, {"items": [0, 2], "reason": "two"})
    view = (await rig.ctl.get_question(run.run_id, asked["question_id"]))["view"]
    assert view["item"] is None
    rig.broker.login_reply = {"status": "needs_live_view", "reason": "not_found"}
    await answer(rig, run.run_id, asked["question_id"], "confirm")
    (site,) = new_sites(rig)
    assert rig.broker.captured == [(site.site_id, (ORIGIN,), "tap:1", "Enable Banking")]
    pending = (await rig.ctl.pending_judgements(run.run_id))["judgements"]
    assert [p["kind"] for p in pending] == ["login_item"]
    assert json.loads(pending[0]["packet"])["site"]["registered_origins"] == [ORIGIN]


@pytest.mark.parametrize("verdict", [{"items": [], "reason": "none"}, None])
async def test_no_fit_goes_to_the_live_view_without_a_second_check(rig, verdict):
    run = await on_signin_page(rig, ITEMS)
    asked = await card(rig, run.run_id, verdict)
    assert asked["outcome"] == "asked"                         # no verdict: the card still goes
    rig.broker.login_reply = {"status": "needs_live_view", "reason": "not_found"}
    await answer(rig, run.run_id, asked["question_id"], "confirm")
    lists = [e for e in rig.log if e[:2] in (("broker", "list_login_items"),
                                              ("broker", "list_vault_logins"))]
    assert len(lists) == 1
    assert [e.outcome for e in rig.events(run.run_id, "login_needed")] == ["not_found"]
    assert (await rig.ctl.pending_judgements(run.run_id))["judgements"] == []


# -- the answer -------------------------------------------------------------------------------

async def test_yes_registers_exactly_the_card_origin_and_the_run_gains_it(rig):
    run = await on_signin_page(rig)
    asked = await card(rig, run.run_id)
    await answer(rig, run.run_id, asked["question_id"], "confirm")
    (site,) = new_sites(rig)
    assert site.origins == site.login_origins == (ORIGIN,) and site.display_name == "Enable Banking"
    assert rig.broker.captured == [(site.site_id, (ORIGIN,), "tap:1", "Enable Banking")]
    stored = rig.store.get_run(run.run_id).spec
    assert stored.mode == "account" and [(g.site_id, g.access) for g in stored.sites] == \
        [(site.site_id, "account")]
    assert ("broker", "begin_run", run.run_id, None) in rig.log
    assert rig.ctl.active.ref.kind == "account"


async def test_a_page_moving_after_the_card_registers_the_cards_origin(rig):
    run = await on_signin_page(rig)
    asked = await card(rig, run.run_id)
    rig.page().show("https://other.test/elsewhere", "Elsewhere")
    await answer(rig, run.run_id, asked["question_id"], "confirm")
    (site,) = new_sites(rig)
    assert site.origins == (ORIGIN,) and rig.broker.captured[0][1] == (ORIGIN,)
    assert ("goto", START) in rig.log


async def test_no_and_expiry_register_nothing(rig):
    run = await on_signin_page(rig)
    asked = await card(rig, run.run_id)
    reply = await answer(rig, run.run_id, asked["question_id"], "not_now")
    assert reply["outcome"] == "declined" and rig.state(run.run_id) == "running"
    again = await card(rig, run.run_id)
    rig.clock.advance(3601)
    await rig.ctl.expire_questions()
    assert rig.store.get_question(again["question_id"]).state == "expired"
    assert new_sites(rig) == [] and rig.broker.captured == [] and logins(rig) == []
    assert rig.state(run.run_id) == "running" and rig.ctl.active.ref.kind == "public"


async def test_a_site_registered_meanwhile_is_never_taken_over(rig):
    run = await on_signin_page(rig)
    asked = await card(rig, run.run_id)
    rig.store.put_site(Site("sm", "Meanwhile", (ORIGIN, "https://unseen.test"), (ORIGIN,), None,
                            (), "https://unseen.test/start", 1))
    reply = await answer(rig, run.run_id, asked["question_id"], "confirm")
    assert reply["outcome"] == "already_saved"
    assert new_sites(rig) == [] and logins(rig) == []
    assert not any(e[0] == "goto" and "unseen.test" in e[1] for e in rig.log)


async def test_an_exact_registered_site_is_signed_in_without_a_second_site(rig):
    rig.store.put_site(Site("sx", "Exact", (ORIGIN,), (ORIGIN,), None, (), START, 1))
    run = await on_signin_page(rig, run_spec=spec(request_id="ow", mode="open_web", sites=(),
                                                  start_url=SIGNIN))
    asked = await card(rig, run.run_id)
    view = (await rig.ctl.get_question(run.run_id, asked["question_id"]))["view"]
    assert view["name"] == "Exact"
    await answer(rig, run.run_id, asked["question_id"], "confirm")
    assert new_sites(rig) == [] and len(logins(rig)) == 1


async def test_yes_replaces_a_public_grant(rig):
    """d1 Astra S2: the first grant decides access, so a public grant is replaced."""
    rig.store.put_site(Site("sx", "Exact", (ORIGIN,), (ORIGIN,), None, (), START, 1))
    run = await on_signin_page(rig, run_spec=spec(request_id="acc", mode="account",
                                                  sites=(("sx", "public"),)))
    page = rig.page()
    page.show(SIGNIN, "Sign in")
    asked = await card(rig, run.run_id)
    await answer(rig, run.run_id, asked["question_id"], "confirm")
    grants = [(g.site_id, g.access) for g in rig.ctl.active.spec.sites]
    assert grants == [("sx", "account")] and len(logins(rig)) == 1
    assert rig.ctl.active.ref.kind == "account"


async def test_a_promoted_open_web_run_owes_its_release_after_a_restart(rig):
    run = await on_signin_page(rig)
    asked = await card(rig, run.run_id)
    await answer(rig, run.run_id, asked["question_id"], "confirm")
    assert run.run_id in rig.store.interrupt_nonterminal()
    assert rig.store.owed_releases() == [run.run_id]


async def test_a_repeated_tap_registers_once(rig):
    run = await on_signin_page(rig)
    asked = await card(rig, run.run_id)
    first = await answer(rig, run.run_id, asked["question_id"], "confirm")
    again = await answer(rig, run.run_id, asked["question_id"], "confirm")
    assert again == {**first, "repeat": True}
    assert len(new_sites(rig)) == 1 and len(rig.broker.captured) == 1


async def test_a_tap_after_the_run_ended_registers_nothing(rig):
    run = await on_signin_page(rig)
    asked = await card(rig, run.run_id)
    q = await rig.ctl.get_question(run.run_id, asked["question_id"])
    await rig.ctl.cancel(run.run_id)
    await rig.settle()
    try:
        reply = await rig.ctl.answer_question(run.run_id, asked["question_id"], {
            "revision": q["revision"], "digest": q["digest"], "choice": "confirm",
            "operator_ref": "tap:1"})
    except ctl_mod.ProtocolError:
        reply = None
    assert reply in (None, {"outcome": "not_current", "generation": 0})
    assert new_sites(rig) == [] and rig.broker.captured == []


async def test_the_card_offers_no_live_view_and_no_item_answer(rig):
    run = await on_signin_page(rig)
    asked = await card(rig, run.run_id)
    q = await rig.ctl.get_question(run.run_id, asked["question_id"])
    with pytest.raises(ctl_mod.ProtocolError):
        await rig.ctl.live_from_question(run.run_id, asked["question_id"], {
            "revision": 1, "digest": q["digest"], "n": 1, "operator_ref": "tap:2"})
    with pytest.raises(ctl_mod.ProtocolError):
        await answer(rig, run.run_id, asked["question_id"], {"item": 0})
    assert new_sites(rig) == []


async def test_a_start_page_that_does_not_open_is_said_on_the_receipt(rig):
    """r1 Terra: the site is saved, but its sign-in page did not open: the answer says so
    (``not_opened``) instead of "signing in"."""
    run = await on_signin_page(rig)
    asked = await card(rig, run.run_id)

    original = rig.ctl._goto

    async def goto(active, url):
        if url == START:
            raise ctl_mod.ProtocolError("blocked")
        return await original(active, url)
    rig.ctl._goto = goto
    reply = await answer(rig, run.run_id, asked["question_id"], "confirm")
    assert reply["outcome"] == "not_opened" and reply["label"] == HOST
    assert len(new_sites(rig)) == 1 and rig.state(run.run_id) == "running"
    assert [e.outcome for e in rig.events(run.run_id, "page_unreachable")][-1] == "blocked"
