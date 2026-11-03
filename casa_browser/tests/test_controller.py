"""Task 12: the run controller over doubles of the browser, proxy, broker, driver and
downloader, with the real store, handle registry and observer."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from casa_browser import controller as ctl_mod
from casa_browser.downloads import DownloadRefused
from casa_browser.policy import RequestMeta
from casa_browser.protocol import (
    Action,
    ProtocolError,
    ProviderBinding,
    ResultManifest,
    SafeNotice,
)
from casa_browser.proxy_client import PrivateRequestEvent
from controllerfakes import ACCOUNT, OTHER, PUBLIC, SEARCH, Rig, spec

pytestmark = pytest.mark.asyncio

A_START = ACCOUNT.start_page_private
A_PAGE = "https://a.example/invoices"
B_PAGE = "https://b.example/"


@pytest.fixture
async def rig(tmp_path):
    r = Rig(tmp_path)
    await r.start()
    yield r
    await r.ctl.shutdown()
    r.close()


async def observe(rig, run_id):
    return await rig.ctl.observe(run_id)


def handle_of(obs, label):
    return next(e.handle for e in obs.elements if label in e.label_masked)


GO = {"verdict": "go", "category": "reading", "reason": "reads the page"}


def sha(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def settle(rig, run_id, result, verdict=GO):
    """The plugin's relay, scripted: a judged step gets ``verdict`` (a go by default, so the
    tests of other behaviour run the action as before); a refusal before dispatch is raised as
    the error it names."""
    wire = result.to_wire() if hasattr(result, "to_wire") else result
    if verdict is None or not isinstance(wire, dict) or wire.get("outcome") != "judgement_required":
        return result
    j = wire["judgement"]
    reply = await rig.ctl.apply_judgement(run_id, j["id"], {"packet_sha256": sha(j["packet"]),
                                                          "verdict": verdict})
    if reply.get("outcome") == "not_run" and reply.get("reason") in ProtocolError_codes():
        raise ProtocolError(reply["reason"])
    return Reply(reply)


class Reply(dict):
    """A verdict's response, readable like an ActionResult (``.outcome``, ``.to_wire()``)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def to_wire(self):
        return dict(self)


def ProtocolError_codes():
    from casa_browser.protocol import ERROR_CODES
    return ERROR_CODES


async def act(rig, run_id, kind, handle, obs, *, verdict=GO, **kw):
    result = await rig.ctl.act(run_id, Action(kind, handle, obs.generation, **kw))
    return await settle(rig, run_id, result, verdict)


async def download_(rig, run_id, handle, *, verdict=GO):
    """A download, its judgement settled: the file record (readable by attribute)."""
    reply = await settle(rig, run_id, await rig.ctl.download(run_id, handle), verdict)
    if isinstance(reply, Reply) and reply.get("outcome") == "done":
        return Reply(reply["file"])
    return reply


def manifest(**kw):
    base = dict(summary_masked="Done.", file_ids=(), source_ids=(), expectation_status="matched")
    base.update(kw)
    return ResultManifest(**base)


# --------------------------------------------------------------------------- queue and lease


async def test_one_active_run_fifo(rig):
    """Three runs started together get one browser lease, in FIFO order."""
    runs = await asyncio.gather(*(rig.ctl.start_run(spec(request_id=f"rq{n}")) for n in range(3)))
    await rig.settle()
    ids = [r.run_id for r, _ in runs]
    assert [rig.state(i) for i in ids] == ["running", "queued", "queued"]
    assert rig.log.kinds().count("open") == 1
    await rig.ctl.finish(ids[0], manifest())
    await rig.settle()
    assert [rig.state(i) for i in ids] == ["completed", "running", "queued"]
    opens = [n for n, e in enumerate(rig.log) if e[0] == "open"]
    closes = [n for n, e in enumerate(rig.log) if e[0] == "close"]
    assert len(opens) == 2 and closes and closes[0] < opens[1]


async def test_idempotent_start_spawns_one_browser(rig):
    s = spec(request_id="same")
    first, created = await rig.ctl.start_run(s)
    again, created_again = await rig.ctl.start_run(s)
    await rig.settle()
    assert created and not created_again and first.run_id == again.run_id
    assert rig.log.kinds().count("open") == 1


async def test_start_refuses_unready(tmp_path):
    (tmp_path / "a").mkdir()
    r = Rig(tmp_path / "a", configured=False)
    await r.start()
    try:
        with pytest.raises(ProtocolError, match="not_ready"):
            await r.ctl.start_run(spec())          # an account run needs 1Password configured
        run = await r.run(spec(request_id="ow", mode="open_web", sites=()))
        assert run.state == "running"              # an open-web run does not
    finally:
        await r.ctl.shutdown()
        r.close()
    (tmp_path / "b").mkdir()
    r = Rig(tmp_path / "b", locale=None)
    await r.start()
    try:
        with pytest.raises(ProtocolError, match="not_ready"):
            await r.ctl.start_run(spec(request_id="ow2", mode="open_web", sites=()))
        assert (await r.ctl.hello())["needs_locale"] is True
    finally:
        await r.ctl.shutdown()
        r.close()


async def test_start_refuses_unknown_site(rig):
    with pytest.raises(ProtocolError, match="not_found"):
        await rig.ctl.start_run(spec(request_id="x", sites=(("missing", "account"),)))


# --------------------------------------------------------------------------- sessions


async def test_account_to_public_never_reuses_profile(rig):
    run = await rig.run(sites=(("sa", "account"), ("sp", "public")))
    first = rig.browser.session
    first_page = first.page
    assert (first.kind, first.site_id) == ("account", "sa")
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Public B"), obs)
    second = rig.browser.session
    second_page = second.page
    assert (second.kind, second.site_id) == ("public", None)
    assert second is not first and second.context is not first.context
    assert first_page.closed and second.generation == first.generation + 1
    # Each open follows the close of the previous browser: never two at once.
    opens = [n for n, e in enumerate(rig.log) if e[0] == "open"]
    assert rig.log[opens[1] - 1][0] in ("close", "install")
    assert rig.log.index_of("close", "account", "sa") < opens[1]
    # The public session got no guest or login driver session and no broker call.
    after = rig.log[rig.log.index_of("close", "account", "sa"):]
    assert not [e for e in after if e[0] == "open_session"]
    assert not [e for e in after if e[0] == "broker" and e[3] == "sp"]
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Site A"), obs)
    third = rig.browser.session
    assert (third.kind, third.site_id) == ("account", "sa") and third is not first
    assert second_page.closed


async def test_multisite_only_granted_sites(rig):
    run = await rig.run(sites=(("sa", "account"), ("sp", "public")))
    obs = await observe(rig, run.run_id)
    labels = [e.label_masked for e in obs.elements]
    assert any("Site A" in l for l in labels) and any("Public B" in l for l in labels)
    assert not any("Site C" in l for l in labels)
    # A link to C on A's page (C is not a site of this run): opened in the public session
    # (design §4), the account browser closed first; nothing held, nothing asked.
    rig.page().show(A_START, "Start", [dict(role="link", name="Elsewhere",
                                            href="https://c.example/x")])
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Elsewhere"), obs)
    assert rig.log.index_of("open", "public") < rig.log.index(("goto", "https://c.example/x"))
    assert rig.browser.session.kind == "public"
    # B is used in the public session, never with credentials.
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Public B"), obs)
    assert rig.browser.session.kind == "public"
    assert not [e for e in rig.log if e[0] == "broker" and e[3] == "sp"]
    assert not [e for e in rig.log if e[0] == "open_session" and e[2] in ("guest", "login")
                and rig.log.index(e) > rig.log.index_of("open", "public")]


async def test_off_site_navigation_moves_to_public_profile(rig):
    """S4-1: in an account run, a link to an address off the run's sites opens in the public
    profile — free there (no judgement) — and a link back to the site returns to the account
    profile, judged as every signed-in navigation is."""
    run = await rig.run()
    rig.page().show(A_START, "Start", [dict(role="link", name="News",
                                            href="https://news.example/today")])
    obs = await observe(rig, run.run_id)
    judged = len(rig.events(run.run_id, "judgement"))
    await act(rig, run.run_id, "click", handle_of(obs, "News"), obs)
    assert (rig.browser.session.kind, rig.browser.session.site_id) == ("public", None)
    assert ("goto", "https://news.example/today") in rig.log
    assert len(rig.events(run.run_id, "judgement")) == judged          # free in public
    assert rig.ctl.active.ref.kind == "public"
    rig.page().show("https://news.example/today", "News", [dict(role="link", name="Back",
                                                                href=A_PAGE)])
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "click", handle_of(obs, "Back"), obs)
    assert (rig.browser.session.kind, rig.browser.session.site_id) == ("account", "sa")
    assert len(rig.events(run.run_id, "judgement")) == judged + 1      # checked on the site


async def test_plain_http_address_of_the_site_stays_signed_in(rig):
    """casa-test F16 with S4-1: an ``http://`` link to a host the site registered over https is
    the site's page — it stays in the account session (judged there) and is sent for the
    proxy's https redirect, never moved to the public session."""
    run = await rig.run()
    rig.page().show(A_START, "Start", [dict(role="link", name="Plain",
                                            href="http://a.example/next")])
    obs = await observe(rig, run.run_id)
    judged = len(rig.events(run.run_id, "judgement"))
    await act(rig, run.run_id, "navigate", handle_of(obs, "Plain"), obs)
    assert ("goto", "http://a.example/next") in rig.log
    assert (rig.browser.session.kind, rig.browser.session.site_id) == ("account", "sa")
    assert not [e for e in rig.log if e[0] == "open" and e[1] == "public"]
    assert len(rig.events(run.run_id, "judgement")) == judged + 1


async def test_account_cookies_never_reach_public_profile(rig):
    """The public session is the ephemeral profile, another browser context: an account
    profile's cookies are never in it, and a public download sends cookies only to the page's
    own origin, never to the account site's origins."""
    from casa_browser.browser import profile_for
    assert profile_for("public", None) == "ephemeral" != profile_for("account", "sa")
    run = await rig.run()
    account = rig.browser.session
    rig.page().show(A_START, "Start", [dict(role="link", name="News",
                                            href="https://news.example/today")])
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "News"), obs)
    public = rig.browser.session
    assert public.kind == "public" and public.context is not account.context
    assert ("open", "public", None) in [e[:3] for e in rig.log if e[0] == "open"]
    from casa_browser.downloads import Downloader
    scope = Downloader._cookie_scope(None, public, rig.ctl.active.policy, "https://news.example")
    assert scope == {"https://news.example"}


async def test_password_field_never_typed(rig):
    run = await rig.run()
    rig.page().show(A_START, "Login", [dict(role="textbox", name="Password", field=True,
                                            input_type="password"),
                                       dict(role="textbox", name="Search", field=True,
                                            input_type="text")])
    obs = await observe(rig, run.run_id)
    with pytest.raises(ProtocolError, match="unsupported"):
        await act(rig, run.run_id, "type", handle_of(obs, "Password"), obs, text="hunter2")
    assert not [e for e in rig.log if e[0] == "type"]
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "type", handle_of(obs, "Search"), obs, text="march")
    assert [e for e in rig.log if e[0] == "type"] == [("type", "Search", 5, False)]


async def test_stale_handle_after_navigation(rig):
    run = await rig.run()
    rig.page().show(A_START, "Start", [dict(role="link", name="Invoices", href=A_PAGE)])
    obs = await observe(rig, run.run_id)
    rig.page().show(A_START, "moved")
    with pytest.raises(ProtocolError, match="stale_handle"):
        await act(rig, run.run_id, "click", handle_of(obs, "Invoices"), obs)


async def test_a_page_off_the_session_is_blocked(rig):
    """S4-3, S4-4: an account session's page that is not the site's (the page navigated
    itself) is not observed: ``blocked``, no question."""
    run = await rig.run()
    rig.page().show("https://c.example/elsewhere", "Elsewhere")
    with pytest.raises(ProtocolError, match="blocked"):
        await observe(rig, run.run_id)
    assert rig.state(run.run_id) == "running"


async def test_start_page_loads(rig):
    """The site's start page belongs to the session: it loads (through the broker's login,
    the site having login origins)."""
    run = await rig.run()
    assert rig.state(run.run_id) == "running"
    assert ("goto", A_START) in rig.log


async def test_an_open_web_run_opens_the_tasks_address_in_the_public_profile(rig):
    """Slice 5b (F-a): an open-web spec's start_url is the run's first page, loaded the way a
    free public navigation is (the public session, the floor, the G1 check before the load)."""
    url = "https://shop.example/forms/post"
    rig.browser.contents[url] = dict(text="Pizza form", title="Order")
    run = await rig.run(spec(request_id="ow", mode="open_web", sites=(), start_url=url))
    assert rig.state(run.run_id) == "running"
    assert ("goto", url) in rig.log and rig.ctl.active.ref.kind == "public"
    obs = await observe(rig, run.run_id)
    assert "Pizza form" in obs.text and obs.source_id
    assert not rig.events(run.run_id, "page_unreachable")


async def test_a_start_url_the_floor_refuses_leaves_the_run_on_a_blank_page(rig):
    url = "https://127.0.0.1/admin"
    run = await rig.run(spec(request_id="ow", mode="open_web", sites=(), start_url=url))
    assert rig.state(run.run_id) == "running" and ("goto", url) not in rig.log
    assert rig.events(run.run_id, "page_unreachable")


async def test_an_open_web_search_for_an_exact_address_opens_that_page(rig):
    """Slice 5c: a run already going reaches a page the task names by its address. In an
    open-web run a query that is exactly one https address opens it the way the start address
    does (public, free, no search page); any other query, and any account run's, searches."""
    run = await rig.run(spec(request_id="ow", mode="open_web", sites=()))
    assert "exact https address" in (await observe(rig, run.run_id)).text
    url = "https://shop.example/forms/post"
    rig.browser.contents[url] = dict(text="Pizza form", title="Order")
    obs = await rig.ctl.search(run.run_id, url)
    assert ("goto", url) in rig.log and rig.ctl.active.ref.kind == "public"
    assert "Pizza form" in obs.text and obs.source_id
    assert not rig.events(run.run_id, "judgement")
    words = "shop.example forms"
    rig.browser.contents[ctl_mod.SEARCH_ORIGIN + ctl_mod.SEARCH_PATH + "shop.example+forms"] = \
        dict(text="Results", title="Search")
    assert (await rig.ctl.search(run.run_id, words)).text.startswith("Results")
    with pytest.raises(ProtocolError, match="blocked"):        # the floor still applies
        await rig.ctl.search(run.run_id, "https://127.0.0.1/admin")
    assert ("goto", "https://127.0.0.1/admin") not in rig.log


async def test_an_account_run_searches_an_address_as_words(rig):
    run = await rig.run(spec(request_id="lone"))
    url = "https://shop.example/forms/post"
    rig.browser.contents[ctl_mod.SEARCH_ORIGIN + ctl_mod.SEARCH_PATH
                         + "https%3A%2F%2Fshop.example%2Fforms%2Fpost"] = dict(
        text="Results", title="Search")
    assert (await rig.ctl.search(run.run_id, url)).text.startswith("Results")
    assert ("goto", url) not in rig.log


async def test_the_page_now_is_an_operator_screenshot_of_a_running_run(rig):
    """Slice 5b (F-j): the plugin can show what a confirmed step led to."""
    url = "https://shop.example/thanks"
    rig.browser.contents[url] = dict(text="Order accepted", title="Thanks")
    run = await rig.run(spec(request_id="ow", mode="open_web", sites=(), start_url=url))
    shot = await rig.ctl.take_screenshot(run.run_id)
    assert (await rig.ctl.screenshot(run.run_id, shot)).startswith(b"\x89PNG")
    await rig.ctl.request_live(run.run_id, "assist")
    with pytest.raises(ProtocolError, match="not_ready"):
        await rig.ctl.take_screenshot(run.run_id)


# --------------------------------------------------------------------------- live view


async def test_live_pause_drains_inflight_action(rig, monkeypatch):
    run = await rig.run()
    rig.page().show(A_START, "Start", [dict(role="button", name="Go")])
    obs = await observe(rig, run.run_id)
    rig.gate = asyncio.Event()
    action = asyncio.create_task(act(rig, run.run_id, "click", handle_of(obs, "Go"), obs))
    await asyncio.sleep(0.01)
    live = asyncio.create_task(rig.ctl.request_live(run.run_id, "assist"))
    await asyncio.sleep(0.01)
    assert not live.done() and rig.state(run.run_id) == "running"
    rig.gate.set()                       # the action completes inside the drain bound
    assert (await action).outcome == "done"
    await live
    assert rig.state(run.run_id) == "waiting_live"
    # Every automation action is refused while the view waits or runs.
    for call in (rig.ctl.observe(run.run_id), rig.ctl.search(run.run_id, "x"),
                 rig.ctl.download(run.run_id, "h"),
                 act(rig, run.run_id, "click", "h", obs)):
        with pytest.raises(ProtocolError, match="not_ready"):
            await call


async def test_live_drain_cuts_a_hung_action(rig, monkeypatch):
    monkeypatch.setattr(ctl_mod, "DRAIN_S", 0.05)
    run = await rig.run()
    rig.page().show(A_START, "Start", [dict(role="button", name="Go")])
    obs = await observe(rig, run.run_id)
    rig.gate = asyncio.Event()           # never set: the action hangs
    action = asyncio.create_task(act(rig, run.run_id, "click", handle_of(obs, "Go"), obs))
    await asyncio.sleep(0.01)
    grant = await rig.ctl.request_live(run.run_id, "assist")
    with pytest.raises(ProtocolError, match="not_ready"):
        await action
    assert grant.grant_id and rig.state(run.run_id) == "waiting_live"
    asked = [e.text_masked for e in rig.events(run.run_id) if e.kind == "live_requested"]
    assert asked == ["The model asked for an assist live view."]     # casa-test O4 grammar


async def test_capture_armed_before_input(rig):
    run = await rig.run()
    await rig.ctl.request_live(run.run_id, "login")
    exposed = []

    async def expose():
        exposed.append(list(rig.log))
    await rig.ctl.begin_live(run.run_id, expose)
    assert rig.state(run.run_id) == "live"
    before = exposed[0]
    manual = before.index(next(e for e in before if e[0] == "begin_manual"))
    capture = before.index(next(e for e in before if e[:2] == ("open_session", "capture")))
    armed = before.index(next(e for e in before if e[:2] == ("broker", "begin_capture")))
    assert manual < capture < armed


async def test_capture_failure_offers_no_live_view(rig):
    run = await rig.run()
    rig.broker.capture_reply = {"error": "conflict"}
    await rig.ctl.request_live(run.run_id, "login")
    exposed = []

    async def expose():
        exposed.append(True)
    with pytest.raises(ProtocolError, match="not_ready"):
        await rig.ctl.begin_live(run.run_id, expose)
    assert exposed == [] and rig.state(run.run_id) == "running"
    assert rig.events(run.run_id, "live_unavailable")[-1].outcome == "conflict"
    assert ("end_manual",) == rig.log[rig.log.index_of("end_manual")][:1]
    token_closes = [e for e in rig.log if e[0] == "close_window"]
    assert token_closes and token_closes[-1][1] == rig.proxy.token
    assert not rig.driver.sessions


async def test_done_drains_events_through_the_view(rig):
    """The binding's callback event recorded just before Done is consumed before learning."""
    run = await rig.run()
    await rig.ctl.request_live(run.run_id, "login")

    async def expose():
        pass
    await rig.ctl.begin_live(run.run_id, expose)
    tags = {k: "%064x" % n for n, k in enumerate(("path_tag", "client_id_tag", "redirect_uri_tag"), 1)}
    base = dict(run_id=run.run_id, revision=1, listener="browser", allow=True, reason="manual_lease",
                event_kind="request_allowed", method="GET",
                resource_type="document", is_navigation=True, at=1.0)
    rig.proxy.queue += [
        PrivateRequestEvent(seq=10, origin="https://login.a.example",
                            binding={"candidate": 1, **tags}, **base),
        PrivateRequestEvent(seq=11, origin="https://a.example", binding={"callback_of": 1}, **base)]
    await rig.ctl.live_done(run.run_id)   # no pump ran in between; nothing asked (ruling 2026-10-31)
    site = rig.store.get_site("sa")
    assert len(site.provider_bindings) == 1
    assert site.provider_bindings[0].client_id_tag == tags["client_id_tag"]
    installs = [n for n, e in enumerate(rig.log) if e[0] == "install"]
    assert installs[-1] > rig.log.index_of("end_manual")
    assert rig.proxy.policy.sites[0].provider_bindings == site.provider_bindings
    # learned under the proxy's current binding key: shown as bound, not as needing a login
    assert rig.store.binding_keys() == {"sa": rig.proxy.binding_key_id}
    assert rig.ctl.bindings_lost() == set()


@pytest.mark.parametrize("outcome", ["no_callback", "live_complete", "two_candidates", "assist"])
async def test_provider_binding_only_from_one_attempt_and_its_callback(rig, outcome):
    """Slice 5g (ruling 2026-10-31): the operator's Done learns a binding without asking — only
    from exactly one provider attempt and the provider's redirect back, in a login view; never
    through the plugin's live-complete."""
    run = await rig.run()
    await rig.ctl.request_live(run.run_id, "assist" if outcome == "assist" else "login")

    async def expose():
        pass
    await rig.ctl.begin_live(run.run_id, expose)
    tags = {k: "%064x" % n for n, k in enumerate(("path_tag", "client_id_tag", "redirect_uri_tag"), 1)}
    base = dict(run_id=run.run_id, revision=1, listener="browser", allow=True, reason="manual_lease",
                event_kind="request_allowed", method="GET",
                resource_type="document", is_navigation=True, at=1.0)
    events = [PrivateRequestEvent(seq=10, origin="https://login.a.example",
                                  binding={"candidate": 1, **tags}, **base)]
    if outcome == "two_candidates":
        events.append(PrivateRequestEvent(seq=11, origin="https://login.a.example",
                                          binding={"candidate": 2, **tags}, **base))
    if outcome != "no_callback":
        events.append(PrivateRequestEvent(seq=12, origin="https://a.example",
                                          binding={"callback_of": 1}, **base))
    rig.proxy.queue += events
    if outcome == "live_complete":
        await rig.ctl.live_complete(run.run_id, "op-1")
    else:
        await rig.ctl.live_done(run.run_id)
    assert rig.store.get_site("sa").provider_bindings == ()
    assert rig.state(run.run_id) == "running"


@pytest.mark.parametrize("values", [[], [("password", "Changed-Pw-91")]], ids=["empty", "changed"])
async def test_live_complete_ends_the_capture(tmp_path, values):
    """``live-complete`` (no operator review) ends the view's capture at the real broker: its
    values reach the broker, an unsaved changed value leaves the warning notice, nothing is
    saved, and the site's next login view can capture again (r2 S2)."""
    from brokerfakes import Rig as BrokerRig
    from casa_browser.broker_client import BrokerClient
    (tmp_path / "b").mkdir()
    br = BrokerRig(tmp_path / "b")
    await br.broker.start()
    await br.call("register_capture", site_id="sa", login_origins=["https://login.a.example"],
                  operator_ref="op", title="Site A")
    r = Rig(tmp_path)
    client = BrokerClient(store=r.store)

    async def call(op, **args):
        return await br.call(op, **args)
    client.call = call
    r.ctl.broker = client
    await r.start()
    try:
        run = await r.run()
        br.driver.values = list(values)

        async def expose():
            pass
        await r.ctl.request_live(run.run_id, "login")
        await r.ctl.begin_live(run.run_id, expose)
        await r.ctl.live_complete(run.run_id, "op")
        assert br.broker._captures == {} and br.writes() == []
        warnings = [n for n in (await br.call("pending_notices", after=0))["notices"]
                    if n["kind"] == "credential_unknown"]
        assert len(warnings) == (1 if values else 0)
        await r.ctl.request_live(run.run_id, "login")
        await r.ctl.begin_live(run.run_id, expose)
        assert r.state(run.run_id) == "live" and len(br.broker._captures) == 1
    finally:
        await r.ctl.shutdown()
        r.close()
        br.journal.close()


# --------------------------------------------------------------------------- broker and driver


@pytest.mark.parametrize("reply,reason", [
    ({"status": "needs_live_view", "reason": "password_change_page"}, "password_change_page"),
    ({"status": "failed", "reason": "rejected"}, "rejected"),
    ({"status": "needs_live_view", "reason": "challenge"}, "challenge")])
async def test_login_needed_event(rig, reply, reason):
    rig.broker.login_reply = reply
    run = await rig.run()
    needed = rig.events(run.run_id, "login_needed")
    assert [e.outcome for e in needed] == [reason]
    assert "http" not in needed[0].text_masked and needed[0].handle is None
    assert rig.state(run.run_id) == "running"


async def test_a_sign_in_that_found_no_form_asks_for_nothing(rig):
    """Slice 5u (operator ruling 2): the broker filled nothing and found no form — it claims
    nothing and the run says nothing: the model reading the page decides whether he is signed
    in or needs the login live view (an operator already signed in sees no form either)."""
    rig.broker.login_reply = {"status": "no_form", "reason": "no_form"}
    run = await rig.run()
    assert rig.events(run.run_id, "login_needed") == []
    assert rig.state(run.run_id) == "running"


@pytest.mark.parametrize("unlock", [{"state": "unlocked", "reason": "completed"},
                                    {"state": "needs_live_view", "reason": "guest_prompt"},
                                    {"state": "unknown"}, {}])
async def test_a_guest_not_unlocked_asks_for_nothing_and_the_login_goes_on(rig, unlock):
    """Slice 5s (casa-test): the extension shows no page whose state the driver reads, so every
    browser start replied guest_prompt, and every account run asked for a login live view before
    its sign-in was even tried. Only a passkey needs the guest: the sign-in that met a passkey
    hint asks for it, the guest flow runs once, and the sign-in runs once more, whatever the
    unlock answered (login, unlock, login)."""
    rig.broker.unlock_reply = unlock
    rig.broker.passkey_hint = True
    run = await rig.run()
    assert rig.events(run.run_id, "login_needed") == []
    assert sign_in_ops(rig) == ["login", "unlock_guest", "login"]
    assert rig.broker.guest_asks == [True, False]
    assert guest_sessions(rig) == 1
    assert rig.state(run.run_id) == "running"


async def test_broker_error_interrupts(rig):
    rig.broker.login_reply = {"error": "unavailable"}
    run = await rig.run()
    assert rig.state(run.run_id) == "interrupted"
    assert not rig.driver.sessions
    assert rig.log.index_of("revoke", run.run_id) < rig.log.index_of(
        "exit_proof", after=rig.log.index_of("revoke", run.run_id))


async def test_close_session_on_every_path(rig):
    """Every driver session the controller opens is closed: after a broker reply, a broker
    error, a timeout, a cancellation, the run's end, a profile switch and a live-view start."""
    def balanced():
        opened = [e[2] for e in rig.log if e[0] == "open_session"]
        closed = [e[2] for e in rig.log if e[0] == "close_session"]
        return opened and sorted(opened) == sorted(set(closed))
    run = await rig.run(sites=(("sa", "account"), ("sp", "public")))      # broker reply
    assert balanced() and not rig.driver.sessions
    rig.broker.login_reply = {"status": "failed", "reason": "timeout"}     # a timeout
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Site A"), obs)
    assert balanced() and not rig.driver.sessions
    obs = await observe(rig, run.run_id)                                    # a profile switch
    await act(rig, run.run_id, "navigate", handle_of(obs, "Public B"), obs)
    assert balanced() and not rig.driver.sessions
    # A cancellation and a live-view start during a hung login.
    gate = asyncio.Event()

    async def hang(args):
        await gate.wait()
    rig.broker.login_hook = hang
    obs = await observe(rig, run.run_id)
    nav = asyncio.create_task(act(rig, run.run_id, "navigate", handle_of(obs, "Site A"), obs))
    await asyncio.sleep(0.02)
    assert rig.driver.sessions
    ctl_mod.DRAIN_S, saved = 0.02, ctl_mod.DRAIN_S
    try:
        await rig.ctl.request_live(run.run_id, "assist")
    finally:
        ctl_mod.DRAIN_S = saved
    with pytest.raises(ProtocolError):
        await nav
    assert balanced() and not rig.driver.sessions
    rig.broker.login_hook = None

    async def expose():
        pass
    await rig.ctl.begin_live(run.run_id, expose)        # a capture session, ended by the run's end
    assert rig.driver.sessions
    await rig.ctl.cancel(run.run_id)
    assert balanced() and not rig.driver.sessions
    # A broker error.
    rig.broker.login_reply = {"error": "internal"}
    run2 = await rig.run(spec(request_id="rq2"))
    assert rig.state(run2.run_id) == "interrupted" and balanced() and not rig.driver.sessions


async def test_forced_close_leaves_no_session(rig):
    rig.driver.close_modes = {"login": "forced"}
    run = await rig.run()       # the broker's reply says nothing: the forced close is noticed
    assert rig.browser.session is None          # the driver closed the browser (K1)
    assert [e.outcome for e in rig.events(run.run_id, "login_needed")][-1] == "timeout"
    assert rig.state(run.run_id) == "running"
    opens = rig.log.kinds().count("open")
    rig.driver.close_modes = {}
    obs = await observe(rig, run.run_id)        # the next step opens a new session
    assert rig.log.kinds().count("open") == opens + 1
    assert rig.browser.session.generation == 2 and obs.text == "No page is open yet."


async def test_unconfirmed_close_interrupts_after_exit_proof(rig, monkeypatch):
    rig.driver.close_mode = "unconfirmed"
    run = await rig.run()
    # Its release is unconfirmed (a driver session remains): it ends interrupted, saying so,
    # and keeps the lease until the session is retired after the proof (ruling E-T12-2 = B1).
    assert rig.state(run.run_id) == "interrupted"
    assert rig.events(run.run_id, "run_interrupted")[-1].outcome == "release_unconfirmed"
    assert rig.ctl.active is not None and rig.driver.sessions
    queued = await rig.ctl.start_run(spec(request_id="next"))
    await rig.settle()
    assert rig.state(queued[0].run_id) == "queued"
    rig.driver.close_mode = "drained"
    async with rig.ctl._lock:
        assert await rig.ctl._teardown(rig.ctl.active)
        await rig.ctl._finalize(rig.ctl.active, True)
    await rig.settle()
    assert rig.state(run.run_id) == "interrupted"
    assert rig.state(queued[0].run_id) == "running"


async def test_unconfirmed_exit_keeps_the_lease(rig):
    run = await rig.run()
    rig.browser.force_fail = True
    await rig.ctl.cancel(run.run_id)
    # Not proved stopped: the cancel ends interrupted (not cancelled) and the lease stays.
    assert rig.state(run.run_id) == "interrupted" and rig.ctl.active is not None
    with pytest.raises(ProtocolError, match="interrupted"):
        await observe(rig, run.run_id)
    queued, _ = await rig.ctl.start_run(spec(request_id="waits"))
    await rig.settle()
    assert rig.state(queued.run_id) == "queued"
    rig.browser.force_fail = False
    async with rig.ctl._lock:
        assert await rig.ctl._teardown(rig.ctl.active)
        await rig.ctl._finalize(rig.ctl.active, True)
    await rig.settle()
    assert rig.state(run.run_id) == "interrupted" and rig.state(queued.run_id) == "running"


# --------------------------------------------------------------------------- ends


async def test_cancel_idempotent(rig):
    run = await rig.run()
    rig.page().show(A_START, "Start", [dict(role="link", name="Invoice", href=A_PAGE)])
    obs = await observe(rig, run.run_id)
    rig.downloader.gate = asyncio.Event()
    download = asyncio.create_task(download_(rig, run.run_id, handle_of(obs, "Invoice")))
    await asyncio.sleep(0.01)
    states = []
    real_revoke = rig.proxy.revoke

    async def revoke(run_id):
        states.append(rig.state(run_id))
        await real_revoke(run_id)
    rig.proxy.revoke = revoke
    cancelled = await rig.ctl.cancel(run.run_id)
    with pytest.raises(ProtocolError, match="interrupted"):
        await download
    assert cancelled.state == "cancelled" and states == ["finishing"]
    assert rig.log.index_of("revoke", run.run_id) < rig.log.index_of("close", "account", "sa")
    assert rig.log.index_of("exit_proof", after=rig.log.index_of("close", "account", "sa")) > 0
    assert (await rig.ctl.cancel(run.run_id)).state == "cancelled"
    assert len(rig.events(run.run_id, "run_cancelled")) == 1
    queued_a, _ = await rig.ctl.start_run(spec(request_id="q-a"))
    await rig.settle()
    queued_b, _ = await rig.ctl.start_run(spec(request_id="q-b"))
    await rig.settle()
    opens = rig.log.kinds().count("open")
    assert (await rig.ctl.cancel(queued_b.run_id)).state == "cancelled"
    assert rig.log.kinds().count("open") == opens


@pytest.mark.parametrize("how", ["cancel", "proxy_restart", "browser_death"])
async def test_interrupt_closes_browser(rig, how):
    run = await rig.run()
    seen = []
    real_revoke = rig.proxy.revoke

    async def revoke(run_id):
        seen.append(("revoke", rig.state(run_id)))
        await real_revoke(run_id)
    rig.proxy.revoke = revoke
    if how == "cancel":
        await rig.ctl.cancel(run.run_id)
    elif how == "proxy_restart":
        rig.proxy.restart()
        await rig.pump()
    else:
        rig.page().closed = True
        with pytest.raises(ProtocolError, match="interrupted"):
            await observe(rig, run.run_id)
    await rig.settle()
    assert seen == [("revoke", "finishing")]
    assert rig.browser.session is None and ("exit_proof", True) in rig.log
    assert rig.state(run.run_id) == ("cancelled" if how == "cancel" else "interrupted")


async def test_proxy_restart_interrupts_run(rig):
    run = await rig.run()
    installs = rig.log.kinds().count("install")
    rig.proxy.restart()
    await rig.pump()
    assert rig.state(run.run_id) == "interrupted"
    assert rig.log.kinds().count("install") == installs     # never re-installed
    assert rig.events(run.run_id, "run_interrupted")[-1].outcome == "proxy_restarted"
    notices = rig.store.notices_after(0)
    assert [n.kind for n in notices if n.run_id == run.run_id] == ["run_interrupted"]


async def test_install_racing_a_proxy_restart_is_refused(rig):
    """A command that reinstalls (here: a search switches to the public session) after the
    proxy restarted, before any poll, is refused by the proxy and interrupts the run."""
    run = await rig.run()
    rig.proxy.restart()
    with pytest.raises(ProtocolError):
        await rig.ctl.search(run.run_id, "invoices")
    await rig.settle()
    assert rig.state(run.run_id) == "interrupted"
    assert rig.proxy.policy is None


async def test_restart_no_resume(tmp_path):
    """A new controller process interrupts every run the previous one left, of every state,
    with one durable notice each, ends each at the broker, and starts no browser."""
    first = Rig(tmp_path)
    ids = {}
    for state in ("starting", "running", "waiting_approval", "waiting_live", "live",
                  "finishing", "queued"):
        run, _ = first.store.create_run(spec(request_id=f"rq-{state}"), f"run-{state}")
        if state != "queued":
            first.store.transition(run.run_id, "queued", state)
        ids[state] = run.run_id
    first.close()
    second = Rig(tmp_path)
    await second.start()
    try:
        for run_id in ids.values():
            assert second.state(run_id) == "interrupted"
        notices = [n for n in second.store.notices_after(0) if n.kind == "run_interrupted"]
        assert sorted(n.run_id for n in notices) == sorted(ids.values())
        assert ("exit_proof", True) in second.log
        assert second.log.kinds().count("open") == 0
        ended = [e[2] for e in second.log if e[:2] == ("broker", "end_run")]
        assert sorted(ended) == sorted(ids.values())
        assert second.log.index_of("exit_proof") < second.log.index_of("broker", "end_run")
        assert len(second.events(ids["running"], "run_interrupted")) == 1
    finally:
        await second.ctl.shutdown()
        second.close()


async def test_start_withdraws_a_surviving_policy_first(tmp_path):
    first = Rig(tmp_path)
    first.store.create_run(spec(request_id="old"), "run-old")
    first.store.transition("run-old", "queued", "running")
    first.close()
    r = Rig(tmp_path)
    r.proxy.policy = type("P", (), {"run_id": "run-old", "revision": 3})()
    order = []
    real_revoke = r.proxy.revoke

    async def revoke(run_id):
        order.append(("revoke", run_id, r.store.get_run("run-old").state))
        await real_revoke(run_id)
    r.proxy.revoke = revoke
    r.browser.force_fail = True
    ctl_mod.RECOVER_RETRY_S, saved = 0.01, ctl_mod.RECOVER_RETRY_S
    try:
        starting = asyncio.create_task(r.ctl.start())
        await asyncio.sleep(0.05)
        assert not starting.done() and r.state("run-old") == "running"   # no exit proof yet
        with pytest.raises(ProtocolError, match="not_ready"):
            await r.ctl.start_run(spec(request_id="new"))
        r.browser.force_fail = False
        await asyncio.wait_for(starting, 2)
    finally:
        ctl_mod.RECOVER_RETRY_S = saved
    assert order[0] == ("revoke", "run-old", "running")
    assert r.log.index_of("revoke", "run-old") < r.log.index_of("exit_proof", True)
    assert r.log.index_of("exit_proof", True) < r.log.index_of("broker", "end_run", "run-old")
    assert r.state("run-old") == "interrupted"
    await r.ctl.shutdown()
    r.close()


class BrokerShim:
    """The controller's broker calls, onto a real ``Broker`` (fake SDK, fake driver)."""

    def __init__(self, broker_rig, log):
        self.r, self.log = broker_rig, log

    async def import_notices(self, store=None):
        self.log.append(("broker", "import_notices", None, None))

    def __getattr__(self, op):
        async def call(*args, **kw):
            names = {"end_run": ("run_id",), "begin_run": ("run_id", "account_site_ids")}[op]
            args = dict(zip(names, args), **kw)
            self.log.append(("broker", op, args.get("run_id"), None))
            return await self.r.call(op, **args)
        return call


async def test_start_ends_interrupted_runs_at_broker(tmp_path):
    """Every run interrupted at start gets the broker's end_run before any new run starts; a
    site whose capture was left unreviewed then accepts the next begin_capture (real broker)."""
    from brokerfakes import Rig as BrokerRig
    (tmp_path / "broker").mkdir()
    broker = BrokerRig(tmp_path / "broker")
    await broker.setup()
    begun = await broker.call("begin_capture", run_id="run", site_id="site",
                              driver_session="driver", purpose="login")
    assert "capture_id" in begun
    await broker.call("begin_run", run_id="run2", account_site_ids=["site"])
    assert await broker.call("begin_capture", run_id="run2", site_id="site",
                             driver_session="driver", purpose="login") == {"error": "conflict"}
    first = Rig(tmp_path)
    first.store.create_run(spec(request_id="old"), "run")
    first.store.transition("run", "queued", "live")
    first.close()
    r = Rig(tmp_path)
    r.ctl.broker = BrokerShim(broker, r.log)
    await r.start()
    try:
        assert r.log.index_of("broker", "end_run", "run") >= 0
        assert r.state("run") == "interrupted"
        again = await broker.call("begin_capture", run_id="run2", site_id="site",
                                  driver_session="driver", purpose="login")
        assert "capture_id" in again
    finally:
        await r.ctl.shutdown()
        r.close()
        broker.journal.close()


# --------------------------------------------------------------------------- records, listing


async def test_records_requester_scope(rig):
    run = await rig.run(spec(requester="finance"))
    rig.page().show(A_START, "Start", [dict(role="link", name="Invoice", href=A_PAGE)])
    obs = await observe(rig, run.run_id)
    rec = await download_(rig, run.run_id, handle_of(obs, "Invoice"))
    await rig.ctl.ack_delivery(rec.file_id, {"requester_id": "finance", "delivery_id": "d1",
                                             "outcome": "deposited"})
    await rig.ctl.finish(run.run_id, manifest(summary_masked="Token Zq81Yt7Wm2Pk5Rn9Xs4Bv6Cd3Lh0Fg",
                                              file_ids=(rec.file_id,)))
    since = rig.clock() - __import__("datetime").timedelta(days=1)
    mine = await rig.ctl.records("finance", since)
    assert [r.run_id for r in mine] == [run.run_id]
    assert mine[0].file_ids == (rec.file_id,)
    assert mine[0].delivery_outcomes == ((rec.file_id, "d1", "deposited"),)
    assert "Zq81Yt7Wm2Pk5Rn9Xs4Bv6Cd3Lh0Fg" not in mine[0].summary_masked
    assert await rig.ctl.records("someone-else", since) == []


async def test_finish_checks_its_manifest(rig):
    run = await rig.run()
    other = await rig.run(spec(request_id="other"))
    with pytest.raises(ProtocolError, match="unsupported"):
        await rig.ctl.finish(run.run_id, manifest(source_ids=("src_never",)))
    with pytest.raises(ProtocolError, match="unsupported"):
        await rig.ctl.finish(run.run_id, manifest(file_ids=("f-unknown",)))
    assert rig.state(run.run_id) == "running"
    await rig.ctl.finish(run.run_id, manifest(expectation_status="mismatch"))
    assert rig.events(run.run_id, "run_finished")[-1].outcome == "completed"
    with pytest.raises(ProtocolError, match="conflict"):
        await rig.ctl.finish(run.run_id, manifest())
    await rig.settle()
    assert rig.state(other.run_id) == "running"


async def test_no_run_duration_kill(rig):
    run = await rig.run()
    rig.clock.advance(25 * 60)
    await rig.pump()
    assert rig.state(run.run_id) == "running"
    rig.page().show(A_START, "Still here")
    assert (await observe(rig, run.run_id)).text.startswith("Still here")


async def test_active_listing(rig):
    first = await rig.run(spec(request_id="a1", requester="finance", sites=(("sa", "account"),
                                                                             ("sp", "public"))))
    second = await rig.run(spec(request_id="b1", requester="travel"))
    third = await rig.run(spec(request_id="c1", requester="finance"))

    async def listing():
        before = len(rig.log)
        rows = await rig.ctl.list_active()
        assert len(rig.log) == before          # the route performs no run action
        return [(r.run_id, r.request_id, r.state) for r in rows]
    assert await listing() == [(first.run_id, "a1", "running"), (second.run_id, "b1", "queued"),
                               (third.run_id, "c1", "queued")]
    obs = await observe(rig, first.run_id)                       # a profile switch
    await act(rig, first.run_id, "navigate", handle_of(obs, "Public B"), obs)
    assert [r[2] for r in await listing()] == ["running", "queued", "queued"]
    await rig.ctl.request_live(first.run_id, "assist")          # a live view
    assert [r[2] for r in await listing()] == ["waiting_live", "queued", "queued"]
    await rig.ctl.live_complete(first.run_id, "op")
    await rig.ctl.cancel(third.run_id)
    await rig.ctl.finish(first.run_id, manifest())
    await rig.settle()
    assert await listing() == [(second.run_id, "b1", "running")]


# --------------------------------------------------------------------------- sources


async def test_sources_public_session_only(rig):
    long_url = "https://pub.example/" + "a" * 800
    ow = await rig.run(spec(request_id="ow", mode="open_web", sites=()))
    contents = rig.browser.contents
    q = ctl_mod.SEARCH_ORIGIN + ctl_mod.SEARCH_PATH
    contents[q + "invoices"] = dict(text="Results", title="invoices - Search", elements=[
        dict(role="link", name="Article", href="https://news.example/a?ref=1"),
        dict(role="link", name="Plain", href="http://plain.example/"),
        dict(role="link", name="Long", href=long_url)])
    contents["https://news.example/a?ref=1"] = dict(final="https://news.example/article",
                                                   text="Article text", title="An article")
    results = await rig.ctl.search(ow.run_id, "invoices")
    assert results.source_id
    await act(rig, ow.run_id, "navigate", handle_of(results, "Article"), results)
    article = await observe(rig, ow.run_id)
    assert article.source_id and article.source_id != results.source_id
    again = await observe(rig, ow.run_id)
    assert again.source_id == article.source_id                 # same address, same id
    rig.page().show("https://news.example/article#part-2", "Client route")
    routed = await observe(rig, ow.run_id)
    assert routed.source_id not in (None, article.source_id)
    results = await rig.ctl.search(ow.run_id, "invoices")
    await act(rig, ow.run_id, "navigate", handle_of(results, "Plain"), results)
    assert (await observe(rig, ow.run_id)).source_id is None      # http
    results = await rig.ctl.search(ow.run_id, "invoices")
    await act(rig, ow.run_id, "navigate", handle_of(results, "Long"), results)
    assert (await observe(rig, ow.run_id)).source_id is None      # over 800 bytes
    sources = await rig.ctl.sources(ow.run_id)
    assert [s.url for s in sources] == [q + "invoices", "https://news.example/article",
                                        "https://news.example/article#part-2"]
    assert sources[1].title_masked == "An article"
    assert {e.text_masked for e in rig.events(ow.run_id)}.isdisjoint({s.url for s in sources})
    assert not any("news.example/article" in json.dumps(e.to_wire()) for e in rig.events(ow.run_id))
    assert "news.example/article" not in json.dumps(article.to_wire())
    with pytest.raises(ProtocolError, match="unsupported"):
        await rig.ctl.finish(ow.run_id, manifest(source_ids=("src_forged",)))
    await rig.ctl.finish(ow.run_id, manifest(source_ids=(article.source_id,)))
    assert [s.source_id for s in await rig.ctl.sources(ow.run_id)] == [s.source_id for s in sources]
    record = (await rig.ctl.records("finance", rig.clock() - __import__("datetime").timedelta(1)))[0]
    assert "news.example" not in json.dumps(record.to_wire())
    # An account session's pages never get a source; another run's sources are not listed.
    await rig.settle()
    acct = await rig.run(spec(request_id="acct"))
    rig.page().show(A_START, "Account page")
    assert (await observe(rig, acct.run_id)).source_id is None
    assert await rig.ctl.sources(acct.run_id) == []


async def test_sources_bounded_per_run(rig, monkeypatch):
    monkeypatch.setattr(ctl_mod, "MAX_SOURCES", 2)
    ow = await rig.run(spec(request_id="ow", mode="open_web", sites=()))
    ids = []
    for n in range(3):
        await rig.ctl.search(ow.run_id, f"q{n}")
        rig.page().show(f"https://p{n}.example/", "Page")
        ids.append((await observe(rig, ow.run_id)).source_id)
    assert ids[2] is None and len(await rig.ctl.sources(ow.run_id)) == 2


async def test_search_is_public_even_on_a_signed_in_search_host(rig):
    """S4-7 (r1 Astra 2): a run that holds the search host as a site with account access
    still searches in the public profile — no account cookie, no check."""
    run = await rig.run(sites=(("sa", "account"), ("sg", "account")))
    rig.browser.contents[ctl_mod.SEARCH_ORIGIN + ctl_mod.SEARCH_PATH + "march+invoice"] = dict(
        text="Results", title="Search")
    opened = len(rig.log)
    obs = await rig.ctl.search(run.run_id, "march invoice")
    assert (rig.browser.session.kind, rig.browser.session.site_id) == ("public", None)
    assert obs.text.startswith("Results")
    assert not [e for e in rig.log[opened:] if e[0] == "open" and e[1] == "account"]
    assert not rig.events(run.run_id, "judgement")


async def test_search_is_public_and_free(rig):
    """S4-7: the app's search is always public and free — an account run whose site set does
    not hold the search origin searches too, in the public profile, with no question."""
    run = await rig.run(spec(request_id="lone"))
    assert rig.browser.session.kind == "account"
    rig.browser.contents[ctl_mod.SEARCH_ORIGIN + ctl_mod.SEARCH_PATH + "march+invoice"] = dict(
        text="Results", title="Search")
    obs = await rig.ctl.search(run.run_id, "march invoice")
    assert rig.browser.session.kind == "public" and obs.text.startswith("Results")
    assert obs.source_id                         # a public-session https page
    assert not rig.events(run.run_id, "judgement")
    assert not [e for e in rig.log if e[0] == "broker" and e[1] in ("login", "unlock_guest")
                and rig.log.index(e) > rig.log.index_of("open", "public")]


# --------------------------------------------------------------------------- downloads


def _hop(url):
    return RequestMeta("GET", url, resource_type="document", is_navigation=True)


async def test_download_refusal_is_blocked(rig):
    """S4-5: a download hop the policy refuses (the floor) is ``blocked`` — never an operator
    question."""
    run = await rig.run()
    rig.page().show(A_START, "Start", [dict(role="link", name="Invoice", href=A_PAGE)])
    obs = await observe(rig, run.run_id)
    rig.downloader.refuse = DownloadRefused("blocked")
    with pytest.raises(ProtocolError, match="blocked"):
        await download_(rig, run.run_id, handle_of(obs, "Invoice"))
    assert rig.state(run.run_id) == "running"


async def test_browser_download_reaches_only_the_download_reply(rig):
    run = await rig.run()
    els = rig.page().show(A_START, "Billing", [dict(role="button", name="Download PDF"),
                                               dict(role="button", name="Other")])
    started = []

    class Download:
        def __init__(self, name):
            self.name = name

        async def cancel(self):
            started.append(("cancel", self.name))

        async def delete(self):
            started.append(("delete", self.name))
    rig.page().download_for[els[0]] = Download("via-download")
    rig.page().download_for[els[1]] = Download("via-click")
    obs = await observe(rig, run.run_id)
    rec = await download_(rig, run.run_id, handle_of(obs, "Download PDF"))
    assert rec.mime == "application/pdf" and ("capture_download",) in rig.log
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "click", handle_of(obs, "Other"), obs)
    await rig.settle()
    assert started == [("cancel", "via-click"), ("delete", "via-click")]
    assert rig.log.kinds().count("capture_download") == 1


# --------------------------------------------------------------------------- windows, notices


async def test_first_browser_closes_start_window(rig):
    chrome_running = []
    real_close = rig.proxy.close_window

    async def close_window(token):
        chrome_running.append(rig.browser.session is not None)
        await real_close(token)
    rig.proxy.close_window = close_window
    start_token = rig.proxy.token
    run = await rig.run(sites=(("sa", "account"), ("sp", "public")))
    first_close = rig.log.index_of("close_window")
    assert rig.log[first_close] == ("close_window", start_token)
    assert first_close < rig.log.index_of("open")
    assert chrome_running == [False] and not rig.proxy.window_open
    # A proxy that opened its window again (as at a proxy start) while Chrome runs: the next
    # session's window close waits until the browser is closed.
    rig.proxy._open()
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Public B"), obs)
    assert chrome_running == [False, False]


async def test_notices_imported(rig):
    starts = [e for e in rig.log if e[:2] == ("broker", "import_notices")]
    assert len(starts) == 1
    rig.store.add_notice(SafeNotice(0, "credential_unknown", None, "sa", "warned", rig.clock(),
                                    dedupe_key="broker:7"))
    rows = await rig.ctl.notices(0)
    assert [e for e in rig.log if e[:2] == ("broker", "import_notices")][1:]
    assert rig.log.index_of("broker", "import_notices", after=rig.log.index(starts[0])) >= 0
    assert [n.kind for n in rows] == ["credential_unknown"]


# --------------------------------------------------------------------------- forget, pairing


async def test_forget_closes_and_never_touches_the_vault(rig):
    run = await rig.run()
    binding = ProviderBinding("https://idp.example", "a" * 64, "b" * 64, "c" * 64)
    binding_site = replace(rig.store.get_site("sa"), provider_bindings=(binding,))
    rig.store.put_site(binding_site)
    rig.store.put_binding_key("sa", "k-old")
    assert rig.ctl.bindings_lost() == {"sa"}     # learned under another key than the current
    reply = await rig.ctl.forget("sa", "op")
    assert reply == {"site_id": "sa", "status": "forgotten"}
    assert rig.state(run.run_id) == "interrupted"
    assert rig.browser.forgotten == ["sa"]
    forget = rig.log.index_of("forget_profile", "sa")
    assert rig.log.index_of("revoke", run.run_id) < forget < rig.log.index_of("broker", "forget_site")
    assert rig.store.get_site("sa").provider_bindings == ()
    assert rig.store.binding_keys() == {} and rig.ctl.bindings_lost() == set()
    assert not [e for e in rig.log if e[0] == "broker" and "delete" in e[1]]


async def test_binding_without_a_recorded_key_needs_a_live_login(rig):
    """A provider binding with no recorded proxy key (none was ever written for it) is not
    shown as bound: its tags cannot be told to match the proxy's current key."""
    binding = ProviderBinding("https://idp.example", "a" * 64, "b" * 64, "c" * 64)
    site = rig.store.get_site("sa")
    rig.store.put_site(replace(site, provider_bindings=(binding,), revision=site.revision + 1))
    assert rig.store.binding_keys() == {}
    assert rig.ctl.bindings_lost() == {"sa"}
    rig.store.put_binding_key("sa", rig.proxy.binding_key_id)
    assert rig.ctl.bindings_lost() == set()


async def test_pairing_revocation_starts_no_queued_run(rig):
    first = await rig.run(spec(request_id="p1"))
    second = await rig.run(spec(request_id="p2"))
    third = await rig.run(spec(request_id="p3"))
    opens = rig.log.kinds().count("open")
    rig.ctl.pairing.arm()
    await rig.settle()
    assert [rig.state(r.run_id) for r in (first, second, third)] == ["interrupted"] * 3
    assert rig.log.kinds().count("open") == opens          # no queued run was leased
    fresh = await rig.run(spec(request_id="p4"))
    assert fresh.state == "running"


# --------------------------------------------------------------------------- the /v1 API


async def test_api_routes_through_the_controller(rig, aiohttp_client):
    """The real controller behind ``make_app``: the handlers await it (call style)."""
    from apihelpers import headers
    from casa_browser.api import make_app
    from casa_browser.protocol import spec_digest
    rig.ctl.pairing.arm()
    await rig.settle()
    secret = rig.ctl.pairing.claim("Casa", "peer")
    client = await aiohttp_client(make_app(rig.ctl))
    h = headers(secret)
    members = {"request_id": "api-1", "requester_id": "finance", "mode": "account",
               "task_text": "t", "task_kind": "k", "sites": [{"site_id": "sa", "access": "account"}],
               "result_expectation": ""}
    body = {**members, "admission": {"origin_kind": "operator_telegram", "origin_ref": "m",
                                     "admitted_spec_digest": spec_digest(members)}}
    resp = await client.post("/v1/runs", json=body, headers=h)
    assert resp.status == 201
    run_id = (await resp.json())["run_id"]
    await rig.settle()
    assert (await (await client.get(f"/v1/runs/{run_id}", headers=h)).json())["state"] == "running"
    obs = await client.post(f"/v1/runs/{run_id}/observe", json={}, headers=h)
    assert obs.status == 200 and "Open site: Site A" in json.dumps(await obs.json())
    listing = await client.get("/v1/runs?state=active", headers=h)
    assert [r["run_id"] for r in (await listing.json())["runs"]] == [run_id]
    events = await (await client.get(f"/v1/runs/{run_id}/events?after=0", headers=h)).json()
    assert "run_started" in [e["kind"] for e in events["events"]]
    cancel = await client.post(f"/v1/runs/{run_id}/cancel", json={}, headers=h)
    assert (await cancel.json())["state"] == "cancelled"
    again = await client.post(f"/v1/runs/{run_id}/observe", json={}, headers=h)
    assert again.status == 409 and (await again.json()) == {"error": "conflict"}


async def test_public_session_live_views(rig):
    """An open-web run may pause for an assist view (no lease, no window); a login view needs
    an account session."""
    ow = await rig.run(spec(request_id="ow", mode="open_web", sites=()))
    with pytest.raises(ProtocolError, match="unsupported"):
        await rig.ctl.request_live(ow.run_id, "login")
    await rig.ctl.request_live(ow.run_id, "assist")

    async def expose():
        pass
    before = len(rig.log)
    await rig.ctl.begin_live(ow.run_id, expose)
    assert rig.state(ow.run_id) == "live"
    assert not [e for e in rig.log[before:] if e[0] in ("begin_manual", "open_session")]
    await rig.ctl.live_done(ow.run_id)
    assert rig.state(ow.run_id) == "running"


async def test_failed_exposure_offers_no_live_view(rig):
    run = await rig.run()
    await rig.ctl.request_live(run.run_id, "assist")

    async def expose():
        raise RuntimeError("ingress gone")
    with pytest.raises(ProtocolError, match="not_ready"):
        await rig.ctl.begin_live(run.run_id, expose)
    assert rig.state(run.run_id) == "running" and rig.proxy.policy.manual_lease is None
    assert rig.events(run.run_id, "live_unavailable")[-1].outcome == "internal"


async def test_command_queued_before_a_repair_never_acts(rig):
    """A model command waiting for the lock when the operator re-pairs is refused once it gets
    the lock: the revocation interrupts the run first (r1 S1)."""
    run = await rig.run()
    rig.page().show(A_START, "Account page")
    await rig.ctl._lock.acquire()
    queued = asyncio.create_task(rig.ctl.observe(run.run_id))
    queued_register = asyncio.create_task(rig.ctl.register_site_detailed({
        "display_name": "New", "origins": ("https://n.example",), "login_origins": (),
        "vault_item_id": None, "start_page": None, "operator_ref": "op"}))
    await asyncio.sleep(0.01)
    rig.ctl.pairing.arm()
    rig.ctl._lock.release()
    with pytest.raises(ProtocolError, match="interrupted"):
        await queued
    with pytest.raises(ProtocolError, match="interrupted"):
        await queued_register
    await rig.settle()
    assert [s.display_name for s in rig.store.list_sites()].count("New") == 0
    assert rig.state(run.run_id) == "interrupted"
    assert not [e for e in rig.events(run.run_id) if e.kind == "action"]


# --------------------------------------------------------------------------- review r1 regressions


class LocalProxy:
    """The real ``EnforcingAddon`` behind the real control dispatch (``Proxy.dispatch``), with
    hooks to hold or fail one control call."""

    def __init__(self, path):
        import json as _json
        from casa_browser.proxy_addon import EnforcingAddon
        from casa_browser.proxy_client import ProxyClient
        from casa_browser.proxy_main import Proxy
        self._json = _json
        self.addon = EnforcingAddon()
        self.server = Proxy(addon=self.addon, confdir=path, control_path=path / "unused",
                            controller_uid=1103)
        self.client = ProxyClient()
        self.client._call = self._call
        self.hold: dict = {}
        self.lose_ack: set = set()
        self.calls: list = []

    async def _call(self, op, **args):
        from casa_browser.proxy_client import ControlError
        self.calls.append(op)
        if op in self.hold:
            await self.hold[op].wait()
        reply = self.server.dispatch(self._json.dumps(dict(op=op, **args)).encode())
        if op in self.lose_ack:
            raise ControlError("proxy_unavailable")   # applied, acknowledgement lost
        if not reply.get("ok"):
            raise ControlError(reply["error"])
        return reply

    def __getattr__(self, name):
        return getattr(self.client, name)


async def test_second_cancel_cannot_abort_a_teardown(tmp_path):
    """Two cancels arriving while a finish tears down (its revoke not yet sent) neither cut
    the teardown nor leave the proxy admitting: the run completes, revoked and closed."""
    r = Rig(tmp_path)
    proxy = LocalProxy(tmp_path)
    r.proxy = r.ctl.proxy = proxy
    await r.start()
    try:
        run = await r.run(spec(mode="open_web", sites=()))
        gate = asyncio.Event()
        proxy.hold["revoke"] = gate
        finishing = asyncio.create_task(r.ctl.finish(run.run_id, manifest()))
        await asyncio.sleep(0.02)
        assert r.state(run.run_id) == "finishing"
        first = asyncio.create_task(r.ctl.cancel(run.run_id))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(r.ctl.cancel(run.run_id))
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(first, second)
        assert (await finishing).state == "completed"
        assert proxy.calls.count("revoke") == 1 and proxy.addon.policy is None
        assert r.browser.session is None and r.ctl.active is None
    finally:
        await r.ctl.shutdown()
        r.close()


async def test_failed_driver_opening_is_retired_before_the_run_ends(tmp_path):
    """A login session whose opening failed (its lease acknowledgement lost) and whose cleanup
    stalls is not left behind: the run stays leased until the exit proof retires it, and the
    next account run gets the driver."""
    from casa_browser.login import DriverServer
    r = Rig(tmp_path)
    proxy = LocalProxy(tmp_path)
    r.proxy = r.ctl.proxy = proxy
    await r.start()
    driver = DriverServer(r.browser, tmp_path / "unused-driver", cleanup_timeout=0.01)
    driver.browser_close_timeout = 0.05
    r.ctl.driver = driver
    try:
        r.broker.login_reply = {"status": "logged_in", "reason": "completed"}
        run = await r.run()
        obs = await observe(r, run.run_id)
        proxy.lose_ack.add("begin_login")
        proxy.hold["end_login"] = asyncio.Event()       # never released
        with pytest.raises(ProtocolError, match="interrupted"):
            await act(r, run.run_id, "navigate", handle_of(obs, "Site A"), obs)
        await r.settle()
        assert not driver.open_sessions()
        assert r.state(run.run_id) == "interrupted"
        proxy.lose_ack.clear()
        proxy.hold.clear()
        second = await r.run(spec(request_id="second"))
        assert second.state == "running"
    finally:
        await r.ctl.shutdown()
        r.close()


async def test_cancelled_caller_cannot_abort_a_teardown(tmp_path):
    """However often the task that ends a run is cancelled (a dropped client, a shutdown), its
    teardown runs to the end: revoked, closed, terminal."""
    r = Rig(tmp_path)
    proxy = LocalProxy(tmp_path)
    r.proxy = r.ctl.proxy = proxy
    await r.start()
    try:
        run = await r.run(spec(mode="open_web", sites=()))
        gate = asyncio.Event()
        proxy.hold["revoke"] = gate
        finishing = asyncio.create_task(r.ctl.finish(run.run_id, manifest()))
        await asyncio.sleep(0.02)
        for _ in range(3):
            finishing.cancel()
            await asyncio.sleep(0.01)
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await finishing
        await r.settle()
        assert proxy.calls.count("revoke") == 1 and proxy.addon.policy is None
        assert r.state(run.run_id) == "completed" and r.browser.session is None
    finally:
        await r.ctl.shutdown()
        r.close()


async def test_no_run_admitted_while_a_repair_ends_runs(tmp_path):
    """While a re-pair is still tearing the active run down, a new run is refused (r2 S1)."""
    r = Rig(tmp_path)
    proxy = LocalProxy(tmp_path)
    r.proxy = r.ctl.proxy = proxy
    await r.start()
    try:
        gate = asyncio.Event()
        run = await r.run(spec(mode="open_web", sites=()))
        proxy.hold["revoke"] = gate
        r.ctl.pairing.arm()
        await asyncio.sleep(0.02)
        with pytest.raises(ProtocolError, match="not_ready"):
            await r.ctl.start_run(spec(request_id="during", mode="open_web", sites=()))
        gate.set()
        await r.settle()
        assert r.state(run.run_id) == "interrupted"
        from casa_browser.protocol import RUN_STATES
        assert all(x.spec.request_id != "during" for x in r.store.list_runs(RUN_STATES))
        after = await r.run(spec(request_id="after", mode="open_web", sites=()))
        assert after.state == "running"
    finally:
        gate.set()
        await r.ctl.shutdown()
        r.close()


async def test_profile_switch_needs_exit_proof(rig):
    """A switch whose old browser cannot be proved stopped installs no new session's policy:
    the run is interrupted (r2 S1)."""
    run = await rig.run(sites=(("sa", "account"), ("sp", "public")))
    obs = await observe(rig, run.run_id)
    rig.browser.force_fail = True
    before = len(rig.log)
    with pytest.raises(ProtocolError, match="interrupted"):
        await act(rig, run.run_id, "navigate", handle_of(obs, "Public B"), obs)
    assert not [e for e in rig.log[before:] if e[0] == "install"]
    assert not [e for e in rig.log[before:] if e[0] == "open"]
    assert rig.state(run.run_id) == "interrupted" and rig.ctl.active is not None
    rig.browser.force_fail = False
    async with rig.ctl._lock:
        assert await rig.ctl._teardown(rig.ctl.active)
        await rig.ctl._finalize(rig.ctl.active, True)
    assert rig.state(run.run_id) == "interrupted"


async def test_no_install_while_a_repair_is_pending(rig):
    """A command already past its own check when the operator re-pairs installs nothing."""
    await rig.run()
    installs = rig.log.kinds().count("install")
    rig.ctl._revoking = True           # Pairing.arm ran; its revocation task has not yet run
    try:
        with pytest.raises(ctl_mod._Dependency, match="pairing_revoked"):
            await rig.ctl._install(rig.ctl.active)
    finally:
        rig.ctl._revoking = False
    assert rig.log.kinds().count("install") == installs


@pytest.mark.parametrize("failure", ["expose", "end_capture"])
async def test_every_live_view_end_ends_its_capture(tmp_path, failure):
    """A failed exposure ends the capture the broker armed (the next login view can capture);
    a Done whose capture cannot be ended interrupts the run, and end_run retires it (r3 S2)."""
    from brokerfakes import Rig as BrokerRig
    from casa_browser.broker_client import BrokerClient
    (tmp_path / "b").mkdir()
    br = BrokerRig(tmp_path / "b")
    await br.broker.start()
    await br.call("register_capture", site_id="sa", login_origins=["https://login.a.example"],
                  operator_ref="op", title="Site A")
    r = Rig(tmp_path)
    client = BrokerClient(store=r.store)
    fail_end = []

    async def call(op, **args):
        if op == "end_capture" and fail_end:
            return {"error": "unavailable"}
        return await br.call(op, **args)
    client.call = call
    r.ctl.broker = client
    await r.start()
    exposures = []

    async def expose():
        exposures.append(True)

    async def broken():
        raise RuntimeError("ingress disconnected")
    try:
        run = await r.run()
        await r.ctl.request_live(run.run_id, "login")
        if failure == "expose":
            with pytest.raises(ProtocolError, match="not_ready"):
                await r.ctl.begin_live(run.run_id, broken)
            assert br.broker._captures == {} and r.state(run.run_id) == "running"
            await r.ctl.request_live(run.run_id, "login")
            await r.ctl.begin_live(run.run_id, expose)
            assert exposures == [True] and len(br.broker._captures) == 1
        else:
            await r.ctl.begin_live(run.run_id, expose)
            fail_end.append(True)
            with pytest.raises(ProtocolError, match="interrupted"):
                await r.ctl.live_complete(run.run_id, "op")
            await r.settle()
            assert r.state(run.run_id) == "interrupted"
            assert br.broker._captures == {}          # end_run retired it
            assert r.proxy.policy is None and not r.proxy.window_open
    finally:
        await r.ctl.shutdown()
        r.close()
        br.journal.close()


async def test_done_with_typed_values_ends_the_capture_and_resumes(tmp_path):
    """Slice 5g: Done alone (no review screen) ends a capture that holds typed values at the
    broker, and the run resumes."""
    from brokerfakes import Rig as BrokerRig
    from casa_browser.broker_client import BrokerClient
    (tmp_path / "b").mkdir()
    br = BrokerRig(tmp_path / "b")
    await br.broker.start()
    await br.call("register_capture", site_id="sa", login_origins=["https://login.a.example"],
                  operator_ref="op", title="Site A")
    r = Rig(tmp_path)
    client = BrokerClient(store=r.store)

    async def call(op, **args):
        return await br.call(op, **args)
    client.call = call
    r.ctl.broker = client
    await r.start()
    try:
        run = await r.run()
        br.driver.values = [("password", "Changed-Pw-91")]

        async def expose():
            pass
        await r.ctl.request_live(run.run_id, "login")
        await r.ctl.begin_live(run.run_id, expose)
        done = await r.ctl.live_done(run.run_id)
        assert done.state == "running" and br.broker._captures == {}
    finally:
        await r.ctl.shutdown()
        r.close()
        br.journal.close()


async def test_unacknowledged_end_run_holds_the_queue(rig, monkeypatch):
    """Ruling E-T12-2 = B1: an account run whose end the broker never acknowledged ends
    interrupted with a message for its requester (event and durable notice), and the next
    run waits until a retry gets the acknowledgement (r4 Terra S1)."""
    monkeypatch.setattr(ctl_mod, "BROKER_TRIES", 1)
    run = await rig.run()
    rig.broker.end_run_fail = 10 ** 6
    await rig.ctl.finish(run.run_id, manifest())
    assert rig.state(run.run_id) == "interrupted" and rig.ctl.active is not None
    event = rig.events(run.run_id, "run_interrupted")[-1]
    assert event.outcome == "release_unconfirmed" and "next run waits" in event.text_masked
    assert [n.kind for n in rig.store.notices_after(0) if n.run_id == run.run_id] == ["run_interrupted"]
    assert rig.events(run.run_id, "run_finished") == []
    since = rig.clock() - __import__("datetime").timedelta(days=1)
    assert [r.run_id for r in await rig.ctl.records("finance", since)] == [run.run_id]
    queued, _ = await rig.ctl.start_run(spec(request_id="waits"))
    await rig.settle()
    assert rig.state(queued.run_id) == "queued"
    rig.broker.end_run_fail = 0
    async with rig.ctl._lock:
        assert await rig.ctl._teardown(rig.ctl.active)
        await rig.ctl._finalize(rig.ctl.active, True)
    await rig.settle()
    assert rig.state(queued.run_id) == "running"


async def test_open_web_run_needs_no_broker_acknowledgement(rig):
    rig.broker.end_run_fail = 10 ** 6
    run = await rig.run(spec(request_id="ow", mode="open_web", sites=()))
    assert (await rig.ctl.finish(run.run_id, manifest())).state == "completed"
    assert rig.ctl.active is None


@pytest.mark.parametrize("purpose,session,failure", [
    (p, "account", f) for p in ("login", "assist")
    for f in ("begin_manual", "open_session", "begin_capture", "expose")
] + [("assist", "public", "expose")])
async def test_every_begin_live_failure_cleans_up(rig, purpose, session, failure):
    """Ruling E-T12-1 = A1: whatever fails while a live view is claimed — in any session kind,
    at any step — the view is not offered, everything it took is released (lease, window,
    capture session, broker capture, grant) and the run is back to running; a second claim
    then works."""
    if session == "public":
        run = await rig.run(spec(request_id="ow", mode="open_web", sites=()))
    else:
        run = await rig.run()
    from casa_browser.proxy_client import ControlError
    from casa_browser.broker import BrokerError

    async def boom(*a, **k):
        raise {"begin_manual": ControlError("proxy_unavailable"),
               "open_session": BrokerError("refused")}[failure]
    real_manual, real_open = rig.proxy.begin_manual, rig.driver.open_session
    if failure == "begin_manual":
        rig.proxy.begin_manual = boom
    elif failure == "open_session":
        async def capture_boom(kind, *a, **k):   # the view's capture session (the sign-in
            if kind == "capture":                # view's guest session before it opens)
                return await boom()
            return await real_open(kind, *a, **k)
        rig.driver.open_session = capture_boom
    elif failure == "begin_capture":
        rig.broker.capture_reply = {"error": "conflict"}
    exposed = []

    async def expose():
        if failure == "expose":
            raise RuntimeError("ingress gone")
        exposed.append(True)
    await rig.ctl.request_live(run.run_id, purpose)
    with pytest.raises(ProtocolError, match="not_ready"):
        await rig.ctl.begin_live(run.run_id, expose)
    assert rig.state(run.run_id) == "running" and rig.ctl.active.live is None
    assert rig.events(run.run_id, "live_unavailable")
    assert run.run_id not in rig.ctl.grants.grants
    assert not rig.driver.sessions
    if session == "account":
        assert rig.proxy.policy.manual_lease is None and not rig.proxy.window_open
        armed = failure == "expose"
        ended = [e for e in rig.log if e[:2] == ("broker", "end_capture")]
        assert len(ended) == (1 if armed else 0)
    rig.proxy.begin_manual, rig.driver.open_session = real_manual, real_open
    rig.broker.capture_reply = {"capture_id": "cap2"}
    await rig.ctl.request_live(run.run_id, purpose)

    async def fine():
        exposed.append(True)
    await rig.ctl.begin_live(run.run_id, fine)
    assert rig.state(run.run_id) == "live" and exposed == [True]


async def test_restart_retries_owed_end_run(tmp_path, monkeypatch):
    """The B1 rule across a controller restart (Task 13): a run whose release ended
    unconfirmed because the broker never acknowledged ``end_run`` is still owed it by the next
    controller — retried until acknowledged — and no queued run is leased before that."""
    monkeypatch.setattr(ctl_mod, "BROKER_TRIES", 1)
    monkeypatch.setattr(ctl_mod, "TEARDOWN_RETRY_S", 0.01)
    first = Rig(tmp_path)
    await first.start()
    run = await first.run()
    first.broker.end_run_fail = 10 ** 6
    await first.ctl.finish(run.run_id, manifest())
    assert first.state(run.run_id) == "interrupted"
    assert first.store.owed_releases() == [run.run_id]
    for task in list(first.ctl._tasks):      # the process dies: nothing of it retries
        task.cancel()
    await asyncio.sleep(0)
    first.close()

    second = Rig(tmp_path)
    second.broker.end_run_fail = 10 ** 6     # still unreachable after the restart
    await second.ctl.start()
    try:
        queued, _ = await second.ctl.start_run(spec(request_id="waits"))
        for _ in range(5):
            await asyncio.sleep(0.02)
        await second.settle()
        assert second.state(queued.run_id) == "queued"      # the queue waits for the broker
        ends = lambda: [e for e in second.log if e[:3] == ("broker", "end_run", run.run_id)]
        refused = len(ends())
        assert refused >= 2                                  # start's attempt and a retry
        second.broker.end_run_fail = 0
        for _ in range(200):
            await asyncio.sleep(0.01)
            if second.store.owed_releases() == []:
                break
        await second.settle()
        assert len(ends()) == refused + 1 and second.store.owed_releases() == []
        assert second.state(queued.run_id) == "running"
    finally:
        await second.ctl.shutdown()
        second.close()


async def test_restart_owes_a_failed_start_end_run(tmp_path, monkeypatch):
    """A run interrupted at start whose end_run fails is owed too: the queue waits."""
    monkeypatch.setattr(ctl_mod, "BROKER_TRIES", 1)
    monkeypatch.setattr(ctl_mod, "TEARDOWN_RETRY_S", 3600)
    first = Rig(tmp_path)
    first.store.create_run(spec(request_id="old"), "run-old")
    first.store.transition("run-old", "queued", "running")
    first.close()
    r = Rig(tmp_path)
    r.broker.end_run_fail = 10 ** 6
    await r.start()
    try:
        assert r.store.owed_releases() == ["run-old"]
        waiting = await r.run(spec(request_id="new"))
        assert waiting.state == "queued"
    finally:
        await r.ctl.shutdown()
        r.close()


async def test_second_restart_still_owes(tmp_path, monkeypatch):
    """d1 Astra S1: a restart while start's end_run is still awaited loses nothing — the owed
    row was written with the interruption itself, so the next start retries it."""
    monkeypatch.setattr(ctl_mod, "BROKER_TRIES", 1)
    first = Rig(tmp_path)
    first.store.create_run(spec(request_id="old"), "run-old")
    first.store.transition("run-old", "queued", "running")
    first.close()
    second = Rig(tmp_path)
    hang = asyncio.Event()
    real = second.broker.call

    async def call(op, **args):
        if op == "end_run":
            second.log.append(("broker", op, args.get("run_id"), None))
            await hang.wait()            # the broker never answers before the process dies
        return await real(op, **args)
    second.broker.call = call
    starting = asyncio.create_task(second.ctl.start())
    for _ in range(20):
        await asyncio.sleep(0)
    assert ("broker", "end_run", "run-old", None) in second.log and not starting.done()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    second.close()
    third = Rig(tmp_path)
    assert third.store.owed_releases() == ["run-old"]
    await third.start()
    try:
        assert third.log.count(("broker", "end_run", "run-old", None)) == 1
        assert third.store.owed_releases() == []
    finally:
        await third.ctl.shutdown()
        third.close()


async def test_start_retries_a_previous_processes_owed_release_at_once(tmp_path, monkeypatch):
    """An owed release left by a previous process is retried at start, not only by the slow
    retry loop: a broker that answers now frees the queue at once."""
    monkeypatch.setattr(ctl_mod, "TEARDOWN_RETRY_S", 3600)
    first = Rig(tmp_path)
    first.store.create_run(spec(request_id="old"), "run-old")
    first.store.transition("run-old", "queued", "interrupted")
    first.store.owe_release("run-old")
    first.close()
    r = Rig(tmp_path)
    await r.start()
    try:
        assert r.log.count(("broker", "end_run", "run-old", None)) == 1
        assert r.store.owed_releases() == []
        assert (await r.run(spec(request_id="new"))).state == "running"
    finally:
        await r.ctl.shutdown()
        r.close()


@pytest.mark.parametrize("value, level", [("debug", 10), ("info", 20), ("warning", 30),
                                          ("error", 40), (None, 20), ("bogus", 20)])
def test_controller_honours_log_level(value, level):
    """The ``log_level`` option sets the app's own loggers; libraries stay at warnings."""
    import logging

    from casa_browser.controller import configure_logging
    app_logger = logging.getLogger("casa_browser")
    before = app_logger.level
    try:
        assert configure_logging({} if value is None else {"log_level": value}) == level
        assert app_logger.level == level
        assert logging.getLogger("casa_browser.controller").getEffectiveLevel() == level
        assert logging.getLogger("aiohttp.server").getEffectiveLevel() >= logging.WARNING
    finally:
        app_logger.setLevel(before)


# --------------------------------------------------------------------------- the action gate
# (action model slice 2: S2-1 to S2-11, G1, G2; design §2–§3, F1–F9)

PAY = "https://a.example/pay"
ASK = {"verdict": "ask", "category": "payment", "reason": "pays the invoice",
       "question": {"text": "Pay €84.20 for the October invoice to Site A?",
                    "confirm_label": "Pay €84.20", "extra_options": ["Store credit"],
                    "screenshot": "required", "highlight": ["€84.20"], "text_incomplete": False}}
ASK_CHANGE = {"verdict": "ask", "category": "account_change", "reason": "saves a setting",
              "question": {"text": "Turn on paper invoices (€1.50 each)?", "confirm_label": "Turn on",
                           "extra_options": [], "screenshot": "none", "highlight": [],
                           "text_incomplete": False}}
SAME = {"verdict": "same", "reason": "nothing material changed", "text_incomplete": False}
CHANGED = {"verdict": "changed", "reason": "a fee appeared", "text_incomplete": False}


async def _gate_run(rig, elements, text="Invoice October\nTotal € 84,20", url=A_START):
    run = await rig.run()
    els = rig.page().show(url, text, elements)
    rig.mark = len(rig.log)            # the app's own start-page load is not an agent input
    return run, els


async def judged(rig, run_id, label, kind="click", **kw):
    obs = await observe(rig, run_id)
    result = await rig.ctl.act(run_id, Action(kind, handle_of(obs, label), obs.generation, **kw))
    wire = result.to_wire()
    assert wire["outcome"] == "judgement_required", wire
    return wire


async def verdict(rig, run_id, wire, v, *, digest=None):
    j = wire["judgement"]
    body = {"no_verdict": "check_unavailable"} if v is None else {
        "packet_sha256": digest or sha(j["packet"]), "verdict": v, "latency_ms": 4200}
    return await rig.ctl.apply_judgement(run_id, j["id"], body)


async def tap(rig, run_id, qid, choice="confirm", *, revision=None):
    q = await rig.ctl.get_question(run_id, qid)
    return await rig.ctl.answer_question(run_id, qid, {
        "revision": revision or q["revision"], "digest": q["digest"], "choice": choice,
        "operator_ref": "tg-op"})


def presses(rig, name=None):
    return [e for e in rig.log[getattr(rig, "mark", 0):] if e[0] in ("click", "enter", "select", "back", "goto", "fetch",
                                            "capture_download")
            and (name is None or (len(e) > 1 and e[1] == name))]


async def asked(rig, run, els, label="Pay now", v=ASK):
    wire = await judged(rig, run.run_id, label)
    reply = await verdict(rig, run.run_id, wire, v)
    assert reply["outcome"] == "asked", reply
    return reply["question_id"]


async def test_in_scope_action_without_verdict_never_runs(rig):
    """F1: a signed-in click returns its packet and runs nothing until a verdict for that
    packet's digest arrives; no verdict, or one outside the reply bounds, is check_unavailable."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    wire = await judged(rig, run.run_id, "Pay now")
    packet = json.loads(wire["judgement"]["packet"])
    assert packet["target"]["name"] == "Pay now" and packet["where"]["session"] == "signed_in"
    assert wire["judgement"]["packet"] == json.dumps(packet, sort_keys=True, ensure_ascii=False,
                                                     separators=(",", ":"))
    assert presses(rig) == [] and rig.state(run.run_id) == "running"
    assert (await verdict(rig, run.run_id, wire, None))["outcome"] == "check_unavailable"
    assert presses(rig) == []
    for bad in ({**GO, "reason": "x" * 601}, {**GO, "category": "browsing"},
                {"verdict": "ask", "category": "payment", "reason": "r"},
                {**ASK, "question": {**ASK["question"], "extra_options": ["a", "b", "c", "d"]}}):
        wire = await judged(rig, run.run_id, "Pay now")
        assert (await verdict(rig, run.run_id, wire, bad))["outcome"] == "check_unavailable"
    assert presses(rig) == []
    kinds = [e.outcome for e in rig.events(run.run_id, "judgement")]
    assert kinds == ["no_verdict"] * 5


async def test_verdict_for_another_digest_is_refused(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    wire = await judged(rig, run.run_id, "Pay now")
    with pytest.raises(ProtocolError, match="digest_mismatch"):
        await verdict(rig, run.run_id, wire, GO, digest="0" * 64)
    assert presses(rig) == []
    # The judgement is still open for its own digest.
    assert (await verdict(rig, run.run_id, wire, GO))["outcome"] == "done"
    assert len(presses(rig, "Pay now")) == 1


async def test_superseded_or_expired_judgement_is_stale(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    first = await judged(rig, run.run_id, "Pay now")
    await observe(rig, run.run_id)                          # any new command supersedes it
    with pytest.raises(ProtocolError, match="judgement_stale"):
        await verdict(rig, run.run_id, first, GO)
    second = await judged(rig, run.run_id, "Pay now")
    rig.clock.advance(181)                                  # past the 180 s lifetime
    with pytest.raises(ProtocolError, match="judgement_stale"):
        await verdict(rig, run.run_id, second, GO)
    assert presses(rig) == []


async def test_go_with_change_category_asks_without_confirm(rig):
    """F2: a go whose category is a change is a question with fixed copy and no confirm."""
    run, els = await _gate_run(rig, [dict(role="switch", name="Paper invoices")])
    wire = await judged(rig, run.run_id, "Paper invoices")
    reply = await verdict(rig, run.run_id, wire, {"verdict": "go", "category": "account_change",
                                                  "reason": "toggles a setting"})
    assert reply["outcome"] == "asked" and presses(rig) == []
    q = await rig.ctl.get_question(run.run_id, reply["question_id"])
    view = q["view"]
    assert view["confirm_offered"] is False and view["confirm_label"] == ""
    assert view["text"] == view["live_only_copy"]
    assert "Ellen was about to press “Paper invoices” on Site A" in view["text"]
    assert rig.state(run.run_id) == "waiting_approval"
    with pytest.raises(ProtocolError, match="unsupported"):
        await tap(rig, run.run_id, reply["question_id"])
    assert presses(rig) == []


async def test_look_first_runs_nothing(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Nu betalen")])
    wire = await judged(rig, run.run_id, "Nu betalen")
    reply = await verdict(rig, run.run_id, wire, {
        "verdict": "look_first", "category": "unclear", "reason": "unclear",
        "look_first": "find where the PDF is offered without paying"})
    assert reply == {"outcome": "look_first", "generation": reply["generation"],
                     "note": "find where the PDF is offered without paying"}
    assert presses(rig) == [] and rig.state(run.run_id) == "running"


async def test_go_rereads_target_facts_before_dispatch(rig):
    """F4 after a go: the label and handler change during the pointer movement; the guard's
    re-read sees it, nothing is pressed, and the step is judged afresh."""
    run, els = await _gate_run(rig, [dict(role="button", name="Download PDF")])
    wire = await judged(rig, run.run_id, "Download PDF")

    def relabel(el):
        el.name = "Pay €84.20"
    rig.pacer_hooks["move_hook"] = relabel
    reply = await verdict(rig, run.run_id, wire, GO)
    assert presses(rig) == []
    assert ("guard", "Pay €84.20", "changed") in rig.log
    assert reply["outcome"] == "judgement_required"
    assert json.loads(reply["judgement"]["packet"])["target"]["name"] == "Pay €84.20"
    assert reply["judgement"]["id"] != wire["judgement"]["id"]


async def test_go_lands_only_on_the_target(rig):
    """F9: an overlay over the point: the hit test fails, nothing is pressed, ``moved``."""
    run, els = await _gate_run(rig, [dict(role="button", name="Download PDF")])
    wire = await judged(rig, run.run_id, "Download PDF")
    els[0].covered = True
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply["outcome"] == "not_run" and reply["reason"] == "moved"
    assert presses(rig) == []


SEARCH_FORM = {"enter_action": "https://a.example/search", "enter_method": "get",
               "enter_target": "", "form": {"id": 1, "method": "get",
                                            "action": "https://a.example/search", "target": "",
                                            "fields": [{"label": "Zoeken", "type": "search"}]}}


async def test_go_type_enter_rereads_before_enter(rig):
    """S2-4: the dispatch of a type that ends in Enter is the Enter; the facts are read again
    immediately before it. The Enter destination changing during typing, or the value changing
    after the last keystroke, sends no Enter."""
    run, els = await _gate_run(rig, [dict(role="textbox", name="Zoeken", field=True,
                                          input_type="text", facts=dict(SEARCH_FORM))])
    wire = await judged(rig, run.run_id, "Zoeken", "type", text="oktober", enter=True)
    packet = json.loads(wire["judgement"]["packet"])
    assert packet["action"] == {"kind": "type", "text": "oktober", "then_enter": True}
    assert packet["target"]["enter_submits_form"]["destination"]["path"] == "/search"

    def retarget(n):
        if n == 3:
            els[0].facts["enter_action"] = "https://a.example/account/delete"
    rig.pacer_hooks["key_hook"] = retarget
    reply = await verdict(rig, run.run_id, wire, GO)
    assert not [e for e in rig.log if e[0] == "enter"]
    assert reply["outcome"] == "judgement_required"          # judged afresh, typed text stays
    assert els[0].value == "oktober"
    rig.pacer_hooks.clear()
    els[0].facts["enter_action"] = "https://a.example/search"
    els[0].value = ""
    wire = await judged(rig, run.run_id, "Zoeken", "type", text="oktober", enter=True)
    real = rig.ctl.ops.focused
    calls = []

    async def focused_then_rewrite(element):
        calls.append(1)
        if len(calls) == len("oktober") + 1:      # the Enter guard's own check
            element.value = "oktober 2026"         # the page rewrites the field before Enter
        return await real(element)
    rig.ctl.ops.focused = focused_then_rewrite
    reply = await verdict(rig, run.run_id, wire, GO)
    assert ("enter_guard", "changed") in rig.log and not [e for e in rig.log if e[0] == "enter"]
    assert reply["outcome"] == "judgement_required"


async def test_go_type_enter_appends_to_a_populated_field_once(rig):
    """p2 Astra: typing inserts at the caret, so the expected value is the read-back one; one
    Enter is sent."""
    run, els = await _gate_run(rig, [dict(role="textbox", name="Zoeken", field=True,
                                          input_type="text", value="Hello",
                                          facts=dict(SEARCH_FORM))])
    wire = await judged(rig, run.run_id, "Zoeken", "type", text=" world", enter=True)
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply["outcome"] == "done" and els[0].value == "Hello world"
    assert [e for e in rig.log if e[0] == "enter"] == [("enter",)]


async def test_keystroke_refused_when_focus_moved(rig):
    """S2-4: before every keystroke the frozen field must hold the focus; when it moves to
    another control (a space would activate a button) typing stops and no Enter is sent."""
    run, els = await _gate_run(rig, [dict(role="textbox", name="Zoeken", field=True,
                                          input_type="text", facts=dict(SEARCH_FORM)),
                                     dict(role="button", name="Delete account")])

    def steal(n):
        if n == 2:
            rig.page().focus = els[1]
    rig.pacer_hooks["key_hook"] = steal
    wire = await judged(rig, run.run_id, "Zoeken", "type", text="a b c", enter=True)
    reply = await verdict(rig, run.run_id, wire, GO)
    assert ("focus_lost", "Zoeken", 2) in rig.log
    assert els[0].value == "a " and not [e for e in rig.log if e[0] == "enter"]
    assert presses(rig, "Delete account") == []
    assert reply["outcome"] == "judgement_required"


@pytest.mark.parametrize("element", [
    dict(role="button", name="Target"),
    dict(role="link", name="Target", href="https://a.example/x"),
    dict(role="checkbox", name="Target", field=True, input_type="checkbox"),
    dict(role="combobox", name="Target", options=[("A", "a")]),
    dict(role="button", name="Target", field=True, input_type="submit"),
    dict(role="button", name="Target", field=True, input_type="image"),
    dict(role="textbox", name="Target", field=True, input_type="password"),
    dict(role="textbox", name="Target", field=True, input_type="text",
         facts={"autocomplete": "one-time-code"}),
    dict(role="textbox", name="Target", field=True, input_type="text",
         facts={"autocomplete": "cc-number"}),
], ids=["button", "link", "checkbox", "select", "submit", "image", "password", "one-time-code",
        "card"])
async def test_type_on_non_text_control_is_refused_before_judgement(rig, element):
    run, els = await _gate_run(rig, [element])
    obs = await observe(rig, run.run_id)
    reads = rig.page().reads
    with pytest.raises(ProtocolError, match="unsupported"):
        await rig.ctl.act(run.run_id, Action("type", handle_of(obs, "Target"), obs.generation,
                                             text=" "))
    assert rig.page().reads == reads and rig.ctl.active.pending is None
    assert presses(rig) == [] and not [e for e in rig.log if e[0] == "type"]


TIME = dict(role="textbox", name="Pickup time", field=True, input_type="time",
            facts={"formats": lambda t: len(t) == 5 and t[2] == ":"})


async def test_a_time_field_takes_the_judged_value_as_one_step(rig):
    """Slice 5g: typing into a value field is judged like typing (the same packet), then the
    value is set as one in-page step — no pointer, no keys — and read back."""
    run, els = await _gate_run(rig, [dict(TIME)])
    wire = await judged(rig, run.run_id, "Pickup time", "type", text="19:30")
    packet = json.loads(wire["judgement"]["packet"])
    assert packet["action"] == {"kind": "type", "text": "19:30", "then_enter": False}
    assert packet["target"]["input_type"] == "time"
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply["outcome"] == "done" and reply["typed"] == "shown"
    assert els[0].value == "19:30" and ("set", "Pickup time", "19:30") in rig.log
    assert presses(rig) == [] and not [e for e in rig.log if e[0] in ("type", "enter")]


async def test_a_value_the_page_changes_after_it_was_set_is_reported(rig):
    """d1 Astra/Terra: the read-back that decides is the one after the page's own listeners
    ran; a page that rounds the time is reported (the step ran: never "not run")."""
    run, els = await _gate_run(rig, [dict(TIME, facts=dict(TIME["facts"],
                                                           normalise=lambda t: t[:3] + "45"))])
    wire = await judged(rig, run.run_id, "Pickup time", "type", text="19:37")
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply["outcome"] == "done" and reply["typed"] == "not_shown" and els[0].value == "19:45"


@pytest.mark.parametrize("text,enter,facts,reason", [
    ("7.30pm", False, {}, "value_format"),
    ("19:30", True, {}, "no_enter"),
    ("19:30", False, {"readonly": True}, "not_typable")],
    ids=["format", "enter", "readonly"])
async def test_a_value_field_refuses_before_any_judgement(rig, text, enter, facts, reason):
    """Slice 5g: a value its format refuses, Enter after a value field and a read-only field do
    not run and nothing is judged: the step says why."""
    run, els = await _gate_run(rig, [dict(TIME, facts=dict(TIME["facts"], **facts))])
    obs = await observe(rig, run.run_id)
    reads = rig.page().reads
    result = await rig.ctl.act(run.run_id, Action("type", handle_of(obs, "Pickup time"),
                                                  obs.generation, text=text, enter=enter))
    assert result.outcome == "not_run" and result.extra == {"reason": reason}
    assert rig.page().reads == reads and rig.ctl.active.pending is None
    assert els[0].value == "" and not [e for e in rig.log if e[0] in ("set", "type")]


async def test_a_value_field_changed_before_the_set_is_judged_afresh(rig):
    """F4 holds for the in-page step: a field whose facts changed after the judgement gets no
    value; the step is judged afresh."""
    run, els = await _gate_run(rig, [dict(TIME)])
    wire = await judged(rig, run.run_id, "Pickup time", "type", text="19:30")
    els[0].name = "Delivery time"
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply["outcome"] in ("judgement_required", "not_run")
    assert els[0].value == "" and not [e for e in rig.log if e[0] == "set"]


async def test_typed_text_is_read_back_as_one_bit(rig):
    """Slice 5g: after typing (no Enter) the result says whether the field shows the text —
    a field that swallowed the keys says not_shown — never the value."""
    run, els = await _gate_run(rig, [dict(role="textbox", name="Name", field=True,
                                          input_type="text")])
    wire = await judged(rig, run.run_id, "Name", "type", text="Jan")
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply["outcome"] == "done" and reply["typed"] == "shown"
    els[0].value = ""
    real = rig.ctl.ops.value

    async def swallowed(element):
        element.value = ""
        return await real(element)
    rig.ctl.ops.value = swallowed
    wire = await judged(rig, run.run_id, "Name", "type", text="Jan")
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply["outcome"] == "done" and reply["typed"] == "not_shown"
    assert "Jan" not in json.dumps(reply)

    async def no_break_space(element):            # r1 Astra: a contenteditable's typed spaces
        return "hello\u00a0 world\u00a0"
    rig.ctl.ops.value = no_break_space
    wire = await judged(rig, run.run_id, "Name", "type", text="hello  world ")
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply["typed"] == "shown"


# --------------------------------------------------------------------------- slice 6b

TEXTAREA = dict(role="textbox", name="Bericht", field=True, input_type="",
                facts={"tag": "textarea"})
LETTER = "Beste klantenservice,\n\nMijn bestelling is niet aangekomen.\nGroet, Jan"


async def test_multi_line_text_is_judged_whole_and_typed_into_a_textarea(rig):
    """Slice 6b: the check's packet holds the whole multi-line text; after its go the text is
    typed into the real <textarea> with its line breaks inserted as text (``line_breaks``),
    every character and line break behind the focus check, and nothing more sent."""
    run, els = await _gate_run(rig, [dict(TEXTAREA)])
    wire = await judged(rig, run.run_id, "Bericht", "type", text=LETTER)
    packet = json.loads(wire["judgement"]["packet"])
    assert packet["action"] == {"kind": "type", "text": LETTER, "then_enter": False}
    assert packet["target"]["tag"] == "textarea"
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply["outcome"] == "done" and reply["typed"] == "shown"
    assert els[0].value == LETTER
    assert ("type", "Bericht", len(LETTER), True) in rig.log
    assert not [e for e in rig.log if e[0] == "enter"]


@pytest.mark.parametrize("element,enter,reason", [
    (dict(role="textbox", name="Bericht", field=True, input_type="text"), False, "one_line"),
    (dict(role="textbox", name="Bericht", field=True, input_type="",
          facts={"tag": "div", "contenteditable": True}), False, "one_line"),
    (dict(TEXTAREA), True, "no_enter")],
    ids=["input", "contenteditable", "textarea-then-enter"])
async def test_a_line_break_anywhere_but_a_textarea_is_refused_before_judgement(
        rig, element, enter, reason):
    """Slice 6b: a line break goes only into a real <textarea>, and never with Enter (sending
    stays its own checked step): anything else does not run and nothing is judged."""
    run, els = await _gate_run(rig, [element])
    obs = await observe(rig, run.run_id)
    reads = rig.page().reads
    result = await rig.ctl.act(run.run_id, Action("type", handle_of(obs, "Bericht"),
                                                  obs.generation, text="a\nb", enter=enter))
    assert result.outcome == "not_run" and result.extra == {"reason": reason}
    assert rig.page().reads == reads and rig.ctl.active.pending is None
    assert els[0].value == "" and not [e for e in rig.log if e[0] in ("type", "click")]


async def test_one_line_text_into_an_input_is_unchanged(rig):
    run, els = await _gate_run(rig, [dict(role="textbox", name="Bericht", field=True,
                                          input_type="text")])
    wire = await judged(rig, run.run_id, "Bericht", "type", text="Klopt dit?")
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply["outcome"] == "done" and ("type", "Bericht", 10, False) in rig.log


async def test_multi_line_typing_stops_when_the_textarea_is_no_longer_the_field(rig):
    """The focus check before every character includes the textarea fact: a focused element
    that is no textarea any more gets no line break."""
    run, els = await _gate_run(rig, [dict(TEXTAREA)])
    rig.pacer_hooks["key_hook"] = lambda n: n == 1 and els[0].facts.update(tag="input")
    wire = await judged(rig, run.run_id, "Bericht", "type", text="a\nb")
    reply = await verdict(rig, run.run_id, wire, GO)
    assert els[0].value == "a" and reply["outcome"] in ("judgement_required", "not_run")


async def test_free_search_typing_takes_no_line_break(rig):
    """Slice 6b: a public search field's typing is free (unjudged), so it takes no line break,
    even when the search box is a textarea: the check judges every multi-line text first."""
    run = await rig.run(sites=(("sa", "account"), ("sp", "public")))
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Public B"), obs)
    els = rig.page().show(B_PAGE, "Search", [dict(role="searchbox", name="Zoek", field=True,
                                                  input_type="", facts={"tag": "textarea"})])
    rig.mark = len(rig.log)
    obs = await observe(rig, run.run_id)
    result = await rig.ctl.act(run.run_id, Action("type", handle_of(obs, "Zoek"), obs.generation,
                                                  text="pizza\nroma"))
    assert result.outcome == "not_run" and result.extra == {"reason": "one_line"}
    assert els[0].value == "" and presses(rig) == []


async def test_tap_executes_the_frozen_operation_once(rig):
    """F3: the tap re-judges, the final read matches, and the frozen press runs once; the run
    records the confirmed change with the question's screenshot."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    q = await rig.ctl.get_question(run.run_id, qid)
    assert q["view"]["confirm_offered"] and q["view"]["screenshot_id"]
    assert q["view"]["confirm_label"] == "Pay €84.20" and q["state"] == "open"
    assert rig.state(run.run_id) == "waiting_approval"
    assert (await rig.ctl.list_active())[0].pending_question == qid
    rj = await tap(rig, run.run_id, qid)
    assert rj["outcome"] == "rejudgement_required" and rj["judgement"]["kind"] == "rejudge"
    body = json.loads(rj["judgement"]["packet"])
    assert set(body) == {"approved_packet", "approved_question", "current_packet", "difference",
                         "destination_changed"}
    assert body["difference"] == {} and body["destination_changed"] is False
    assert body["approved_question"] == {"text": ASK["question"]["text"],
                                         "confirm_label": "Pay €84.20", "highlight": ["€84.20"]}
    assert presses(rig) == []
    done = await verdict(rig, run.run_id, rj, SAME)
    assert done["outcome"] == "done" and len(presses(rig, "Pay now")) == 1
    assert (await rig.ctl.get_question(run.run_id, qid))["state"] == "executed"
    assert rig.state(run.run_id) == "running"
    assert [c.kind for c in rig.ctl.active.changes] == ["action_confirmed"]
    confirmed = rig.events(run.run_id, "action")[-1]
    assert confirmed.outcome == "confirmed" and confirmed.screenshot_id == q["view"]["screenshot_id"]


async def test_duplicate_tap_answers_first_outcome(rig):
    """F7: a repeated tap answers its stored response and builds nothing again; a repeated
    verdict on the re-judgement presses nothing again."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    first = await tap(rig, run.run_id, qid)
    again = await tap(rig, run.run_id, qid)
    assert again == {**first, "repeat": True}
    done = await verdict(rig, run.run_id, first, SAME)
    assert (await verdict(rig, run.run_id, first, SAME)) == {**done, "repeat": True}
    assert (await tap(rig, run.run_id, qid)) == {**first, "repeat": True}
    assert len(presses(rig, "Pay now")) == 1


async def test_lapsed_and_other_revision_taps_execute_nothing(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    assert (await tap(rig, run.run_id, qid, revision=2))["outcome"] == "not_current"
    rj = await tap(rig, run.run_id, qid)
    lapsed = await verdict(rig, run.run_id, rj, CHANGED)
    assert lapsed["outcome"] == "judgement_required"
    assert (await rig.ctl.get_question(run.run_id, qid))["state"] == "lapsed"
    # The lapsed question's tap: the stored response of its one answer, nothing else.
    assert (await tap(rig, run.run_id, qid))["repeat"] is True
    other = await rig.ctl.answer_question(run.run_id, qid, {
        "revision": 1, "digest": "f" * 64, "choice": "confirm", "operator_ref": "tg"})
    assert other["outcome"] == "not_current"
    assert presses(rig) == []


async def test_rejudge_changed_lapses_and_rejudges(rig):
    """F4 after a tap: ``changed`` lapses the question and judges the action afresh; an ask
    then is a new question with a new screenshot."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    rig.page().text += "\nTransaction fee € 2,50"
    rj = await tap(rig, run.run_id, qid)
    assert json.loads(rj["judgement"]["packet"])["difference"] == {
        "blocks_added": ["Transaction fee € 2,50"]}
    fresh = await verdict(rig, run.run_id, rj, CHANGED)
    assert fresh["outcome"] == "judgement_required" and presses(rig) == []
    assert rig.state(run.run_id) == "running"
    again = await verdict(rig, run.run_id, fresh, ASK)
    assert again["outcome"] == "asked" and again["question_id"] != qid
    first_shot = (await rig.ctl.get_question(run.run_id, qid))["view"]["screenshot_id"]
    second = await rig.ctl.get_question(run.run_id, again["question_id"])
    assert second["view"]["screenshot_id"] not in (None, first_shot)


async def test_same_with_destination_changed_is_changed(rig):
    run, els = await _gate_run(rig, [dict(role="link", name="Betalen", href=PAY)])
    obs = await observe(rig, run.run_id)
    result = await rig.ctl.act(run.run_id, Action("click", handle_of(obs, "Betalen"),
                                                  obs.generation))
    reply = await verdict(rig, run.run_id, result.to_wire(), ASK)
    qid = reply["question_id"]
    els[0].href = PAY + "?quote=Q90"            # the masked path stays /pay
    rj = await tap(rig, run.run_id, qid)
    assert json.loads(rj["judgement"]["packet"])["destination_changed"] is True
    reply = await verdict(rig, run.run_id, rj, SAME)
    # The question lapses; the link no longer is the one observed, so it is not judged afresh
    # either: the model observes again.
    assert reply["outcome"] == "lapsed" and presses(rig) == []
    assert not [e for e in rig.log[rig.mark:] if e[0] == "move"]   # the destination rule, first
    assert (await rig.ctl.get_question(run.run_id, qid))["state"] == "lapsed"


async def test_duplicate_verdict_during_the_press_waits_for_its_response(rig):
    """A verdict re-posted while its press is in flight (the first response not yet back)
    gets the press's real response, not the journal's provisional outcome_unknown, and the
    press runs once."""
    run, els = await _gate_run(rig, [dict(role="button", name="Download PDF")])
    wire = await judged(rig, run.run_id, "Download PDF")
    rig.gate = asyncio.Event()
    first = asyncio.create_task(verdict(rig, run.run_id, wire, GO))
    for _ in range(50):
        await asyncio.sleep(0.005)
        if presses(rig, "Download PDF"):
            break
    second = asyncio.create_task(verdict(rig, run.run_id, wire, GO))
    await asyncio.sleep(0.02)
    assert not second.done()
    rig.gate.set()
    done = await first
    assert done["outcome"] == "done" and (await second) == {**done, "repeat": True}
    assert len(presses(rig, "Download PDF")) == 1


async def test_a_link_changed_since_observation_is_never_judged(rig):
    """A1 (r1): the handle's address and the page's differ: nothing is judged or opened."""
    run, els = await _gate_run(rig, [dict(role="link", name="Read invoices", href=A_PAGE)])
    obs = await observe(rig, run.run_id)
    els[0].href = "https://a.example/account/delete"
    with pytest.raises(ProtocolError, match="stale_handle"):
        await rig.ctl.act(run.run_id, Action("navigate", handle_of(obs, "Read invoices"),
                                             obs.generation))
    assert presses(rig) == [] and rig.ctl.active.pending is None


async def test_same_with_text_incomplete_is_changed(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    rj = await tap(rig, run.run_id, qid)
    reply = await verdict(rig, run.run_id, rj, {**SAME, "text_incomplete": True})
    assert reply["outcome"] == "judgement_required" and presses(rig) == []


async def test_final_read_difference_reruns_rejudge_at_most_twice(rig):
    """Ruling 8: the final read must equal the reading the re-judgement judged, with no
    exemption; a counter that ticks between reads reruns it, twice in all, then the question
    goes back as a new one."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")],
                               text="Total € 84,20\nUpdated 1 s ago")
    qid = await asked(rig, run, els)

    def tick(page, n):
        page.text = f"Total € 84,20\nUpdated {n} s ago"
    rig.page().read_hook = tick
    first = await tap(rig, run.run_id, qid)
    second = await verdict(rig, run.run_id, first, SAME)
    assert second["outcome"] == "rejudgement_required" and presses(rig) == []
    assert json.loads(second["judgement"]["packet"])["difference"]["blocks_changed"]
    third = await verdict(rig, run.run_id, second, SAME)
    assert third["outcome"] == "judgement_required" and presses(rig) == []
    assert (await rig.ctl.get_question(run.run_id, qid))["state"] == "lapsed"
    assert len([e for e in rig.events(run.run_id, "judgement") if "re-check" in e.text_masked]) == 2


async def test_tap_type_enter_is_rejudged_after_typing(rig):
    """A tapped type+Enter: the typed page is re-judged before Enter (one more check), and the
    count is shared with the tap's limit of two."""
    run, els = await _gate_run(rig, [dict(role="textbox", name="Bericht", field=True,
                                          input_type="text", facts=dict(SEARCH_FORM))])
    wire = await judged(rig, run.run_id, "Bericht", "type", text="Klopt dit?", enter=True)
    qid = (await verdict(rig, run.run_id, wire, ASK_CHANGE))["question_id"]
    rj = await tap(rig, run.run_id, qid)
    typed = await verdict(rig, run.run_id, rj, SAME)
    assert typed["outcome"] == "rejudgement_required" and els[0].value == "Klopt dit?"
    body = json.loads(typed["judgement"]["packet"])
    assert body["difference"]["controls_changed"]       # the typed value is in the difference
    assert not [e for e in rig.log if e[0] == "enter"]
    # changed on the typed page: no Enter, the text stays, the question lapses.
    reply = await verdict(rig, run.run_id, typed, CHANGED)
    assert reply["outcome"] == "judgement_required" and not [e for e in rig.log if e[0] == "enter"]
    assert els[0].value == "Klopt dit?"
    # same, then a difference at the final read: still no Enter, and no third re-judgement.
    els[0].value = ""
    wire = await judged(rig, run.run_id, "Bericht", "type", text="Klopt dit?", enter=True)
    qid = (await verdict(rig, run.run_id, wire, ASK_CHANGE))["question_id"]
    rj = await tap(rig, run.run_id, qid)
    typed = await verdict(rig, run.run_id, rj, SAME)
    rig.page().text += "\nNew notice"
    reply = await verdict(rig, run.run_id, typed, SAME)
    assert reply["outcome"] == "judgement_required" and not [e for e in rig.log if e[0] == "enter"]
    # And the clean path: one Enter.
    els[0].value = ""
    wire = await judged(rig, run.run_id, "Bericht", "type", text="Klopt dit?", enter=True)
    qid = (await verdict(rig, run.run_id, wire, ASK_CHANGE))["question_id"]
    rj = await tap(rig, run.run_id, qid)
    typed = await verdict(rig, run.run_id, rj, SAME)
    done = await verdict(rig, run.run_id, typed, SAME)
    assert done["outcome"] == "done" and [e for e in rig.log if e[0] == "enter"] == [("enter",)]


async def test_press_timeout_after_mousedown_is_outcome_unknown_never_retried(rig):
    """F8: a press that may have happened is outcome_unknown, recorded, and never retried —
    a repeated verdict replays it."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    rj = await tap(rig, run.run_id, qid)
    rig.pacer_hooks["fail_after_down"] = "timeout"
    reply = await verdict(rig, run.run_id, rj, SAME)
    assert reply["outcome"] == "outcome_unknown" and len(presses(rig, "Pay now")) == 1
    assert (await verdict(rig, run.run_id, rj, SAME)) == {**reply, "repeat": True}
    assert len(presses(rig, "Pay now")) == 1
    assert (await rig.ctl.get_question(run.run_id, qid))["state"] == "outcome_unknown"
    assert [c.kind for c in rig.ctl.active.changes] == ["outcome_unknown"]
    assert rig.state(run.run_id) == "running"


async def test_money_question_without_faithful_screenshot_offers_no_confirm(rig):
    """F6: a payment question needs the app's own faithful screenshot, or offers only the live
    view."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    rig.page().unfaithful = True
    qid = await asked(rig, run, els)
    view = (await rig.ctl.get_question(run.run_id, qid))["view"]
    assert view["screenshot_id"] is None and view["confirm_offered"] is False
    assert view["live_only_copy"] == "I can't show you this page reliably; you can do it in the live view."
    with pytest.raises(ProtocolError, match="unsupported"):
        await tap(rig, run.run_id, qid)
    assert presses(rig) == []


async def test_card_field_question_needs_the_screenshot_too(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Save card")])
    rig.page().has_cc = True
    rig.page().unfaithful = True
    qid = await asked(rig, run, els, "Save card", ASK_CHANGE)
    view = (await rig.ctl.get_question(run.run_id, qid))["view"]
    assert view["confirm_offered"] is False and view["screenshot_id"] is None


async def test_text_incomplete_offers_no_confirm(rig):
    """F6a and operator ruling (a): an account change whose terms the page does not state (a
    direct debit) offers only the live view."""
    run, els = await _gate_run(rig, [dict(role="button", name="Automatische incasso activeren")])
    v = {**ASK_CHANGE, "question": {**ASK_CHANGE["question"], "text_incomplete": True,
                                    "text": "Activate direct debit for Site A?"}}
    qid = await asked(rig, run, els, "Automatische incasso", v)
    view = (await rig.ctl.get_question(run.run_id, qid))["view"]
    assert view["confirm_offered"] is False
    assert view["text"] == "Activate direct debit for Site A?"
    assert view["live_only_copy"] == ("I can't check the amount on this page myself, so please "
                                      "finish it in the live view.")
    with pytest.raises(ProtocolError, match="unsupported"):
        await tap(rig, run.run_id, qid)
    assert presses(rig) == []


async def test_extra_option_executes_nothing(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    reply = await tap(rig, run.run_id, qid, {"extra": 0})
    assert reply["outcome"] == "answered" and reply["label"] == "Store credit"
    assert presses(rig) == [] and rig.state(run.run_id) == "running"
    assert (await rig.ctl.get_question(run.run_id, qid))["state"] == "answered"
    # r1 Astra 3: the chosen option reaches the model through the run's events
    answered = rig.events(run.run_id, "question")[-1]
    assert answered.outcome == "answered" and "“Store credit”" in answered.text_masked
    with pytest.raises(ProtocolError, match="unsupported"):
        await tap(rig, run.run_id, (await asked(rig, run, els)), {"extra": 1})
    declined = await tap(rig, run.run_id, (await rig.ctl.list_active())[0].pending_question,
                         "not_now")
    assert declined["outcome"] == "declined" and presses(rig) == []


async def test_no_route_or_tool_accepts_a_verdict_from_the_model_side():
    from casa_browser.api import make_app
    from casa_browser.protocol import parse_action

    class Nothing:
        pairing = None
    routes = [r.resource.canonical for r in make_app(Nothing()).router.routes()
              if r.resource is not None]
    for path in routes:
        if "judgement" in path or "question" in path:
            assert path.startswith("/v1/control/"), path
    with pytest.raises(ProtocolError, match="unsupported"):
        parse_action({"kind": "click", "handle": "h1", "generation": 1,
                      "verdict": {"verdict": "go"}})


# -- S2-5 / G2: each id applied once; the stored response for every control request

async def test_replayed_go_verdict_dispatches_once(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Download PDF")])
    wire = await judged(rig, run.run_id, "Download PDF")
    first = await verdict(rig, run.run_id, wire, GO)
    assert first["outcome"] == "done"
    assert (await verdict(rig, run.run_id, wire, GO)) == {**first, "repeat": True}
    assert len(presses(rig, "Download PDF")) == 1


async def test_replayed_rejudge_verdict_presses_once(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    rj = await tap(rig, run.run_id, qid)
    first = await verdict(rig, run.run_id, rj, SAME)
    for _ in range(3):
        assert (await verdict(rig, run.run_id, rj, SAME)) == {**first, "repeat": True}
    assert len(presses(rig, "Pay now")) == 1


async def test_replay_after_f4_change_returns_the_same_successor(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Download PDF")])
    wire = await judged(rig, run.run_id, "Download PDF")
    rig.pacer_hooks["move_hook"] = lambda el: setattr(el, "name", "Pay €84.20")
    first = await verdict(rig, run.run_id, wire, GO)
    again = await verdict(rig, run.run_id, wire, GO)
    assert again == {**first, "repeat": True}
    assert again["judgement"]["id"] == first["judgement"]["id"]
    assert len(rig.events(run.run_id, "judgement")) == 1 and presses(rig) == []


async def test_replay_after_final_read_difference_returns_the_same_successor(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")], text="Updated 1")
    qid = await asked(rig, run, els)
    rig.page().read_hook = lambda page, n: setattr(page, "text", f"Updated {n}")
    rj = await tap(rig, run.run_id, qid)
    first = await verdict(rig, run.run_id, rj, SAME)
    assert first["outcome"] == "rejudgement_required"
    assert (await verdict(rig, run.run_id, rj, SAME)) == {**first, "repeat": True}
    assert presses(rig) == []


async def test_replay_after_post_typing_rejudge_returns_the_same_outcome(rig):
    run, els = await _gate_run(rig, [dict(role="textbox", name="Bericht", field=True,
                                          input_type="text", facts=dict(SEARCH_FORM))])
    wire = await judged(rig, run.run_id, "Bericht", "type", text="Klopt dit?", enter=True)
    qid = (await verdict(rig, run.run_id, wire, ASK_CHANGE))["question_id"]
    rj = await tap(rig, run.run_id, qid)
    typed = await verdict(rig, run.run_id, rj, SAME)
    assert (await verdict(rig, run.run_id, rj, SAME)) == {**typed, "repeat": True}
    done = await verdict(rig, run.run_id, typed, SAME)
    assert (await verdict(rig, run.run_id, typed, SAME)) == {**done, "repeat": True}
    assert els[0].value == "Klopt dit?" and [e for e in rig.log if e[0] == "enter"] == [("enter",)]


async def test_unknown_or_evicted_id_is_stale(rig, monkeypatch):
    from casa_browser import store as store_mod
    run, els = await _gate_run(rig, [dict(role="button", name="Download PDF")])
    with pytest.raises(ProtocolError, match="judgement_stale"):
        await rig.ctl.apply_judgement(run.run_id, "jnope", {"no_verdict": "check_unavailable"})
    monkeypatch.setattr(store_mod, "RESPONSES_KEPT", 2)
    first = await judged(rig, run.run_id, "Download PDF")
    await verdict(rig, run.run_id, first, None)
    for _ in range(2):
        await verdict(rig, run.run_id, await judged(rig, run.run_id, "Download PDF"), None)
    with pytest.raises(ProtocolError, match="judgement_stale"):
        await verdict(rig, run.run_id, first, None)


async def test_no_verdict_body_is_stored_and_replayed(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    wire = await judged(rig, run.run_id, "Pay now")
    first = await verdict(rig, run.run_id, wire, None)
    assert first["outcome"] == "check_unavailable"
    assert (await verdict(rig, run.run_id, wire, None)) == {**first, "repeat": True}
    qid = await asked(rig, run, els)
    rj = await tap(rig, run.run_id, qid)
    lapse = await verdict(rig, run.run_id, rj, None)       # a re-judgement's no verdict lapses
    assert lapse["outcome"] == "judgement_required"
    assert (await verdict(rig, run.run_id, rj, None)) == {**lapse, "repeat": True}
    assert presses(rig) == []


async def test_cancel_after_mousedown_records_outcome_unknown(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    rj = await tap(rig, run.run_id, qid)
    rig.gate = asyncio.Event()                             # the press holds after mouse-down
    pressing = asyncio.create_task(verdict(rig, run.run_id, rj, SAME))
    await asyncio.sleep(0.01)
    assert len(presses(rig, "Pay now")) == 1
    await rig.ctl.cancel(run.run_id)
    with pytest.raises(ProtocolError, match="interrupted"):
        await pressing
    assert rig.store.get_question(qid).state == "outcome_unknown"
    assert rig.store.get_response(run.run_id, "j:" + rj["judgement"]["id"])["outcome"] == \
        "outcome_unknown"
    assert [e.outcome for e in rig.events(run.run_id, "action")][-1] == "outcome_unknown"
    assert len(presses(rig, "Pay now")) == 1


async def test_cancel_after_enter_keydown_records_outcome_unknown(rig):
    run, els = await _gate_run(rig, [dict(role="textbox", name="Zoeken", field=True,
                                          input_type="text", facts=dict(SEARCH_FORM))])
    wire = await judged(rig, run.run_id, "Zoeken", "type", text="oktober", enter=True)
    rig.pacer_hooks["enter_gate"] = asyncio.Event()
    pressing = asyncio.create_task(verdict(rig, run.run_id, wire, GO))
    for _ in range(50):
        await asyncio.sleep(0.005)
        if ("enter",) in rig.log:
            break
    assert ("enter",) in rig.log
    await rig.ctl.cancel(run.run_id)
    with pytest.raises(ProtocolError, match="interrupted"):
        await pressing
    assert rig.store.get_response(run.run_id, "j:" + wire["judgement"]["id"])["outcome"] == \
        "outcome_unknown"
    assert [e.outcome for e in rig.events(run.run_id, "action")][-1] == "outcome_unknown"


async def test_cancel_before_mousedown_is_not_run(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    wire = await judged(rig, run.run_id, "Pay now")
    held = asyncio.Event()
    real_hit = rig.ctl.ops.hit

    async def slow_hit(element, point):
        await held.wait()
        return await real_hit(element, point)
    rig.ctl.ops.hit = slow_hit
    pressing = asyncio.create_task(verdict(rig, run.run_id, wire, GO))
    await asyncio.sleep(0.01)
    await rig.ctl.cancel(run.run_id)
    with pytest.raises(ProtocolError, match="interrupted"):
        await pressing
    assert presses(rig) == []
    assert rig.store.get_response(run.run_id, "j:" + wire["judgement"]["id"]) is None
    assert "outcome_unknown" not in [e.outcome for e in rig.events(run.run_id)]


DISPATCHES = ["press", "enter", "select", "back", "goto", "button_download", "link_download"]


@pytest.mark.parametrize("kind", DISPATCHES)
async def test_every_dispatch_kind_journals_before_input(rig, kind):
    """G2: immediately before every dispatching input the operation is journalled; a cut right
    after it leaves exactly one outcome_unknown and no retry."""
    run = await rig.run()
    page = rig.page()
    els = page.show(A_START, "Invoices", [
        dict(role="button", name="Pay now"),
        dict(role="textbox", name="Zoeken", field=True, input_type="text", facts=dict(SEARCH_FORM)),
        dict(role="combobox", name="Jaar", options=[("2026", "2026"), ("2025", "2025")]),
        dict(role="option", name="2025", facts={"value": "2025"}),
        dict(role="link", name="Invoices", href=A_PAGE),
        dict(role="link", name="Invoice PDF", href="https://a.example/x.pdf")])
    page.history.append("https://a.example/previous")
    rig.mark = len(rig.log)
    cut = asyncio.Event()
    hold = asyncio.Event()

    async def stall_goto(url):
        cut.set()
        await hold.wait()
    page.goto_hook = stall_goto
    real_select = rig.ctl.ops.select_one

    async def stall_select(*a):
        out = await real_select(*a)
        cut.set()
        await hold.wait()
        return out
    rig.ctl.ops.select_one = stall_select
    real_back = page.go_back

    async def stall_back(**kw):
        await real_back(**kw)
        cut.set()
        await hold.wait()
    page.go_back = stall_back
    rig.gate = hold if kind in ("press", "button_download") else None
    rig.pacer_hooks["enter_gate"] = hold
    rig.downloader.gate = hold
    obs = await observe(rig, run.run_id)
    h = lambda label: handle_of(obs, label)  # noqa: E731
    action = {"press": Action("click", h("Pay now"), obs.generation),
              "enter": Action("type", h("Zoeken"), obs.generation, text="x", enter=True),
              "select": Action("select", h("Jaar"), obs.generation, choice_handle=h("2025")),
              "back": Action("back", obs.page_handle, obs.generation),
              "goto": Action("navigate", h("Invoices"), obs.generation)}.get(kind)
    if kind in ("button_download", "link_download"):
        label = "Pay now" if kind == "button_download" else "Invoice PDF"
        result = await rig.ctl.download(run.run_id, h(label))
    else:
        result = await rig.ctl.act(run.run_id, action)
    wire = result.to_wire()
    pressing = asyncio.create_task(verdict(rig, run.run_id, wire, GO))
    for _ in range(200):
        await asyncio.sleep(0.002)
        if cut.is_set() or (rig.ctl.active.pending is not None
                            and rig.ctl.active.pending.dispatch_started):
            break
    await asyncio.sleep(0.01)
    await rig.ctl.cancel(run.run_id)
    with pytest.raises(ProtocolError, match="interrupted"):
        await pressing
    stored = rig.store.get_response(run.run_id, "j:" + wire["judgement"]["id"])
    assert stored["outcome"] == "outcome_unknown", kind
    unknown = [e for e in rig.events(run.run_id, "action") if e.outcome == "outcome_unknown"]
    assert len(unknown) == 1
    inputs = {"press": "click", "enter": "enter", "select": "select", "back": "back",
              "goto": "goto", "button_download": "click", "link_download": "fetch"}[kind]
    assert len([e for e in rig.log[rig.mark:] if e[0] == inputs]) <= 1


@pytest.mark.parametrize("request_kind", ["verdict", "answer"])
async def test_every_control_request_replays_its_stored_response(rig, request_kind):
    """G2: the response is stored with the state move; a lost response's repeat returns it
    with ``repeat: true`` and nothing moves or runs again. (The live-from-question and
    login-item requests join with slices 3 and 5.)"""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    if request_kind == "answer":
        first = await tap(rig, run.run_id, qid)
        assert rig.store.get_question(qid).response == first
        again = await tap(rig, run.run_id, qid)
    else:
        rj = await tap(rig, run.run_id, qid)
        first = await verdict(rig, run.run_id, rj, SAME)
        assert rig.store.get_response(run.run_id, "j:" + rj["judgement"]["id"]) == first
        again = await verdict(rig, run.run_id, rj, SAME)
    assert json.dumps(again, sort_keys=True) == json.dumps({**first, "repeat": True}, sort_keys=True)
    assert len(presses(rig, "Pay now")) == (1 if request_kind == "verdict" else 0)


async def test_scope_uses_destination_session(rig):
    """G1: scope is decided on the session the input is delivered in: a public page's link to
    a registered account site is checked; a link within the public web is free."""
    run = await rig.run(sites=(("sa", "account"), ("sp", "public")))
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Public B"), obs)
    assert rig.browser.session.kind == "public"
    rig.page().show(B_PAGE, "Public", [dict(role="link", name="Cancel subscription",
                                            href="https://a.example/subscription/cancel?confirm=1"),
                                       dict(role="link", name="Article", href=B_PAGE + "article")])
    obs = await observe(rig, run.run_id)
    judged_nav = await rig.ctl.act(run.run_id, Action("navigate", handle_of(obs, "Cancel subscription"),
                                                      obs.generation))
    assert judged_nav.outcome == "judgement_required"
    judged_click = await rig.ctl.act(run.run_id, Action("click", handle_of(obs, "Cancel subscription"),
                                                        obs.generation))
    assert judged_click.outcome == "judgement_required"
    assert not [e for e in rig.log if e[0] == "goto" and "cancel" in e[1]]
    free = await rig.ctl.act(run.run_id, Action("click", handle_of(obs, "Article"), obs.generation))
    assert free.outcome == "done" and ("click", "Article") in rig.log


async def test_free_public_navigation_refused_outside_the_public_session(rig):
    """G1 a: a free navigation runs only if the session at the load is the public one."""
    run = await rig.run(mode="open_web", sites=())
    rig.page().show(B_PAGE, "Public", [dict(role="link", name="Next", href=B_PAGE + "next")])
    obs = await observe(rig, run.run_id)
    rig.ctl.active.ref = replace(rig.ctl.active.ref, kind="account")   # as if it had moved

    async def no_switch(active, url, **_kw):
        return None                    # a switch that did not reach the public profile
    rig.ctl._switch_for = no_switch
    result = await rig.ctl.act(run.run_id, Action("navigate", handle_of(obs, "Next"),
                                                  obs.generation))
    assert result.to_wire()["outcome"] == "not_run" and result.to_wire()["reason"] == "changed"
    assert ("goto", B_PAGE + "next") not in rig.log


async def test_re_resolution_one_match_executes_two_matches_do_not(rig):
    """Design §2.6: a new generation at the same address re-resolves the frozen target to
    exactly one matching control; two matches execute nothing."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    wire = await judged(rig, run.run_id, "Pay now")
    page = rig.page()
    url = page.url
    page.show(url, page.text, [dict(role="button", name="Pay now")])    # same address, new gen
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply["outcome"] == "done" and presses(rig, "Pay now") == [("click", "Pay now")]
    wire = await judged(rig, run.run_id, "Pay now")
    page.show(url, page.text, [dict(role="button", name="Pay now"), dict(role="button", name="Pay now")])
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply == {"outcome": "not_run", "generation": reply["generation"], "reason": "changed"}
    assert len(presses(rig, "Pay now")) == 1


async def test_restart_expires_open_questions_and_marks_confirming_unknown(tmp_path):
    r = Rig(tmp_path)
    await r.start()
    run = await r.run()
    r.page().show(A_START, "Pay", [dict(role="button", name="Pay now")])
    r.mark = len(r.log)
    qid = await asked(r, run, None)
    run2_q = r.store.get_question(qid)
    assert run2_q.state == "open"
    await tap(r, run.run_id, qid)
    assert r.store.get_question(qid).state == "confirming"
    r.close()
    again = Rig(tmp_path)
    await again.start()
    try:
        assert again.store.get_question(qid).state == "outcome_unknown"
        assert again.store.get_run(run.run_id).state == "interrupted"
    finally:
        await again.ctl.shutdown()
        again.close()


async def test_run_end_expires_its_question(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    await rig.ctl.cancel(run.run_id)
    assert rig.store.get_question(qid).state == "expired"
    assert (await tap(rig, run.run_id, qid))["outcome"] == "not_current"


async def test_free_search_typing_rechecks_its_field_at_every_input(rig):
    """G1 b (r1): a public search field that stops being one — before the click, or between
    two characters — receives nothing more."""
    run = await rig.run(sites=(("sa", "account"), ("sp", "public")))
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Public B"), obs)
    els = rig.page().show(B_PAGE, "Search", [dict(role="searchbox", name="Zoek", field=True,
                                                  input_type="search")])
    rig.mark = len(rig.log)
    obs = await observe(rig, run.run_id)
    rig.pacer_hooks["move_hook"] = lambda el: setattr(el, "input_type", "checkbox")
    result = await rig.ctl.act(run.run_id, Action("type", handle_of(obs, "Zoek"), obs.generation,
                                                  text=" x"))
    assert result.to_wire()["outcome"] == "not_run" and presses(rig) == []
    els[0].input_type = "search"
    rig.pacer_hooks = {"key_hook": lambda n: n == 1 and setattr(els[0], "input_type", "checkbox")}
    obs = await observe(rig, run.run_id)
    result = await rig.ctl.act(run.run_id, Action("type", handle_of(obs, "Zoek"), obs.generation,
                                                  text="a b"))
    assert result.to_wire()["outcome"] == "not_run" and els[0].value == "a"


async def test_judged_typing_stops_when_the_field_is_no_text_field(rig):
    run, els = await _gate_run(rig, [dict(role="textbox", name="Bericht", field=True,
                                          input_type="text", facts=dict(SEARCH_FORM))])
    rig.pacer_hooks["key_hook"] = lambda n: n == 2 and setattr(els[0], "input_type", "checkbox")
    wire = await judged(rig, run.run_id, "Bericht", "type", text="ab c", enter=True)
    reply = await verdict(rig, run.run_id, wire, GO)
    assert els[0].value == "ab" and not [e for e in rig.log if e[0] == "enter"]
    assert reply["outcome"] in ("judgement_required", "not_run")


async def test_free_link_click_rechecks_the_link_and_the_point(rig):
    run = await rig.run(sites=(("sa", "account"), ("sp", "public")))
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Public B"), obs)
    els = rig.page().show(B_PAGE, "Public", [dict(role="link", name="Article", href=B_PAGE + "a")])
    rig.mark = len(rig.log)
    obs = await observe(rig, run.run_id)
    rig.pacer_hooks["move_hook"] = lambda el: setattr(el, "href", "https://a.example/cancel")
    result = await rig.ctl.act(run.run_id, Action("click", handle_of(obs, "Article"), obs.generation))
    assert result.to_wire()["outcome"] == "not_run" and presses(rig) == []
    els[0].href = B_PAGE + "a"
    rig.pacer_hooks = {}
    els[0].covered = True
    obs = await observe(rig, run.run_id)
    result = await rig.ctl.act(run.run_id, Action("click", handle_of(obs, "Article"), obs.generation))
    assert result.to_wire() == {"outcome": "not_run", "generation": result.generation,
                                "reason": "moved"} and presses(rig) == []


async def test_select_in_a_card_select_is_refused_before_judgement(rig):
    run, els = await _gate_run(rig, [
        dict(role="combobox", name="Expiry", options=[("2031", "2031")],
             facts={"autocomplete": "cc-exp-year"}),
        dict(role="option", name="2031", facts={"value": "2031"})])
    obs = await observe(rig, run.run_id)
    with pytest.raises(ProtocolError, match="unsupported"):
        await rig.ctl.act(run.run_id, Action("select", handle_of(obs, "Expiry"), obs.generation,
                                             choice_handle=handle_of(obs, "2031")))
    assert rig.ctl.active.pending is None and presses(rig) == []


async def test_a_type_frozen_afresh_is_admitted_again(rig):
    """r3: a field that became a checkbox is neither clicked by the go nor frozen afresh."""
    run, els = await _gate_run(rig, [dict(role="textbox", name="Note", field=True,
                                          input_type="text")])
    wire = await judged(rig, run.run_id, "Note", "type", text=" ")
    els[0].input_type = "checkbox"
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply["outcome"] == "not_run" and presses(rig) == []
    assert not [e for e in rig.log[rig.mark:] if e[0] in ("guard", "move")
                and e[-1] == "ok"]


# -- slice 3: the live view from a question, and the question's lifetime (S3-3, S3-4) --------

async def live(rig, run_id, qid, n=1):
    q = await rig.ctl.get_question(run_id, qid)
    return await rig.ctl.live_from_question(run_id, qid, {
        "revision": q["revision"], "digest": q["digest"], "n": n, "operator_ref": "tg-op"})


async def _expose():
    pass


async def test_live_from_question_returns_to_waiting_approval(rig):
    """S3-3: [Open live view] opens an assist view from the open question; a repeat of the same
    request asks nothing again; a confirm while the operator has the view is refused; at Done
    the run waits on the same question, whose confirm then runs the frozen press once."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    opened = await live(rig, run.run_id, qid)
    assert opened["outcome"] == "live" and opened["grant"]["grant_id"]
    assert rig.state(run.run_id) == "waiting_live"
    assert await live(rig, run.run_id, qid) == {**opened, "repeat": True}
    assert (await live(rig, run.run_id, qid, n=2))["outcome"] == "live_open"
    await rig.ctl.begin_live(run.run_id, _expose)
    assert rig.state(run.run_id) == "live"
    with pytest.raises(ProtocolError, match="not_ready"):
        await tap(rig, run.run_id, qid)
    await rig.ctl.live_done(run.run_id)
    assert rig.state(run.run_id) == "waiting_approval"
    q = await rig.ctl.get_question(run.run_id, qid)
    assert q["state"] == "open" and (await rig.ctl.list_active())[0].pending_question == qid
    assert rig.events(run.run_id, "live_done")[-1].outcome == "waiting_approval"
    rj = await tap(rig, run.run_id, qid)
    assert rj["outcome"] == "rejudgement_required"
    assert (await verdict(rig, run.run_id, rj, SAME))["outcome"] == "done"
    assert len(presses(rig, "Pay now")) == 1


async def test_answer_ends_a_live_view_never_opened(rig):
    """A live view asked from the question but never opened does not hold its buttons: the
    answer ends it and applies."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    await live(rig, run.run_id, qid)
    assert rig.state(run.run_id) == "waiting_live"
    assert (await tap(rig, run.run_id, qid, "not_now"))["outcome"] == "declined"
    assert rig.state(run.run_id) == "running" and rig.ctl.active.live is None
    assert presses(rig) == []
    with pytest.raises(ProtocolError, match="not_ready"):
        await rig.ctl.begin_live(run.run_id, _expose)


async def test_tap_after_operator_submitted_lapses(rig):
    """Design §2.5: the operator submits the form himself in the live view; the later confirm
    finds the target gone, executes nothing, and the question lapses with a note to observe."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    await live(rig, run.run_id, qid)
    await rig.ctl.begin_live(run.run_id, _expose)
    rig.page().show(A_PAGE, "Thank you, paid", [dict(role="link", name="Invoices",
                                                    href=A_START)])
    await rig.ctl.live_done(run.run_id)
    assert rig.state(run.run_id) == "waiting_approval"
    reply = await tap(rig, run.run_id, qid)
    assert reply["outcome"] == "lapsed" and reply["reason"] == "target_gone"
    assert presses(rig) == []
    assert (await rig.ctl.get_question(run.run_id, qid))["state"] == "lapsed"
    assert rig.state(run.run_id) == "running"
    assert "observe the page" in rig.events(run.run_id, "question")[-1].text_masked


async def test_question_expires_with_its_tokens(rig):
    """S3-4: an open question lives one hour (its tap tokens' lifetime); then it expires, the
    run runs again, and neither a tap nor a live view acts on it."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    rig.clock.advance(3599)
    await rig.ctl.expire_questions()
    assert (await rig.ctl.get_question(run.run_id, qid))["state"] == "open"
    rig.clock.advance(2)
    await rig.ctl.expire_questions()
    assert (await rig.ctl.get_question(run.run_id, qid))["state"] == "expired"
    assert rig.state(run.run_id) == "running"
    assert rig.events(run.run_id, "question")[-1].outcome == "expired"
    assert (await rig.ctl.list_active())[0].pending_question is None
    assert (await tap(rig, run.run_id, qid))["outcome"] == "not_current"
    assert (await live(rig, run.run_id, qid, n=1))["outcome"] == "not_current"
    assert presses(rig) == [] and rig.state(run.run_id) == "running"


async def test_live_view_of_an_expired_question_returns_to_running(rig):
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    await live(rig, run.run_id, qid)
    await rig.ctl.begin_live(run.run_id, _expose)
    rig.clock.advance(3601)
    await rig.ctl.expire_questions()
    assert rig.state(run.run_id) == "live"
    await rig.ctl.live_done(run.run_id)
    assert rig.state(run.run_id) == "running"
    assert (await rig.ctl.get_question(run.run_id, qid))["state"] == "expired"


async def test_identical_answer_racing_a_settled_one_gets_its_stored_response(rig):
    """r1 Astra 2a: a second, identical answer that passed the outer check before the first
    settled finds the run already running; it gets the first answer's stored response, never
    ``not_ready``, and nothing executes."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    q = await rig.ctl.get_question(run.run_id, qid)
    body = {"revision": q["revision"], "digest": q["digest"], "choice": {"extra": 0},
            "operator_ref": "tg-op"}
    stale = rig.store.get_question(qid)              # the row as the racing request read it
    first = await rig.ctl.answer_question(run.run_id, qid, dict(body))
    real = rig.store.get_question
    calls = []

    def once_stale(question_id):
        calls.append(1)
        return stale if len(calls) == 1 else real(question_id)
    rig.store.get_question = once_stale
    try:
        again = await rig.ctl.answer_question(run.run_id, qid, dict(body))
    finally:
        rig.store.get_question = real
    assert again == {**first, "repeat": True} and presses(rig) == []


async def test_expiry_ends_a_live_view_never_opened(rig):
    """r3 Terra: a question that expires with a live view asked but never opened leaves no
    run waiting on that view; the run runs again and the view cannot be claimed."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    await live(rig, run.run_id, qid)
    assert rig.state(run.run_id) == "waiting_live"
    rig.clock.advance(3601)
    await rig.ctl.expire_questions()
    assert (await rig.ctl.get_question(run.run_id, qid))["state"] == "expired"
    assert rig.state(run.run_id) == "running" and rig.ctl.active.live is None
    with pytest.raises(ProtocolError, match="not_ready"):
        await rig.ctl.begin_live(run.run_id, _expose)
    assert presses(rig) == []



async def test_a_live_view_nobody_opens_ends_its_run_after_an_hour(rig):
    """casa-test 2026-11-03: a login live view the operator never opened kept its run waiting
    all night, and Fido was woken every minute for it. A view the model asked for that nobody
    opened within a question's hour cancels the run, once, with one event and no notice; the
    view cannot be claimed after."""
    run = await rig.run()
    await rig.ctl.request_live(run.run_id, "login")
    assert rig.state(run.run_id) == "waiting_live"
    rig.clock.advance(3599)
    await rig.ctl.expire_live()
    assert rig.state(run.run_id) == "waiting_live" and rig.ctl.active.live is not None
    rig.clock.advance(2)
    await rig.ctl.expire_live()
    await rig.settle()
    assert rig.state(run.run_id) == "cancelled"
    assert [(e.kind, e.outcome) for e in rig.events(run.run_id)
            if e.kind in ("live_done", "run_cancelled")] == [("live_done", "expired"),
                                                             ("run_cancelled", "cancelled")]
    assert rig.store.notices_after(0) == []
    with pytest.raises(ProtocolError):
        await rig.ctl.begin_live(run.run_id, _expose)
    await rig.ctl.expire_live()                       # nothing more to end
    assert len(rig.events(run.run_id, "run_cancelled")) == 1


async def test_the_question_sweep_ends_an_unopened_live_view(tmp_path, monkeypatch):
    """The controller's own 30-second sweep runs the live view expiry (no caller needed)."""
    monkeypatch.setattr(ctl_mod, "QUESTION_SWEEP_S", 0.01)
    r = Rig(tmp_path)
    await r.start()
    try:
        run = await r.run()
        await r.ctl.request_live(run.run_id, "login")
        r.clock.advance(3601)
        for _ in range(200):
            if r.state(run.run_id) == "cancelled":
                break
            await asyncio.sleep(0.01)
        assert r.state(run.run_id) == "cancelled"
    finally:
        await r.ctl.shutdown()
        r.close()


async def test_a_live_view_the_operator_opened_never_expires(rig):
    """The hour counts only while nobody has opened the view: one the operator is in stays his
    until Done, however long."""
    run = await rig.run()
    await rig.ctl.request_live(run.run_id, "login")
    await rig.ctl.begin_live(run.run_id, _expose)
    rig.clock.advance(3 * 3600)
    await rig.ctl.expire_live()
    assert rig.state(run.run_id) == "live"
    await rig.ctl.live_done(run.run_id)
    assert rig.state(run.run_id) == "running"
    rig.clock.advance(3 * 3600)
    await rig.ctl.expire_live()                       # no view now: nothing to end
    assert rig.state(run.run_id) == "running"


async def test_a_question_view_is_left_to_its_question(rig):
    """A view asked from a question is ended by that question's expiry, which returns the run
    to running; the live view expiry never cancels that run."""
    run, els = await _gate_run(rig, [dict(role="button", name="Pay now")])
    qid = await asked(rig, run, els)
    await live(rig, run.run_id, qid)
    rig.clock.advance(3601)
    await rig.ctl.expire_live()
    assert rig.state(run.run_id) == "waiting_live"
    await rig.ctl.expire_questions()
    await rig.ctl.expire_live()
    assert rig.state(run.run_id) == "running"


# --- slice 4: public downloads (S4-6)
# A public link download is fetched without a check; a file whose sniffed type is outside the
# document set is held as a private partial file and judged before it is kept. The downloader
# here is the real one (its storage, its quarantine, keep and discard); only the transfer's
# bytes are scripted.

PUB_PAGE = "https://pub.example/downloads"
EXE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00" + b"\0" * 600
INSTALL = {"verdict": "ask", "category": "software", "reason": "installs a program",
           "question": {"text": "Keep the installer setup.exe (616 bytes)?",
                        "confirm_label": "Keep the installer", "extra_options": [],
                        "screenshot": "none", "highlight": [], "text_incomplete": False}}
LOOK = {"verdict": "look_first", "category": "unclear", "reason": "unclear",
        "look_first": "find the release notes first"}


def _real_downloader(rig, data, mime, name):
    from casa_browser.downloads import Downloader
    dl = Downloader(rig.store, rig.registry, rig.tmp / "artifacts")
    fetched = []

    async def fetch(run, handle, session, policy, *, on_send=None, quarantine=False):
        fetched.append(quarantine)
        rig.log.append(("fetch", handle))

        async def chunks():
            yield data
        return await dl._store_stream(run, chunks(), raw_name=name, declared=mime,
                                      source_handle=handle, limit=dl.max_file,
                                      quarantine=quarantine)
    dl.fetch = fetch
    rig.ctl.downloader = dl
    return dl, fetched


def _partials(rig):
    from casa_browser.downloads import PARTIAL_PREFIX
    d = rig.tmp / "artifacts" / "files"
    return sorted(p.name for p in d.iterdir() if p.name.startswith(PARTIAL_PREFIX)) \
        if d.exists() else []


async def _public_link(rig, name="Installer", href="https://pub.example/setup.exe"):
    run = await rig.run(spec(request_id="ow", mode="open_web", sites=()))
    rig.page().show(PUB_PAGE, "Downloads", [dict(role="link", name=name, href=href)])
    obs = await observe(rig, run.run_id)
    return run, handle_of(obs, name)


async def test_public_document_kept_without_check(rig):
    """A public link download whose content is a document (here a PDF) is fetched and kept at
    once: no judgement, no partial file left."""
    _dl, fetched = _real_downloader(rig, b"%PDF-1.7\n" + b"0" * 100, "application/pdf",
                                    "report.pdf")
    run, link = await _public_link(rig, "Report", "https://pub.example/report.pdf")
    rec = await rig.ctl.download(run.run_id, link)
    assert rec.mime == "application/pdf" and rig.store.get_file(rec.file_id) is not None
    assert fetched == [True] and _partials(rig) == []
    assert rig.events(run.run_id, "judgement") == []


async def test_public_non_document_quarantined_until_go(rig):
    """An installer fetched on a public site is not kept: it becomes a pending judgement of kind
    download whose packet carries ``downloaded_file`` (kept name, sniffed type, size). A go keeps
    exactly that file, with no second fetch; an ask keeps it while the question stands, and the
    confirmed answer keeps it."""
    _dl, fetched = _real_downloader(rig, EXE_BYTES, "application/x-msdownload", "setup.exe")
    run, link = await _public_link(rig)
    wire = (await rig.ctl.download(run.run_id, link)).to_wire()
    assert wire["outcome"] == "judgement_required"
    packet = json.loads(wire["judgement"]["packet"])
    assert packet["downloaded_file"] == {"name": "setup.exe", "type": "application/octet-stream",
                                         "size": len(EXE_BYTES)}
    assert packet["where"]["session"] == "public"
    assert rig.store.files_for_run(run.run_id) == [] and len(_partials(rig)) == 1
    reply = await verdict(rig, run.run_id, wire, GO)
    assert reply["outcome"] == "done", reply
    rec = rig.store.get_file(reply["file"]["file_id"])
    assert rec is not None and rec.safe_name == "setup.exe"
    from pathlib import Path
    assert Path(rec.private_path).read_bytes() == EXE_BYTES
    assert fetched == [True] and _partials(rig) == []
    # An ask: the file waits with the question; the confirmed answer keeps it.
    obs = await observe(rig, run.run_id)
    wire = (await rig.ctl.download(run.run_id, handle_of(obs, "Installer"))).to_wire()
    reply = await verdict(rig, run.run_id, wire, INSTALL)
    assert reply["outcome"] == "asked" and len(_partials(rig)) == 1
    assert len(rig.store.files_for_run(run.run_id)) == 1
    rj = await tap(rig, run.run_id, reply["question_id"])
    assert rj["outcome"] == "rejudgement_required", rj
    rj_packet = json.loads(rj["judgement"]["packet"])
    # the re-check sees the same fetched file on both sides, so the file itself is no difference
    assert (rj_packet["current_packet"]["downloaded_file"]
            == rj_packet["approved_packet"]["downloaded_file"])
    done = await verdict(rig, run.run_id, rj, SAME)
    assert done["outcome"] == "done", done
    assert len(rig.store.files_for_run(run.run_id)) == 2 and _partials(rig) == []
    assert fetched == [True, True]


async def test_quarantine_deleted_on_look_first_or_no_verdict(rig):
    """At every end but its execution a quarantined file is deleted and never registered: a
    look first, no verdict, a declined question, a superseding command, the run's end."""
    _dl, fetched = _real_downloader(rig, EXE_BYTES, "application/x-msdownload", "setup.exe")
    run, link = await _public_link(rig)

    async def held():
        obs = await observe(rig, run.run_id)
        wire = (await rig.ctl.download(run.run_id, handle_of(obs, "Installer"))).to_wire()
        assert wire["outcome"] == "judgement_required" and len(_partials(rig)) == 1
        return wire

    outcomes = []
    for v in (LOOK, None):
        wire = await held()
        outcomes.append((await verdict(rig, run.run_id, wire, v))["outcome"])
        assert _partials(rig) == [], v
    wire = await held()
    reply = await verdict(rig, run.run_id, wire, INSTALL)
    assert len(_partials(rig)) == 1
    outcomes.append((await tap(rig, run.run_id, reply["question_id"], "not_now"))["outcome"])
    assert _partials(rig) == []
    await held()
    await observe(rig, run.run_id)                    # any command supersedes the pending one
    assert _partials(rig) == []
    await held()
    await rig.ctl.cancel(run.run_id)
    await rig.settle()
    assert _partials(rig) == []
    assert outcomes == ["look_first", "check_unavailable", "declined"]
    assert rig.store.files_for_run(run.run_id) == [] and len(fetched) == 5


# --------------------------------------------------------------------------- slice 5: logins

ITEMS = [{"item_id": "itm_runpod", "title": "RunPod", "website_hosts": ["www.runpod.io"]},
         {"item_id": "itm_work", "title": "RunPod (work)", "website_hosts": ["runpod.io"]},
         {"item_id": "itm_bank", "title": "Bank", "website_hosts": ["bank.example"]}]


async def relay_app(rig, run_id, verdict, kind="login_item"):
    """The plugin's relay of an app-started judgement, scripted: GET pending, then the verdict
    (None: no verdict) bound to its packet's digest."""
    pending = (await rig.ctl.pending_judgements(run_id))["judgements"]
    j = next(p for p in pending if p["kind"] == kind)
    body = ({"no_verdict": "check_unavailable"} if verdict is None
            else {"packet_sha256": sha(j["packet"]), "verdict": verdict})
    return j, await rig.ctl.apply_judgement(run_id, j["id"], body)


async def not_found_run(rig, items=ITEMS):
    rig.broker.login_reply = {"status": "needs_live_view", "reason": "not_found"}
    rig.broker.items_reply = {"items": items, "truncated": False}
    return await rig.run()


async def answer(rig, run_id, qid, choice):
    q = await rig.ctl.get_question(run_id, qid)
    return await rig.ctl.answer_question(run_id, qid, {
        "revision": q["revision"], "digest": q["digest"], "choice": choice,
        "operator_ref": "tap:1"})


async def test_not_found_lists_and_judges_items(rig):
    """No item linked: the vault is listed and frozen as a login_item judgement whose packet
    holds the site, the sign-in page's real host and each item's index, title and website hosts;
    the run keeps running (no login_needed yet) and a run command does not supersede it."""
    run = await not_found_run(rig)
    assert rig.state(run.run_id) == "running"
    assert rig.events(run.run_id, "login_needed") == []
    assert [e.outcome for e in rig.events(run.run_id, "login_item_judgement")] == ["pending"]
    assert ("broker", "list_login_items", run.run_id, "sa") in rig.log
    await observe(rig, run.run_id)                           # a run command meanwhile
    pending = (await rig.ctl.pending_judgements(run.run_id))["judgements"]
    assert [p["kind"] for p in pending] == ["login_item"]
    packet = json.loads(pending[0]["packet"])
    assert packet == {"kind": "login_item",
                      "site": {"name": "Site A", "sign_in_host": "a.example",
                               "registered_origins": ["https://a.example",
                                                      "https://login.a.example"]},
                      "items": [{"index": i, "title": it["title"],
                                 "website_hosts": it["website_hosts"]}
                                for i, it in enumerate(ITEMS)],
                      "items_truncated": False}


async def test_item_ids_never_in_question_or_packet(rig):
    run = await not_found_run(rig)
    j, asked = await relay_app(rig, run.run_id, {"items": [0, 1], "reason": "the brand"})
    q = await rig.ctl.get_question(run.run_id, asked["question_id"])
    shown = json.dumps([j, asked, q, [e.to_wire() for e in rig.events(run.run_id)]])
    for it in ITEMS:
        assert it["item_id"] not in shown


async def test_one_candidate_yes_no_question_copy(rig):
    """One candidate: the fixed copy with the browser's host and the vault title, Yes links
    exactly that item (the item read, never written) and signs in again; the question's answer
    is stored once."""
    run = await not_found_run(rig)
    _j, asked = await relay_app(rig, run.run_id, {"items": [1], "reason": "work account"})
    assert asked["outcome"] == "asked" and rig.state(run.run_id) == "waiting_approval"
    q = await rig.ctl.get_question(run.run_id, asked["question_id"])
    view = q["view"]
    assert view["kind"] == "login_item" and view["options"] == ["RunPod (work)"]
    assert view["text"] == "To sign in to a.example I'd use your 1Password item \"RunPod (work)\". OK?"
    assert view["confirm_offered"] is False and view["extra_options"] == []
    logins = [e for e in rig.log if e[:2] == ("broker", "login")]
    rig.broker.login_reply = {"status": "logged_in", "reason": "completed"}
    reply = await answer(rig, run.run_id, asked["question_id"], {"item": 0})
    assert reply == {"outcome": "linked", "generation": reply["generation"], "label": "RunPod (work)"}
    assert rig.broker.registered == [("sa", "itm_work", "tap:1")]
    assert rig.store.get_site("sa").vault_item_id == "itm_work"
    assert len([e for e in rig.log if e[:2] == ("broker", "login")]) == len(logins) + 1
    assert rig.state(run.run_id) == "running"
    again = await answer(rig, run.run_id, asked["question_id"], {"item": 0})
    assert again["repeat"] is True and len(rig.broker.registered) == 1


async def test_several_candidates_none_of_these(rig):
    """Several candidates: which item, titles in the check's order; None of these links
    nothing, reports login_needed, and the next navigation to the start page does not ask
    again in this run."""
    run = await not_found_run(rig)
    _j, asked = await relay_app(rig, run.run_id, {"items": [1, 0], "reason": "two"})
    view = (await rig.ctl.get_question(run.run_id, asked["question_id"]))["view"]
    assert view["options"] == ["RunPod (work)", "RunPod"]
    assert view["text"] == "To sign in to a.example, which 1Password item?"
    with pytest.raises(ctl_mod.ProtocolError):
        await answer(rig, run.run_id, asked["question_id"], "confirm")
    reply = await answer(rig, run.run_id, asked["question_id"], "not_now")
    assert reply["outcome"] == "declined" and rig.broker.registered == []
    assert [e.outcome for e in rig.events(run.run_id, "login_needed")] == ["not_found"]
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Open site"), obs)
    assert [e.outcome for e in rig.events(run.run_id, "login_needed")] == ["not_found", "not_found"]
    assert (await rig.ctl.pending_judgements(run.run_id))["judgements"] == []


@pytest.mark.parametrize("case", ["empty_vault", "no_fit", "no_verdict", "index_outside"])
async def test_no_candidate_goes_to_live_view(rig, case):
    """No item, no fitting item, no verdict or an index the packet does not hold: no question,
    login_needed (the live view), nothing linked."""
    run = await not_found_run(rig, [] if case == "empty_vault" else ITEMS)
    if case != "empty_vault":
        verdict = {"no_fit": {"items": [], "reason": "none"}, "no_verdict": None,
                   "index_outside": {"items": [3], "reason": "x"}}[case]
        _j, reply = await relay_app(rig, run.run_id, verdict)
        assert reply["outcome"] == "login_needed"
    assert [e.outcome for e in rig.events(run.run_id, "login_needed")] == ["not_found"]
    assert rig.state(run.run_id) == "running" and rig.broker.registered == []
    assert rig.store.open_question(run.run_id) is None


async def test_identical_titles_are_told_apart_or_offered_once(rig):
    """d1 Astra A: the operator always sees what tells two candidates apart."""
    same = [{"item_id": "a1", "title": "RunPod", "website_hosts": ["runpod.io"]},
            {"item_id": "a2", "title": "RunPod", "website_hosts": ["console.runpod.io"]},
            {"item_id": "a3", "title": "RunPod", "website_hosts": ["runpod.io"]}]
    run = await not_found_run(rig, same)
    _j, asked = await relay_app(rig, run.run_id, {"items": [0, 1, 2], "reason": "same"})
    view = (await rig.ctl.get_question(run.run_id, asked["question_id"]))["view"]
    assert view["options"] == ["RunPod (runpod.io)", "RunPod (console.runpod.io)"]
    await answer(rig, run.run_id, asked["question_id"], {"item": 1})
    assert rig.broker.registered == [("sa", "a2", "tap:1")]


async def test_a_login_question_expires_to_the_live_view(rig):
    run = await not_found_run(rig)
    _j, asked = await relay_app(rig, run.run_id, {"items": [0], "reason": "x"})
    q = await rig.ctl.get_question(run.run_id, asked["question_id"])
    with pytest.raises(ctl_mod.ProtocolError):      # no live view from a login question
        await rig.ctl.live_from_question(run.run_id, asked["question_id"],
                                         {"revision": 1, "digest": q["digest"], "n": 1,
                                          "operator_ref": "tap:2"})
    rig.clock.advance(3601)
    await rig.ctl.expire_questions()
    assert rig.state(run.run_id) == "running"
    assert [e.outcome for e in rig.events(run.run_id, "login_needed")] == ["not_found"]


async def test_a_login_item_waits_while_the_run_is_not_running(rig):
    """Its question needs the run: while the run waits on something else the judgement is not
    served (no check is spent) and a verdict for it is refused, the judgement kept."""
    run = await not_found_run(rig)
    j = (await rig.ctl.pending_judgements(run.run_id))["judgements"][0]
    assert rig.store.transition(run.run_id, "running", "waiting_approval")
    assert (await rig.ctl.pending_judgements(run.run_id))["judgements"] == []
    with pytest.raises(ctl_mod.ProtocolError, match="not_ready"):
        await rig.ctl.apply_judgement(run.run_id, j["id"], {
            "packet_sha256": sha(j["packet"]), "verdict": {"items": [0], "reason": "x"}})
    assert rig.store.transition(run.run_id, "waiting_approval", "running")
    _j, asked = await relay_app(rig, run.run_id, {"items": [0], "reason": "x"})
    assert asked["outcome"] == "asked"


# --------------------------------------------------------------------------- slice 6: codes

NEEDS_CODE = {"status": "needs_code", "code_field": True, "totp_held": False}


def _code(code="482915", site_id="sa"):
    from casa_browser.protocol import LoginCode
    return LoginCode(code, site_id)


def _driver_sessions(rig):
    opened = [e[2] for e in rig.log if e[0] == "open_session"]
    closed = [e[2] for e in rig.log if e[0] == "close_session"]
    return opened, closed


async def _waiting_for_code(rig):
    rig.broker.login_reply = NEEDS_CODE
    run = await rig.run()
    return run


async def test_needs_code_leaves_the_page_and_names_the_site(rig):
    run = await _waiting_for_code(rig)
    needed = rig.events(run.run_id, "login_needed")
    assert [e.outcome for e in needed] == ["needs_code"]
    assert "Site A" in needed[0].text_masked and "code" in needed[0].text_masked
    assert rig.state(run.run_id) == "running" and not rig.driver.sessions
    (active,) = await rig.ctl.list_active()
    assert active.to_wire()["code_site"] == "sa"
    obs = await observe(rig, run.run_id)               # Fido can look at the code page
    assert obs.generation >= 0


async def test_login_code_opens_and_closes_its_own_driver_session(rig):
    """S6-3: every path — success, refusal, cancel mid-fill — opens one fresh login driver
    session and leaves zero open."""
    run = await _waiting_for_code(rig)
    before = len(_driver_sessions(rig)[0])
    generation = rig.browser.session.view_generation
    result = await rig.ctl.login_code(run.run_id, _code())
    assert result.to_wire()["outcome"] == "done"
    assert rig.broker.filled_generations == [generation]      # the page that asked
    opened, closed = _driver_sessions(rig)
    assert len(opened) == before + 1 and sorted(opened) == sorted(set(closed))
    assert not rig.driver.sessions and rig.broker.codes == ["482915"]
    assert [e for e in rig.log if e[0] == "open_session"][-1][1] == "login"
    assert (await rig.ctl.list_active())[0].to_wire()["code_site"] is None
    # A refusal (the code page is gone).
    rig.ctl.active.code_wait = None
    rig.broker.login_reply = NEEDS_CODE
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Site A"), obs)
    rig.broker.fill_reply = {"status": "needs_live_view", "reason": "code_page_gone"}
    result = await rig.ctl.login_code(run.run_id, _code())
    assert result.to_wire() == {"outcome": "login_needed", "generation": result.generation,
                                "reason": "code_page_gone"}
    assert rig.events(run.run_id, "login_needed")[-1].outcome == "code_page_gone"
    opened, closed = _driver_sessions(rig)
    assert sorted(opened) == sorted(set(closed)) and not rig.driver.sessions
    gate = asyncio.Event()

    async def hang(args):
        await gate.wait()
    rig.broker.fill_hook = hang
    # A live view started while the broker fills cuts the fill; the run goes on.
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Site A"), obs)
    filling = asyncio.create_task(rig.ctl.login_code(run.run_id, _code()))
    await asyncio.sleep(0.02)
    assert rig.driver.sessions
    ctl_mod.DRAIN_S, saved = 0.02, ctl_mod.DRAIN_S
    try:
        await rig.ctl.request_live(run.run_id, "assist")
    finally:
        ctl_mod.DRAIN_S = saved
    with pytest.raises(ProtocolError):
        await filling
    opened, closed = _driver_sessions(rig)
    assert sorted(opened) == sorted(set(closed)) and not rig.driver.sessions
    assert rig.state(run.run_id) not in ("cancelled", "interrupted")
    await rig.ctl.live_done(run.run_id)
    # A cancel while the broker fills.
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Site A"), obs)
    filling = asyncio.create_task(rig.ctl.login_code(run.run_id, _code()))
    await asyncio.sleep(0.02)
    assert rig.driver.sessions
    await rig.ctl.cancel(run.run_id)
    with pytest.raises(ProtocolError):
        await filling
    opened, closed = _driver_sessions(rig)
    assert sorted(opened) == sorted(set(closed)) and not rig.driver.sessions
    assert rig.state(run.run_id) == "cancelled"


async def test_login_code_broker_error_interrupts_and_closes(rig):
    run = await _waiting_for_code(rig)
    rig.broker.fill_reply = {"error": "refused"}
    with pytest.raises(ProtocolError, match="interrupted"):
        await rig.ctl.login_code(run.run_id, _code())
    opened, closed = _driver_sessions(rig)
    assert sorted(opened) == sorted(set(closed)) and not rig.driver.sessions
    assert rig.state(run.run_id) == "interrupted"


async def test_login_code_without_needs_code_is_not_ready(rig):
    run = await rig.run()                              # signed in: no code was asked for
    opens = len(_driver_sessions(rig)[0])
    with pytest.raises(ProtocolError, match="not_ready"):
        await rig.ctl.login_code(run.run_id, _code())
    assert rig.broker.codes == [] and len(_driver_sessions(rig)[0]) == opens


@pytest.mark.parametrize("case", ["other_site", "used_twice", "new_login"])
async def test_login_code_for_another_report_is_not_ready(rig, case):
    run = await _waiting_for_code(rig)
    site_id = "sa"
    if case == "other_site":
        site_id = "sc"
    elif case == "used_twice":
        await rig.ctl.login_code(run.run_id, _code())
    else:                                              # a later sign-in replaced the report
        rig.broker.login_reply = {"status": "logged_in", "reason": "completed"}
        obs = await observe(rig, run.run_id)
        await act(rig, run.run_id, "navigate", handle_of(obs, "Site A"), obs)
    codes = list(rig.broker.codes)
    with pytest.raises(ProtocolError, match="not_ready"):
        await rig.ctl.login_code(run.run_id, _code(site_id=site_id))
    assert rig.broker.codes == codes


@pytest.mark.parametrize("change", ["address", "reload", "other_tab", "session"])
async def test_login_code_after_the_page_moved_fills_nothing(rig, change):
    """Rev 2 (Terra d1, d2): the code is for the very page that reported it: another address, a
    reload of the same address, or a new browser session is ``code_page_gone`` with no driver
    session and no fill."""
    run = await _waiting_for_code(rig)
    if change == "address":
        rig.page()._navigated("https://login.a.example/other")
    elif change == "reload":
        rig.page()._navigated(rig.page().url)
    elif change == "other_tab":
        # Astra r1: the reporting tab closed; another tab of the same session is now the
        # session's page, with the generation unchanged.
        old = rig.page()
        other = type(old)(old._session, old.log, old.contents)
        other.url = old.url
        rig.browser.session.context.pages.append(other)
        old.closed = True
        assert rig.browser.session.page is other
    else:
        await rig.browser.close(session=rig.browser.session, force=True)
        await rig.browser.open("sa", "account", rig.ctl.c.locale)
        rig.ctl.active.session = rig.browser.session
        rig.page()._navigated(A_START)
    opens = len(_driver_sessions(rig)[0])
    result = await rig.ctl.login_code(run.run_id, _code())
    assert result.to_wire()["outcome"] == "login_needed"
    assert result.to_wire()["reason"] == "code_page_gone"
    assert rig.broker.codes == [] and len(_driver_sessions(rig)[0]) == opens
    with pytest.raises(ProtocolError, match="not_ready"):     # the report is used up
        await rig.ctl.login_code(run.run_id, _code())


async def test_a_second_code_step_asks_again(rig):
    run = await _waiting_for_code(rig)
    rig.broker.fill_reply = NEEDS_CODE
    result = await rig.ctl.login_code(run.run_id, _code())
    assert (result.outcome, result.extra["reason"]) == ("login_needed", "needs_code")
    assert (await rig.ctl.list_active())[0].to_wire()["code_site"] == "sa"
    rig.broker.fill_reply = {"status": "logged_in", "reason": "completed"}
    assert (await rig.ctl.login_code(run.run_id, _code("777777"))).outcome == "done"
    assert rig.broker.codes == ["482915", "777777"]


async def test_the_code_is_in_no_event_or_record(rig, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    run = await _waiting_for_code(rig)
    code = "Z9-" + "71k3"
    await rig.ctl.login_code(run.run_id, _code(code))
    texts = json.dumps([e.to_wire() for e in rig.events(run.run_id)])
    assert code not in texts and code not in caplog.text
    assert "A sign-in code was given for Site A." in texts
    assert code not in repr(rig.ctl.active) and code not in json.dumps(
        [a.to_wire() for a in await rig.ctl.list_active()])
    for path in rig.tmp.rglob("*"):
        if path.is_file():
            assert code.encode() not in path.read_bytes(), path


# --------------------------------------------------------------------------- lazy guest unlock


def guest_sessions(rig):
    return len([e for e in rig.log if e[:2] == ("open_session", "guest")])


def sign_in_ops(rig):
    return [e[1] for e in rig.log if e[0] == "broker" and e[1] in ("login", "unlock_guest")]


async def back_to_running(rig, run_id):
    async def expose():
        pass
    await rig.ctl.begin_live(run_id, expose)
    await rig.ctl.live_done(run_id)
    assert rig.state(run_id) == "running"


async def open_site_a(rig, run_id):
    obs = await observe(rig, run_id)
    return await act(rig, run_id, "navigate", handle_of(obs, "Site A"), obs)


@pytest.mark.parametrize("reply", [{"status": "logged_in", "reason": "completed"},
                                   {"status": "needs_code", "code_field": True,
                                    "totp_held": False},
                                   {"status": "no_form", "reason": "no_form"}])
async def test_a_sign_in_with_no_passkey_hint_opens_no_guest_session(rig, reply):
    """A password sign-in, an email-code sign-in and a start page already signed in by cookie
    never run the guest flow: no popup tab, no unlock, no new-sign-in email."""
    rig.broker.login_reply = reply
    run = await rig.run()
    assert guest_sessions(rig) == 0
    assert sign_in_ops(rig) == ["login"] and rig.broker.guest_asks == [True]
    assert rig.state(run.run_id) == "running"


async def test_a_forced_close_in_the_guest_flow_ends_the_sign_in(rig):
    """K1 inside the guest flow: the session is gone, so no second sign-in; the run says
    ``login_needed timeout`` and goes on."""
    rig.broker.passkey_hint = True
    rig.driver.close_modes = {"guest": "forced"}
    run = await rig.run()
    assert sign_in_ops(rig) == ["login", "unlock_guest"]
    assert [e.outcome for e in rig.events(run.run_id, "login_needed")] == ["timeout"]
    assert rig.browser.session is None and rig.state(run.run_id) == "running"


async def test_the_guest_flow_runs_once_per_browser_session(rig):
    """A later sign-in in the same browser session never asks again (``guest_ask`` False); a
    new browser session (a profile switch and back) asks again, and unlocks once more."""
    rig.broker.passkey_hint = True
    run = await rig.run(sites=(("sa", "account"), ("sp", "public")))
    assert guest_sessions(rig) == 1
    await open_site_a(rig, run.run_id)               # the same session: no guest asked
    assert rig.broker.guest_asks == [True, False, False]
    assert guest_sessions(rig) == 1
    obs = await observe(rig, run.run_id)
    await act(rig, run.run_id, "navigate", handle_of(obs, "Public B"), obs)
    assert rig.browser.session.kind == "public"
    await open_site_a(rig, run.run_id)               # a new account session: asked again
    assert rig.broker.guest_asks == [True, False, False, True, False]
    assert guest_sessions(rig) == 2
    assert rig.events(run.run_id, "login_needed") == []


async def test_a_guest_flow_cancelled_in_its_cleanup_still_counts(rig, monkeypatch):
    """r3: a live-view request's drain cuts a command while the guest flow cleans up. The flow
    counts for that browser session all the same: the sign-in view and the next sign-in in that
    session open no second guest session."""
    monkeypatch.setattr(ctl_mod, "DRAIN_S", 0.05)
    run = await rig.run()
    assert guest_sessions(rig) == 0
    rig.broker.passkey_hint = True
    real_close = rig.driver.close_session
    hang = asyncio.Event()

    async def slow_close(sid):
        kind = rig.driver.sessions.get(sid, ("",))[0]
        await real_close(sid)
        if kind == "guest" and not hang.is_set():
            hang.set()                               # once: a second guest flow is counted,
            await asyncio.Event().wait()             # never hung; this cleanup outlasts the drain
    rig.driver.close_session = slow_close
    action = asyncio.create_task(open_site_a(rig, run.run_id))
    for _ in range(100):
        await asyncio.sleep(0)
        if guest_sessions(rig) == 1 and not rig.driver.sessions:
            break
    await rig.ctl.request_live(run.run_id, "login")
    with pytest.raises(ProtocolError, match="not_ready"):
        await action
    rig.driver.close_session = real_close
    assert rig.state(run.run_id) == "waiting_live" and guest_sessions(rig) == 1
    await back_to_running(rig, run.run_id)
    await open_site_a(rig, run.run_id)
    assert guest_sessions(rig) == 1 and rig.broker.guest_asks[-1] is False


@pytest.mark.parametrize("hinted", [False, True])
async def test_a_sign_in_live_view_unlocks_the_guest_before_its_link(rig, hinted):
    """Ruling 2: a ``login`` view's request runs the guest flow before the run waits for the
    operator, once per browser session: none when a sign-in already ran it in this session,
    and none for a second view in it."""
    rig.broker.passkey_hint = hinted
    run = await rig.run()
    states = []

    async def at_unlock(args):
        states.append(rig.state(run.run_id))
    rig.broker.unlock_hook = at_unlock
    await rig.ctl.request_live(run.run_id, "login")
    assert rig.state(run.run_id) == "waiting_live"
    assert guest_sessions(rig) == 1
    assert states == ([] if hinted else ["running"])
    await back_to_running(rig, run.run_id)
    await rig.ctl.request_live(run.run_id, "login")
    assert guest_sessions(rig) == 1


async def test_an_assist_live_view_does_not_unlock_the_guest(rig):
    run = await rig.run()
    await rig.ctl.request_live(run.run_id, "assist")
    assert rig.state(run.run_id) == "waiting_live" and guest_sessions(rig) == 0


async def test_a_forced_close_in_the_views_guest_flow_still_grants_the_view(rig):
    """K1 inside the sign-in view's guest flow: the request goes on; the claim opens a new
    session (its guest not run: a declared residual) and the view starts."""
    run = await rig.run()
    rig.driver.close_modes = {"guest": "forced"}
    grant = await rig.ctl.request_live(run.run_id, "login")
    assert grant.grant_id and rig.state(run.run_id) == "waiting_live"
    assert guest_sessions(rig) == 1 and rig.browser.session is None
    rig.driver.close_modes = {}

    async def expose():
        pass
    await rig.ctl.begin_live(run.run_id, expose)
    assert rig.state(run.run_id) == "live" and rig.browser.session is not None


async def test_a_sign_in_view_after_a_forced_close_reopens_and_unlocks(rig):
    """r4 Terra: after a sign-in's K1 (no session), the sign-in view's request opens the run's
    session again and runs the guest flow on it, exactly once, then the view starts."""
    rig.driver.close_modes = {"login": "forced"}
    run = await rig.run()
    assert rig.ctl.active.session is None and rig.browser.session is None
    rig.driver.close_modes = {}
    opens = rig.log.kinds().count("open")
    await rig.ctl.request_live(run.run_id, "login")
    assert rig.log.kinds().count("open") == opens + 1
    assert guest_sessions(rig) == 1 and rig.state(run.run_id) == "waiting_live"

    async def expose():
        pass
    await rig.ctl.begin_live(run.run_id, expose)
    assert rig.state(run.run_id) == "live"
    assert rig.log.kinds().count("open") == opens + 1


async def test_a_sign_in_view_after_a_drain_cancelled_forced_close_reopens_and_unlocks(
        rig, monkeypatch):
    """r4 Astra: the drain cuts a sign-in whose driver close killed the browser (K1) before
    the controller noticed: the run's session is stale. The sign-in view's request opens a
    new one, runs the guest flow on it exactly once, and the view starts (not interrupted)."""
    monkeypatch.setattr(ctl_mod, "DRAIN_S", 0.05)
    run = await rig.run()
    real_close = rig.driver.close_session
    hang = asyncio.Event()

    async def forced_then_hang(sid):
        kind = rig.driver.sessions.get(sid, ("",))[0]
        if kind == "login" and not hang.is_set():
            rig.driver.close_modes = {"login": "forced"}
        await real_close(sid)
        rig.driver.close_modes = {}
        if kind == "login" and not hang.is_set():
            hang.set()
            await asyncio.Event().wait()
    rig.driver.close_session = forced_then_hang
    action = asyncio.create_task(open_site_a(rig, run.run_id))
    for _ in range(100):
        await asyncio.sleep(0)
        if rig.browser.session is None:
            break
    await rig.ctl.request_live(run.run_id, "login")
    with pytest.raises(ProtocolError, match="not_ready"):
        await action
    rig.driver.close_session = real_close
    assert rig.state(run.run_id) == "waiting_live"
    assert guest_sessions(rig) == 1 and rig.browser.session is rig.ctl.active.session

    async def expose():
        pass
    await rig.ctl.begin_live(run.run_id, expose)
    assert rig.state(run.run_id) == "live"


async def test_a_needs_guest_reply_to_a_sign_in_that_did_not_ask_is_not_retried(rig):
    """At most one retry per sign-in: a ``needs_guest`` the broker gives a sign-in asked with
    ``guest_ask`` False (it never does) is that sign-in's reason, not another guest flow."""
    rig.broker.login_reply = {"status": "needs_guest", "reason": "passkey"}
    run = await rig.run()
    assert sign_in_ops(rig) == ["login", "unlock_guest", "login"]
    assert rig.broker.guest_asks == [True, False]
    assert [e.outcome for e in rig.events(run.run_id, "login_needed")] == ["passkey"]
