"""With no 1Password token, nothing made for 1Password runs.

One scenario over the app's own pieces, wired as a start wires them: root bootstrap on an
options file, the broker built from the file bootstrap wrote for it and served on a socket,
the controller with a client of that broker, the pages and the ``/v1`` API over that
controller, and the proxy's rules built from the file bootstrap wrote for the proxy. Doubles
stand only where a browser or a network would be: the page a sign-in reads and fills, the
browser manager, the proxy's control client, and 1Password's SDK, whose sign-in is counted.

The options file has an empty token and all four guest options filled, the way an install
looks whose 1Password token was taken out. The rule is asserted by count: how many guest
sessions, SDK clients, admitted hosts, policy keys, guest values and texts naming 1Password
this start produced. Every one of them is zero.
"""
from __future__ import annotations

import json
import os
import re
from html import unescape
from types import SimpleNamespace

import onepassword
import pytest
from mitmproxy import http
from mitmproxy.test import tflow, tutils

import proxyharness as h
from apihelpers import headers
from brokerfakes import FakeDriver, Step, canary, sdk_error
from controllerfakes import Rig
from uihelpers import Operator

from casa_browser import proxy_main
from casa_browser.api import make_app
from casa_browser.broker.journal import Journal
from casa_browser.broker.main import Broker, BrokerServer, build_vault
from casa_browser.broker_client import BrokerClient
from casa_browser.i18n import catalog
from casa_browser.options import BrokerOptions, Owners, Paths, bootstrap, read_locale_record
from casa_browser.protocol import spec_digest
from casa_browser.ui import make_ui

TEAM_HOST = "casa-it.1password.eu"
SITE, SIGN_IN = "https://shop.example", "https://login.shop.example"
# What no text of an install without 1Password may say.
WORDS = ("1password", "extension", "guest")
# Chrome's own check for an update of the 1Password extension its policy forces, as measured.
EXTENSION_UPDATE = (
    "clients2.google.com",
    "/service/update2/crx?os=linux&arch=x86_64&prod=chromecrx&prodchannel="
    "&prodversion=155.0.8059.84&lang=en-GB&acceptformat=crx3,puff"
    "&x=id%3Daeblfdkhhhdcdjpifhhbdiojplfjncoa%26installsource%3Dpolicy%26uc")
CHROME_OWN = {"sec-fetch-site": "none", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "empty"}


def started(tmp_path, options: dict) -> Paths:
    """Root bootstrap on *options*, on paths of this test."""
    paths = Paths(data=tmp_path / "data", run=tmp_path / "run", container_env=tmp_path / "s6env",
                  chrome_policy=tmp_path / "etc" / "casa.json")
    paths.data.mkdir()
    paths.container_env.mkdir()
    paths.options.write_text(json.dumps(options))
    bootstrap({"TZ": "UTC"}, paths, Owners(), log=lambda _line: None)
    return paths


def proxy_rules(hosts_file):
    """The proxy's addon as its own start code builds it from the file bootstrap wrote."""
    async def resolver(host, port):
        return [h.FIXTURE_IP]
    return proxy_main.build_addon(resolver=resolver, hosts_file=hosts_file,
                                  binding_key_path=hosts_file.with_name("binding.key"))


async def extension_updates_admitted(addon) -> int:
    """How many of Chrome's update checks for the extension the addon lets through (of one)."""
    host, path = EXTENSION_UPDATE
    flow = tflow.tflow(req=tutils.treq(
        method=b"GET", scheme=b"https", host=host, port=443, path=path.encode(), content=b"",
        authority=b"", headers=http.Headers(host=host, **CHROME_OWN)))
    flow.client_conn.sockname = ("127.0.0.1", addon.browser_port)
    flow.client_conn.sni = host
    addon.install(h.account_policy())
    addon.requestheaders(flow)
    await addon.request(flow)
    return len([event for event in addon.events if event.allow])


def shown(page: str) -> str:
    """A page as a person reads it; a form's nonce is random letters, not text of the page."""
    return unescape(re.sub(r'name="nonce" value="[^"]*"', "", page))


@pytest.fixture
async def app(tmp_path, monkeypatch, aiohttp_client):
    """The app after a start with no 1Password token and every guest option filled."""
    made = SimpleNamespace(sdk_clients=[], broker_replies=[])

    async def authenticate(**given):
        made.sdk_clients.append(sorted(given))
        raise sdk_error("Internal")
    monkeypatch.setattr(onepassword.Client, "authenticate", authenticate)

    made.guest = {"guest_sign_in_address": TEAM_HOST, "guest_email": canary(),
                  "guest_password": canary(), "guest_secret_key": canary()}
    paths = made.paths = started(tmp_path, {
        "onepassword_service_account_token": "", **made.guest,
        "language": "en-GB", "timezone": "Europe/Amsterdam"})
    public = json.loads(paths.public_options.read_text())

    options = BrokerOptions(**json.loads(paths.broker_config.read_text()))
    made.vault = build_vault(options, tmp_path / "vault")
    # A page with a sign-in form that also offers a passkey, then the page after it.
    made.driver = FakeDriver([Step(("username", "password"), passkey_hint=True), Step()])
    journal = Journal(paths.broker_state / "journal.db")
    broker = Broker(made.vault, made.driver, journal, paths.broker_state / "registry.json",
                    options)
    server = BrokerServer(broker, tmp_path / "rpc.sock", allowed_uid=os.getuid())
    await broker.start()
    await server.start()

    client = BrokerClient(server.path)
    answer = client.call

    async def recorded(op, **args):
        reply = await answer(op, **args)
        made.broker_replies.append((op, reply))
        return reply
    client.call = recorded

    rig = made.rig = Rig(tmp_path, sites=(), configured=public["configured"],
                         login_store=public["login_store"], locale=read_locale_record(paths),
                         broker=client)
    await rig.start()
    made.operator = Operator(await aiohttp_client(make_ui(rig.ctl)))
    made.api = await aiohttp_client(make_app(rig.ctl))
    yield made
    await rig.live.end_all()
    await rig.ctl.shutdown()
    rig.close()
    await server.close()
    journal.close()


async def test_nothing_made_for_1password_runs_in_built_in_mode(app, tmp_path):
    rig, operator, api, paths = app.rig, app.operator, app.api, app.paths
    texts = []          # (where it was read, the text): every text the scenario produced

    # What bootstrap left for the proxy and for Chrome, and what the proxy makes of it. The
    # same request on rules built for 1Password is let through: the control.
    hosts_file = paths.proxy_private / "hosts.json"
    addon = proxy_rules(hosts_file)
    updates_admitted = await extension_updates_admitted(addon)
    with_token = tmp_path / "with-token.json"
    with_token.write_text(json.dumps({"onepassword_account_host": TEAM_HOST,
                                      "login_store": "onepassword"}))
    assert await extension_updates_admitted(proxy_rules(with_token)) == 1
    policy = json.loads(paths.chrome_policy.read_text())
    guest_values_written = [(name, f.name) for f in tmp_path.rglob("*")
                            if f.is_file() and f not in (paths.options, with_token)
                            for name, value in app.guest.items()
                            if value.encode() in f.read_bytes()]

    username, password = canary(), canary()
    sign_ins = []

    async def page(path, where):
        status, html, _ = await operator.get(path)
        assert status == 200, (where, status)
        texts.append((where, shown(html)))
        return html

    async def asked(method, path, where, **kw):
        reply = await getattr(api, method)(path, headers=secret, **kw)
        text = await reply.text()
        texts.append((where, text))
        return reply.status, json.loads(text)

    try:
        # The Setup page, a login in the store, and a site registered with it on the Sites form.
        setup = await page("/setup", "the Setup page")
        login = await app.vault.create_login("Shop", username, password, [SIGN_IN])
        await page("/sites/new", "the form for a new site")
        status, _, reply = await operator.post("/sites", {
            "display_name": "Shop", "origins": SITE, "login_origins": SIGN_IN,
            "vault_item_id": login.id, "start_page": SITE + "/account", "confirm": "yes"})
        assert status == 303, status
        status, listing, _ = await operator.follow(reply)
        assert status == 200
        texts.append(("the Sites page", shown(listing)))
        site = next(s for s in rig.store.list_sites() if s.display_name == "Shop")
        await page(f"/sites/{site.site_id}", "the site's page")

        # An account run on that site: its start page signs in on its own.
        rig.ctl.pairing.arm()
        secret = headers(rig.ctl.pairing.claim("Casa", "peer"))
        members = {"request_id": "rule-1", "requester_id": "finance", "mode": "account",
                   "task_text": "get the invoice", "task_kind": "invoice",
                   "sites": [{"site_id": site.site_id, "access": "account"}],
                   "result_expectation": ""}
        status, run = await asked("post", "/v1/runs", "the run's start", json={
            **members, "admission": {"origin_kind": "operator_telegram", "origin_ref": "m1",
                                     "admitted_spec_digest": spec_digest(members)}})
        assert status == 201, run
        run_id = run["run_id"]
        for _ in range(100):
            await rig.settle()
            if rig.state(run_id) == "running":
                break
        assert rig.state(run_id) == "running"
        sign_ins = [reply for op, reply in app.broker_replies if op == "login"]

        # A login live view on it, then what the caller and the operator read of the run.
        status, live = await asked("post", f"/v1/runs/{run_id}/live", "the live view's request",
                                   json={"purpose": "login"})
        assert status == 201, live
        status, hello = await asked("get", "/v1/hello", "the hello")
        assert hello["configured"] is True, hello     # set up, with no guest option in use
        await asked("get", f"/v1/runs/{run_id}", "the run")
        status, events = await asked("get", f"/v1/runs/{run_id}/events?after=0", "the events")
        assert "live_requested" in [event["kind"] for event in events["events"]]
        status, records = await asked(
            "get", "/v1/records?requester_id=finance&since=2000-01-01T00:00:00Z", "the records")
        assert status == 200, records
        await asked("get", "/v1/notices?after=0", "the notices")
        await page("/", "the home page")
        await page(f"/runs/{run_id}", "the run's page")
    finally:
        # The counts are compared whatever became of the scenario: a piece made for 1Password
        # that ran is reported as that, also when it stopped the scenario on its way.
        texts += [(f"the broker's reply to {op}", json.dumps(reply))
                  for op, reply in app.broker_replies]
        naming = [where for where, text in texts if any(word in text.lower() for word in WORDS)]
        assert len([e for e in rig.log if e[:2] == ("open_session", "guest")]) == 0
        assert len(app.sdk_clients) == 0
        assert len(addon.broker_hosts | addon.extension_hosts) == 0 and updates_admitted == 0
        assert len({"ExtensionSettings", "DeveloperToolsAvailability"} & set(policy)) == 0
        assert len(guest_values_written) == 0, guest_values_written
        assert len(naming) == 0, naming

    # The scenario was the built-in one from its first page, and it did sign in: the form with
    # the passkey beside it was filled with the stored login's two values and sent.
    assert catalog("en")["setup.store_builtin"] in shown(setup)
    assert sign_ins == [{"status": "logged_in", "reason": "completed"}]
    assert app.driver.fills == [(0, "username", username), (0, "password", password)]
    # thirteen pages and API replies were read, and every reply of the broker
    assert len(texts) == 13 + len(app.broker_replies) and app.broker_replies
