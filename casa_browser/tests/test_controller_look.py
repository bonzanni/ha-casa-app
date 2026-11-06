"""Signing in off the start page, the run controller's side: the look at ``observe`` with
``sign_in``, the ``sign_in`` judgement, its verdict and the sign-in on the judged page, over the
controller's doubles. Synthetic sites and values."""
from __future__ import annotations

import pytest

from casa_browser.protocol import Action, Observation, ProtocolError
from controllerfakes import Rig

pytestmark = pytest.mark.asyncio

START = "https://a.example/start"
LOGIN = "https://a.example/login"
FORGOT = "https://a.example/forgot_password"
READ = {"found": True, "passkey": False, "fields": [
    {"id": "f1", "role": None, "label": "Username", "password": False, "button": "Login"},
    {"id": "f2", "role": "password", "label": "Password", "password": True, "button": "Login"}]}
SIGN_IN = {"verdict": "sign_in", "reason": "the site's login form",
           "fields": {"username": "f1", "password": "f2"}}
NOT_SIGN_IN = {"verdict": "not_sign_in", "reason": "a password reset form"}
GO = {"verdict": "go", "category": "reading", "reason": "opens a page"}


@pytest.fixture
async def rig(tmp_path):
    r = Rig(tmp_path)
    await r.start()
    yield r
    await r.ctl.shutdown()
    r.close()


def sha(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pages(rig):
    link = lambda name, href: {"role": "link", "name": name, "href": href}  # noqa: E731
    rig.browser.contents[START] = {"text": "Welcome to the-internet", "elements": [
        link("Form Authentication", LOGIN), link("Forgotten Password", FORGOT)]}
    rig.browser.contents[LOGIN] = {"text": "Login Page\nUsername\nPassword", "elements": [
        {"role": "textbox", "name": "Username", "field": True},
        {"role": "textbox", "name": "Password", "field": True, "input_type": "password"},
        {"role": "button", "name": "Login"}]}
    rig.browser.contents[FORGOT] = {"text": "Forgot Password\nE-mail", "elements": [
        {"role": "textbox", "name": "E-mail", "field": True},
        {"role": "button", "name": "Retrieve password"}]}


def handle_of(obs, label):
    return next(e.handle for e in obs.elements if label in e.label_masked)


async def _step(rig, run_id, kind, label=None, **kw):
    """A step of Fido's, judged go where the gate judges it."""
    obs = await rig.ctl.observe(run_id)
    handle = handle_of(obs, label) if label else obs.page_handle
    result = await rig.ctl.act(run_id, Action(kind, handle, obs.generation, **kw))
    wire = result.to_wire()
    if wire.get("outcome") == "judgement_required":
        j = wire["judgement"]
        wire = await rig.ctl.apply_judgement(run_id, j["id"], {"packet_sha256": sha(j["packet"]),
                                                               "verdict": GO})
    return wire


async def _scroll(rig, run_id):
    return await _step(rig, run_id, "scroll", direction="down", amount="step")


async def phone(rig, *, start_reply=None):
    """The phone sequence (casa-test, 2026-11-02): the start page is the root, which has no
    form (``no_form``); Fido's judged step opens /login."""
    _pages(rig)
    rig.broker.login_reply = start_reply or {"status": "no_form", "reason": "no_form"}
    run = await rig.run()
    await _step(rig, run.run_id, "navigate", "Form Authentication")
    assert rig.page().url == LOGIN
    rig.broker.sign_in_read = READ
    rig.broker.login_reply = {"status": "logged_in", "reason": "completed"}
    return run


async def look(rig, run_id):
    return await rig.ctl.observe(run_id, sign_in=True)


async def verdict(rig, run_id, result, v=SIGN_IN):
    j = result.to_wire()["judgement"] if hasattr(result, "to_wire") else result["judgement"]
    body = {"no_verdict": "check_unavailable"} if v is None else {
        "packet_sha256": sha(j["packet"]), "verdict": v}
    return await rig.ctl.apply_judgement(run_id, j["id"], body)


def reads(rig):
    return [e for e in rig.log if e[:2] == ("broker", "read_sign_in")]


def auto_logins(rig):
    return [a for a in rig.broker.logins if a.get("auto")]


def gotos_since(rig, mark):
    return [e for e in rig.log[mark:] if e[0] == "goto"]


async def test_the_phone_sequence_signs_in_on_the_page_it_landed_on(rig):
    run = await phone(rig)
    result = await look(rig, run.run_id)
    assert result.outcome == "judgement_required" and len(reads(rig)) == 1
    j = result.to_wire()["judgement"]
    assert j["kind"] == "sign_in"
    import json
    packet = json.loads(j["packet"])
    assert packet["planned"] == {"passkey": False, "fields": [
        {"id": "f1", "role": None, "label": "Username", "password_field": False,
         "button": "Login"},
        {"id": "f2", "role": "password", "label": "Password", "password_field": True,
         "button": "Login"}]}
    assert packet["site"]["host"] == "a.example" and packet["page"]["path"] == "/login"
    mark = len(rig.log)
    reply = await verdict(rig, run.run_id, result)
    assert len(auto_logins(rig)) == 1 and gotos_since(rig, mark) == []
    assert auto_logins(rig)[0]["fields"] == {"username": "f1", "password": "f2"}
    assert [e.outcome for e in rig.events(run.run_id, "signed_in")] == ["logged_in"]
    assert rig.events(run.run_id, "login_needed") == []
    assert reply["outcome"] == "done"
    assert reply["observation"]["notice"] == "Casa Browser signed in to Site A by itself."


async def test_a_page_judged_not_a_sign_in_is_never_filled_nor_asked_about_again(rig):
    run = await phone(rig)
    reply = await verdict(rig, run.run_id, await look(rig, run.run_id), NOT_SIGN_IN)
    assert auto_logins(rig) == [] and reply["observation"]["notice"] is None
    await _scroll(rig, run.run_id)                              # a step
    rig.page().show("https://a.example/login2", "Login Page")   # another address
    again = await look(rig, run.run_id)
    assert isinstance(again, Observation)                       # the same shape: no judgement
    assert len(reads(rig)) == 2 and auto_logins(rig) == []


async def test_no_verdict_never_signs_in(rig):
    run = await phone(rig)
    reply = await verdict(rig, run.run_id, await look(rig, run.run_id), None)
    assert auto_logins(rig) == [] and reply["outcome"] == "done"
    assert [e.outcome for e in rig.events(run.run_id, "judgement")][-1] == "no_verdict"


@pytest.mark.parametrize("v", [{"verdict": "sign_in"}, {"verdict": "yes", "reason": ""},
                               {"verdict": "sign_in", "reason": "x" * 601}])
async def test_a_verdict_outside_its_bounds_is_no_verdict(rig, v):
    run = await phone(rig)
    await verdict(rig, run.run_id, await look(rig, run.run_id), v)
    assert auto_logins(rig) == []


async def test_a_step_between_the_look_and_the_verdict_cancels_the_sign_in(rig):
    run = await phone(rig)
    result = await look(rig, run.run_id)
    await _scroll(rig, run.run_id)                              # a step, the same address
    assert rig.page().url == LOGIN
    await verdict(rig, run.run_id, result)
    assert auto_logins(rig) == []


async def test_a_page_that_changed_since_the_look_is_never_filled(rig):
    run = await phone(rig)
    result = await look(rig, run.run_id)
    rig.page().text = "Confirm with your password\nPassword"   # same address, other text
    await verdict(rig, run.run_id, result)
    assert auto_logins(rig) == []


async def test_an_address_is_looked_at_once_between_two_steps(rig):
    run = await phone(rig)
    rig.broker.sign_in_read = None                              # no sign-in fields
    await look(rig, run.run_id)
    await look(rig, run.run_id)
    assert len(reads(rig)) == 1
    rig.page().show("https://a.example/other", "Other")         # A -> B -> A, no step
    await look(rig, run.run_id)
    rig.page().show(LOGIN, "Login Page")
    await look(rig, run.run_id)
    assert len(reads(rig)) == 2
    await _scroll(rig, run.run_id)                              # a step: looked again
    await look(rig, run.run_id)
    assert len(reads(rig)) == 3


@pytest.mark.parametrize("first, more", [
    ({"status": "logged_in", "reason": "completed"}, 1),
    ({"status": "failed", "reason": "rejected"}, 0),
    ({"status": "needs_code", "code_field": True, "totp_held": False}, 0),
    ({"status": "failed", "reason": "timeout"}, 0),
    ({"status": "no_form", "reason": "passkey"}, 0),
    ({"status": "no_form", "reason": "page_moved"}, 2),
    ({"status": "needs_live_view", "reason": "not_found"}, 2)])
async def test_the_ledger_allows_one_sign_in_and_one_more_after_success(rig, first, more):
    """R4: after a look's sign-in that worked, exactly one more, then none; after one that
    failed (rejected, a code, a timeout, a passkey that claims nothing), none; an outcome that
    acted on nothing is not counted."""
    run = await phone(rig)
    rig.broker.login_reply = first
    await verdict(rig, run.run_id, await look(rig, run.run_id))
    rig.broker.login_reply = {"status": "logged_in", "reason": "completed"}
    rig.ctl.active.code_wait = None                             # a code report stops looks too
    for _ in range(3):
        await _scroll(rig, run.run_id)
        result = await look(rig, run.run_id)
        if result.to_wire().get("outcome") == "judgement_required":
            await verdict(rig, run.run_id, result)
    assert len(auto_logins(rig)) == 1 + more


@pytest.mark.parametrize("start_reply", [{"status": "no_form", "reason": "no_form"},
                                         {"status": "needs_live_view", "reason": "not_found"}])
async def test_a_start_page_that_acted_on_nothing_leaves_the_look(rig, start_reply):
    """Astra r3: the vault card, Yes, the root (no form, or no item at the start page), then a
    judged click to /login: still one sign-in."""
    run = await phone(rig, start_reply=start_reply)
    await verdict(rig, run.run_id, await look(rig, run.run_id))
    assert len(auto_logins(rig)) == 1


async def test_a_start_page_sign_in_that_failed_leaves_no_look(rig):
    run = await phone(rig, start_reply={"status": "failed", "reason": "rejected"})
    assert isinstance(await look(rig, run.run_id), Observation) and reads(rig) == []


async def test_a_plain_observe_never_looks(rig):
    run = await phone(rig)
    assert isinstance(await rig.ctl.observe(run.run_id), Observation) and reads(rig) == []


@pytest.mark.parametrize("case", ["public", "open_web", "no_item", "code_wait", "blank",
                                  "no_login_origins"])
async def test_nothing_is_read_where_no_look_can_sign_in(rig, case):
    from dataclasses import replace
    from controllerfakes import ACCOUNT
    _pages(rig)
    rig.broker.sign_in_read = READ
    if case == "open_web":
        run = await rig.run(mode="open_web", sites=(("sp", "public"),))
    elif case == "public":
        run = await rig.run(sites=(("sa", "account"), ("sp", "public")))
        await rig.ctl._switch_for(rig.ctl.active, "https://b.example/")
        rig.page().show("https://b.example/login", "Login")
    else:
        if case == "no_item":
            rig.store.put_site(replace(ACCOUNT, vault_item_id=None))
        if case == "no_login_origins":
            rig.store.put_site(replace(ACCOUNT, login_origins=()))
        rig.broker.login_reply = {"status": "no_form", "reason": "no_form"}
        run = await rig.run()
        rig.page().show(LOGIN, "Login Page")
        if case == "code_wait":
            rig.ctl.active.code_wait = ("sa", rig.browser.session, 1, rig.page())
        if case == "blank":
            rig.page().url = "about:blank"
    await look(rig, run.run_id)
    assert reads(rig) == []


async def test_the_guest_unlock_reloads_and_judges_the_page_afresh(rig):
    """A passkey hint at the judged page: the guest flow, one load of the looked address and a
    fresh look, whose judgement is the verdict's successor; its own ``sign_in`` signs in with
    no second unlock and the password already sent carried. The page served on the reload is
    judged again, never filled on the old verdict."""
    run = await phone(rig)
    rig.broker.passkey_hint = True
    rig.broker.login_reply = {"status": "needs_guest", "reason": "passkey", "password_sent": True}
    first = await look(rig, run.run_id)
    mark = len(rig.log)
    rig.broker.passkey_hint = False
    reply = await verdict(rig, run.run_id, first)
    assert len([e for e in rig.log[mark:] if e[:2] == ("open_session", "guest")]) == 1
    assert [e[1] for e in gotos_since(rig, mark)] == [LOGIN]
    assert len(reads(rig)) == 2
    assert reply["outcome"] == "judgement_required"
    assert reply["judgement"]["kind"] == "sign_in"
    assert len(rig.broker.logins) == 2                          # start page + the look's
    rig.broker.login_reply = {"status": "logged_in", "reason": "completed"}
    done = await verdict(rig, run.run_id, reply)
    last = rig.broker.logins[-1]
    assert len(auto_logins(rig)) == 2 and last["guest_ask"] is False
    assert last["password_sent"] is True
    assert done["observation"]["notice"] == "Casa Browser signed in to Site A by itself."


async def test_the_reloaded_page_judged_not_a_sign_in_is_not_filled(rig):
    run = await phone(rig)
    rig.broker.login_reply = {"status": "needs_guest", "reason": "passkey"}
    rig.broker.passkey_hint = True
    reply = await verdict(rig, run.run_id, await look(rig, run.run_id))
    rig.broker.passkey_hint = False
    await verdict(rig, run.run_id, reply, NOT_SIGN_IN)
    assert len(auto_logins(rig)) == 1


async def test_the_sign_in_judgement_is_never_listed_as_pending(rig):
    run = await phone(rig)
    result = await look(rig, run.run_id)
    assert result.outcome == "judgement_required"
    assert await rig.ctl.pending_judgements(run.run_id) == {"judgements": []}


async def test_a_look_that_needs_the_operator_says_so_in_its_notice(rig):
    run = await phone(rig)
    rig.broker.login_reply = {"status": "failed", "reason": "rejected"}
    reply = await verdict(rig, run.run_id, await look(rig, run.run_id))
    assert [e.outcome for e in rig.events(run.run_id, "login_needed")] == ["rejected"]
    assert reply["observation"]["notice"] == "The site needs the operator in a login live view."


# -- the observe route -----------------------------------------------------------------------

async def test_the_observe_route_takes_the_flag_and_nothing_else(aiohttp_client, tmp_path):
    from apihelpers import headers, paired_client, run_body
    client, fake, secret = await paired_client(aiohttp_client, tmp_path)
    h = headers(secret)
    rid = (await (await client.post("/v1/runs", json=run_body(), headers=h)).json())["run_id"]
    fake.store.lease_next_run()
    fake.store.transition(rid, "starting", "running")
    for body, status in (({}, 200), ({"sign_in": True}, 200), ({"sign_in": False}, 200),
                         ({"sign_in": 1}, 400), ({"sign_in": True, "x": 1}, 400),
                         ({"look": True}, 400)):
        resp = await client.post(f"/v1/runs/{rid}/observe", json=body, headers=h)
        assert resp.status == status, body
    assert fake.observe_flags == [False, True, False]
    assert ProtocolError                                        # imported for the route's codes


async def test_the_packet_and_the_reading_the_verdict_is_held_to_are_one_read(rig):
    """Astra (applied diff r3): the page the check is shown and the bound reading the verdict
    is held to come from one pass over the page, so a control changed right after it (a box
    ticked by the page) is a difference at the verdict: 0 sign-ins."""
    run = await phone(rig)
    page = rig.page()
    start = page.reads

    def tick(p, n):
        if n == start + 2:               # the read after the look's one pass
            p.elements[0].value = "changed by the page"
    page.read_hook = tick
    result = await look(rig, run.run_id)
    assert page.reads == start + 1
    await verdict(rig, run.run_id, result)
    assert auto_logins(rig) == []



@pytest.mark.parametrize("fields, sent", [
    ({"username": "f1", "password": "f2"}, {"username": "f1", "password": "f2"}),
    ({"username": "f1"}, {"username": "f1"}),
    ({"password": "f2"}, {"password": "f2"}),
    ({}, None),                                 # nothing named: the page's own roles
    ({"username": "f1", "password": None}, {"username": "f1"})])
async def test_the_checks_mapping_names_the_plain_fields(rig, fields, sent):
    """R9: the verdict's fields name the username and the password (a plain field included);
    the broker gets exactly the named fields."""
    run = await phone(rig)
    await verdict(rig, run.run_id, await look(rig, run.run_id), {**SIGN_IN, "fields": fields})
    assert len(auto_logins(rig)) == 1 and auto_logins(rig)[0].get("fields") == sent


@pytest.mark.parametrize("fields", [
    {"username": "f9"},                          # a field the reading does not have
    {"password": "f1"},                          # a password into a non-password field
    {"username": "f2"},                          # a username into the password field
    {"username": "f1", "password": "f1"},        # one field, two roles
])
async def test_a_mapping_that_does_not_fit_the_reading_signs_nothing_in(rig, fields):
    run = await phone(rig)
    reply = await verdict(rig, run.run_id, await look(rig, run.run_id),
                          {**SIGN_IN, "fields": fields})
    assert auto_logins(rig) == [] and reply["outcome"] == "done"
    assert [e.outcome for e in rig.events(run.run_id, "sign_in_mapping")] == ["refused"]


async def test_a_marked_field_of_another_form_never_blocks_the_named_one(rig):
    """Terra (R9 post-fix): the login form's plain username beside a newsletter box the page
    marks as an email field: the check names the login form's fields, and the sign-in gets
    exactly those (the newsletter box is not named, so it is not filled)."""
    run = await phone(rig)
    rig.broker.sign_in_read = {"found": True, "passkey": False, "fields": [
        {"id": "f1", "role": None, "label": "Username", "password": False, "button": "Login"},
        {"id": "f2", "role": "password", "label": "Password", "password": True, "button": "Login"},
        {"id": "f3", "role": "username", "label": "Email", "password": False,
         "button": "Subscribe"}]}
    await verdict(rig, run.run_id, await look(rig, run.run_id),
                  {**SIGN_IN, "fields": {"username": "f1", "password": "f2"}})
    assert [a["fields"] for a in auto_logins(rig)] == [{"username": "f1", "password": "f2"}]


@pytest.mark.parametrize("fields", [["f1"], {"login": "f1"}, {"username": "x" * 17}])
async def test_a_mapping_outside_its_bounds_is_no_verdict(rig, fields):
    run = await phone(rig)
    await verdict(rig, run.run_id, await look(rig, run.run_id), {**SIGN_IN, "fields": fields})
    assert auto_logins(rig) == []
    assert [e.outcome for e in rig.events(run.run_id, "judgement")][-1] == "no_verdict"


@pytest.mark.parametrize("read_fields, fields", [
    # two plain text fields: the password named into one of them
    ([("f1", None, False), ("f2", None, False)], {"username": "f1", "password": "f2"}),
    # a field the page marks as a code field, named for the username
    ([("f1", "one_time_code", False), ("f2", "password", True)], {"username": "f1"}),
])
async def test_a_mapping_against_the_fields_facts_signs_nothing_in(rig, read_fields, fields):
    """A password only into a password field; a field the page marks keeps its role."""
    run = await phone(rig)
    rig.broker.sign_in_read = {"found": True, "passkey": False, "fields": [
        {"id": i, "role": role, "label": i, "password": pw, "button": "Login"}
        for i, role, pw in read_fields]}
    await verdict(rig, run.run_id, await look(rig, run.run_id), {**SIGN_IN, "fields": fields})
    assert auto_logins(rig) == []


# -- a start-page form with a plain box, and a page that changed under the verdict -------------

NEEDS_CHECK = {"status": "no_form", "reason": "needs_check"}


async def _start_page_needs_check(rig):
    _pages(rig)
    rig.browser.contents[START] = rig.browser.contents[LOGIN]
    rig.broker.login_reply = NEEDS_CHECK
    run = await rig.run()
    rig.broker.sign_in_read = READ
    rig.broker.login_reply = {"status": "logged_in", "reason": "completed"}
    return run


async def test_a_start_page_form_with_a_plain_box_goes_to_the_check(rig):
    """Run start: no step came between, and the look still runs. Not counted in the ledger;
    the verdict's sign-in is the run's one send."""
    run = await _start_page_needs_check(rig)
    assert rig.ctl.active.sign_ins == {}
    result = await look(rig, run.run_id)
    assert result.outcome == "judgement_required" and len(reads(rig)) == 1
    await verdict(rig, run.run_id, result)
    assert len(auto_logins(rig)) == 1
    assert auto_logins(rig)[0]["fields"] == {"username": "f1", "password": "f2"}
    assert rig.events(run.run_id, "login_needed") == []


async def test_needs_check_clears_an_older_look_at_the_same_page(rig):
    """The vault question's Yes reloads the same start page: an earlier sign-in had already
    put that address in ``looked``, which must not keep the look away."""
    run = await _start_page_needs_check(rig)
    active = rig.ctl.active
    site = rig.store.get_site(active.ref.site_id)
    page = rig.page()
    active.looked.add((active.session, page.url))
    rig.broker.login_reply = NEEDS_CHECK
    await rig.ctl._login(active, site, page.url)
    assert (active.session, page.url) not in active.looked
    rig.broker.login_reply = {"status": "logged_in", "reason": "completed"}
    assert (await look(rig, run.run_id)).outcome == "judgement_required"


async def test_a_page_that_changed_under_the_verdict_is_judged_again_once(rig):
    """A widget solved between the look and the verdict: the same address reads differently.
    Nothing filled on the old verdict; one successor judgement, and its verdict signs in."""
    run = await phone(rig)
    result = await look(rig, run.run_id)
    rig.page().text = "Login Page\nUsername\nPassword\nYou are verified"
    reply = await verdict(rig, run.run_id, result)
    assert reply["outcome"] == "judgement_required" and auto_logins(rig) == []
    assert len(reads(rig)) == 2
    await verdict(rig, run.run_id, reply)
    assert len(auto_logins(rig)) == 1


async def test_a_page_that_keeps_changing_is_judged_again_only_once(rig):
    run = await phone(rig)
    result = await look(rig, run.run_id)
    rig.page().text = "changed once"
    reply = await verdict(rig, run.run_id, result)
    rig.page().text = "changed twice"
    last = await verdict(rig, run.run_id, reply)
    assert last["outcome"] == "done" and auto_logins(rig) == [] and len(reads(rig)) == 2


async def test_another_address_under_the_verdict_is_not_judged_again(rig):
    run = await phone(rig)
    result = await look(rig, run.run_id)
    rig.page().show(FORGOT, "Forgot Password", (), "T")
    reply = await verdict(rig, run.run_id, result)
    assert reply["outcome"] == "done" and len(reads(rig)) == 1 and auto_logins(rig) == []
