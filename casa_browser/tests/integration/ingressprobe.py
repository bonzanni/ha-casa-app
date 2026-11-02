"""Task 13 in the image: a run controller with production components serving the ingress UI on
the production socket (the production controller service is stopped meanwhile), driven over
stdin/stdout by ``test_ingress.py``. Runs as casa-controller with its own groups
(``casa-vncview`` reaches the VNC socket) and the controller's X cookie. Test instructions,
never shipped behaviour.

Protocol: one JSON command per stdin line, one JSON reply per stdout line; the first line out
is ``{"ready": run_id}`` once an open-web run waits in ``waiting_live`` (purpose assist) with
Chrome on the fixture's login page."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

FIXTURE_PAGE = "https://site-a.example/login"


async def main() -> None:
    sys.path.insert(0, "/opt")
    from casa_browser import ui
    from casa_browser.broker_client import BrokerClient
    from casa_browser.browser import BrowserManager
    from casa_browser.controller import Components, RunController
    from casa_browser.downloads import Downloader
    from casa_browser.handles import HandleRegistry
    from casa_browser.login import DriverServer
    from casa_browser.observe import Observer
    from casa_browser.pacing import Pacer
    from casa_browser.protocol import Admission, EffectiveLocale, RunSpec, spec_digest
    from casa_browser.proxy_client import ProxyClient
    from casa_browser.store import Store

    home = Path(os.environ["HOME"])
    store = Store(home / "state.db")
    registry = HandleRegistry()
    browser = BrowserManager(placeholder_dir=home / "placeholder", gpu=False)
    ctl = RunController(Components(
        store=store, browser=browser, proxy=ProxyClient(),
        broker=BrokerClient(home / "none.sock", store=store),
        driver=DriverServer(browser, home / "driver.sock", allowed_uid=1102),
        observer=Observer(registry, home / "artifacts", store), registry=registry,
        downloader=Downloader(store, registry, home / "artifacts"),
        pacer=Pacer,
        locale=EffectiveLocale("en-GB", "Europe/Amsterdam", "option"), configured=False))
    await ctl.start()
    runner = await ui.serve(ui.make_ui(ctl))
    members = {"request_id": "it-live", "requester_id": "finance", "mode": "open_web",
               "task_text": "look something up", "task_kind": "lookup", "sites": [],
               "result_expectation": ""}
    spec = RunSpec("it-live", "finance", "open_web", "look something up", "lookup", (), "",
                   Admission("operator_telegram", "m1", spec_digest(members)))
    run, _ = await ctl.start_run(spec)
    for _ in range(600):
        if store.get_run(run.run_id).state == "running":
            break
        await asyncio.sleep(0.1)
    page = browser.session.page
    await page.goto(FIXTURE_PAGE, wait_until="domcontentloaded", timeout=30_000)
    await ctl.request_live(run.run_id, "assist")
    print(json.dumps({"ready": run.run_id}), flush=True)

    reader = asyncio.StreamReader()
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    from casa_browser.liveview import READ_BACK
    while True:
        line = await reader.readline()
        if not line:
            break
        cmd = json.loads(line)
        op = cmd["op"]
        if op == "state":
            out = store.get_run(run.run_id).state
        elif op == "values":
            p = browser.session.page
            out = await p.evaluate("() => [...document.querySelectorAll('input')].map(i => i.value)")
        elif op == "url":
            out = browser.session.page.url
        elif op == "goto":
            await browser.session.page.goto(cmd["arg"], wait_until="load", timeout=30_000)
            out = browser.session.page.url
        elif op == "eval":       # the page's own script (its main world), as a site would run it
            out = await browser.session.page.evaluate(cmd["arg"], isolated_context=False)
        elif op == "active":
            p = browser.session.page
            out = await p.evaluate("() => (document.activeElement || {}).id || null")
        elif op == "window":
            p = browser.session.page
            cdp = await browser.session.context.new_cdp_session(p)
            window = (await cdp.send("Browser.getWindowForTarget"))["windowId"]
            bounds = (await cdp.send("Browser.getWindowBounds", {"windowId": window}))["bounds"]
            out = {"bounds": [bounds["left"], bounds["top"], bounds["width"], bounds["height"]],
                   "screen": await p.evaluate(READ_BACK)}
        elif op == "quit":
            print(json.dumps({"bye": True}), flush=True)
            break
        else:
            out = {"error": "unknown"}
        print(json.dumps({"out": out}), flush=True)
    await runner.cleanup()
    await ctl.shutdown()
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
