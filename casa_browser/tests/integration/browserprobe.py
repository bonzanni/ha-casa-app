"""Runs one browser scenario as the controller user, in the app image (``run_scenario``).

The scenario drives the production ``BrowserManager`` (Patchright in this process, Chrome as
the browser user through the launcher) and prints one JSON line of results. Steps are test
instructions, never shipped behaviour; ``eval`` runs fixed test code in the page.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ONEPASSWORD_EXTENSION = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"
DEEP_TEXT = """() => { const out = []; const walk = (n) => { if (n.shadowRoot) walk(n.shadowRoot);
  for (const c of n.childNodes) { if (c.nodeType === 3) { const s = c.textContent.trim(); if (s) out.push(s); }
  else walk(c); } }; walk(document.body); return out.join(' | '); }"""
EXT_INFO = """(id) => new Promise(r => chrome.developerPrivate.getExtensionInfo(id, i => r(i ? {
  state: i.state, mustRemainInstalled: i.mustRemainInstalled, userMayModify: i.userMayModify,
  name: i.name} : null)))"""


async def _scenario(steps: list[dict]) -> list:
    sys.path.insert(0, "/opt")
    from casa_browser.browser import BrowserManager
    from casa_browser.pacing import ActionError, Pacer
    from casa_browser.protocol import EffectiveLocale

    locale = EffectiveLocale("en-GB", "Europe/Amsterdam", "option")
    mgr = BrowserManager(placeholder_dir=Path(os.environ["HOME"]) / "placeholder",
                         gpu=os.environ.get("CASA_IT_GPU") == "1")
    from types import SimpleNamespace
    from casa_browser.proxy_client import decode_policy
    run_ = SimpleNamespace(run_id="r-it", state="running")
    observer = None
    credential_driver = None
    credential_session = None
    credential_capture_id = None
    credential_token = None
    code_generation = None          # slice 6: the page a needs_code reply named

    def get_observer():  # Task 7's observer, imported only by the steps that use it
        nonlocal observer
        if observer is None:
            from casa_browser.handles import HandleRegistry
            from casa_browser.observe import Observer
            observer = Observer(HandleRegistry(), Path(os.environ["HOME"]) / "artifacts")
        return observer
    downloader = None

    def get_downloader():  # Task 10's downloader, with a real store holding the run
        nonlocal downloader, run_
        if downloader is None:
            from casa_browser.downloads import Downloader
            from casa_browser.protocol import Admission, RunSpec, SiteGrant
            from casa_browser.store import Store
            store = Store(Path(os.environ["HOME"]) / "state.db")
            spec = RunSpec("rq-it", "req-it", "account", "download", "invoice",
                           (SiteGrant("s1", "account"),), "",
                           Admission("operator_telegram", "m1", "sha256:" + "0" * 64))
            run_, _ = store.create_run(spec, "r-it")
            run_ = SimpleNamespace(run_id="r-it", state="running", spec=spec)
            downloader = Downloader(store, get_observer().registry, Path(os.environ["HOME"]) / "artifacts")
        return downloader

    def file_out(rec):
        import base64
        data = Path(rec.private_path).read_bytes()
        return {**rec.to_wire(), "head": base64.b64encode(data[:16]).decode(),
                "mode": oct(os.stat(rec.private_path).st_mode & 0o777)}
    out = []
    shot_clip = None
    try:
        for step in steps:
            op, arg = next(iter(step.items()))
            page = mgr.session.page if mgr.session else None
            if op == "credential":
                from casa_browser.login import DriverServer
                from casa_browser.broker_client import BrokerClient
                from casa_browser.proxy_client import ProxyClient
                client = BrokerClient(Path(arg['broker_socket']))
                proxy = ProxyClient()
                if credential_driver is None:
                    credential_driver = DriverServer(mgr, Path(arg['driver_socket']), allowed_uid=1102)
                    await credential_driver.start()
                    os.chmod(arg['driver_socket'], 0o666)
                action = arg['action']
                if action == 'guest_sign_in':
                    calls = []
                    real_open = proxy.open_window
                    async def opened():
                        token = await real_open()
                        calls.append('open_window')
                        return token
                    proxy.open_window = opened
                    mgr.session.context.on('page', lambda p: p.on('framenavigated', lambda f: calls.append('navigate')))
                    sid = await credential_driver.open_session('guest', 'r1', 's1', mgr.session, proxy, arg['login_origins'])
                    try:
                        reply = await credential_driver.dispatch(dict(op='guest_sign_in', driver_session=sid,
                            region_host=arg['region_host'], email=arg['email'], secret_key=arg['secret_key'], password=arg['password']))
                        out.append({'reply': reply, 'order': calls})
                    finally:
                        await credential_driver.close_session(sid)
                    continue
                if action == 'setup':
                    await client.begin_run('r1', ['s1'])
                    if arg.get('linked', True):
                        await client.register_item('s1', 'item', arg['login_origins'], 'operator')
                    else:   # slice 5: a site with no linked item (a live-view save creates one)
                        await client.register_capture('s1', arg['login_origins'], 'operator', 'Site')
                    out.append({'ready': True})
                elif action in ('open_login', 'login', 'begin_capture'):
                    kind = 'capture' if action == 'begin_capture' else 'login'
                    if credential_session is None:
                        reader = None
                        if kind == 'capture' and arg.get('policy') is not None:
                            # slice 5: the controller's reader, as the controller wires it
                            from casa_browser import gate
                            pol = decode_policy(arg['policy'])

                            async def reader(p, pol=pol):
                                return await gate.read_signin_page(gate.PageOps(), p, pol)
                        credential_session = await credential_driver.open_session(
                            kind, 'r1', 's1', mgr.session, proxy, arg['login_origins'],
                            reader=reader)
                    if action == 'open_login':
                        out.append({'opened': True})
                    elif action == 'login' and arg.get('auto'):
                        # Signing in off the start page: the look's read, then (``verdict``
                        # sign_in, scripted) the sign-in on the page as it is, each in its own
                        # login driver session as the controller opens them.
                        try:
                            if arg.get('read', True):
                                read = await client.read_sign_in('r1', 's1', credential_session)
                                out.append(read)
                                frames = credential_driver.sign_in_frames('r1', 's1')
                                out.append({'bound_frames': [f.url for f in frames]})
                        finally:
                            await credential_driver.close_session(credential_session)
                            credential_session = None
                        for code in arg.get('between', ()):
                            await mgr.session.page.evaluate(code, isolated_context=False)
                        if arg.get('settle_s'):
                            await asyncio.sleep(arg['settle_s'])
                        sid = await credential_driver.open_session(
                            'login', 'r1', 's1', mgr.session, proxy, arg['login_origins'])
                        try:
                            out.append(await client.login('r1', 's1', sid, auto=True,
                                                          password_sent=arg.get('password_sent', False),
                                                          fields=arg.get('fields')))
                        finally:
                            await credential_driver.close_session(sid)
                    elif action == 'login':
                        try:
                            reply = await client.login('r1', 's1', credential_session)
                            if reply.get('status') == 'needs_code':
                                code_generation = reply.get('page_generation')
                            out.append(reply)
                        finally:
                            await credential_driver.close_session(credential_session)
                            credential_session = None
                    else:
                        from casa_browser.policy import Lease
                        credential_token = await proxy.begin_manual(Lease('manual', 'r1', 's1', mgr.session.generation, 'manual'), purpose='login')
                        capture_reply = await client.begin_capture('r1', 's1', credential_session, 'login')
                        credential_capture_id = capture_reply['capture_id']
                        out.append(capture_reply)
                elif action == 'fill_code':   # slice 6 (S6-3): a fresh login driver session
                    sid = await credential_driver.open_session(
                        'login', 'r1', 's1', mgr.session, proxy, arg['login_origins'])
                    try:
                        generation = (code_generation if arg.get('page_generation') is None
                                      else arg['page_generation'])
                        out.append(await client.fill_code('r1', 's1', sid, generation, arg['code']))
                    finally:
                        await credential_driver.close_session(sid)
                    out.append({'open_driver_sessions': len(credential_driver.open_sessions())})
                elif action == 'review':
                    # The image broker's test window (``brokerfakes.serve_image_broker``): what
                    # the capture took, as roles and hints; no broker op shows it.
                    out.append(await client.call('test_review_capture', run_id='r1', site_id='s1',
                                                 capture_id=credential_capture_id))
                    await proxy.end_manual('manual')
                    await proxy.close_window(credential_token)
                elif action == 'take_attempt':      # slice 5: Done
                    taken = await client.take_attempt('r1', 's1', credential_capture_id)
                    out.append({'taken': taken,
                                'readings': credential_driver.take_readings(credential_capture_id)})
                    credential_session = None        # retired by the driver's take
                    await proxy.end_manual('manual')
                    await proxy.close_window(credential_token)
                elif action in ('save_attempt', 'drop_attempt'):   # a scripted signin verdict
                    args = ('r1', 's1', credential_capture_id)
                    out.append(await (client.save_attempt(*args, 'Site') if action == 'save_attempt'
                                      else client.drop_attempt(*args)))
                elif action == 'forced_close_action':
                    out.append(await forced_close_action(
                        credential_driver, credential_session, mgr, proxy, arg, locale))
                    credential_session = None
                elif action == 'attempt_bound_action':
                    from casa_browser.browser import BrowserSession
                    original = mgr.session.page
                    survivor = await mgr.session.context.new_page()
                    await survivor.goto(arg['same_origin_page'], wait_until='domcontentloaded')
                    found = await credential_driver.dispatch({'op': 'find_login_fields',
                        'driver_session': credential_session, 'page_generation': mgr.session.view_generation})
                    assert found['origin_ok'] and found['roles']['password'] and found['submit']
                    sid = credential_session
                    invalidation = arg['invalidate']
                    if invalidation == 'adopted':
                        async with mgr.session.context.expect_page() as popup:
                            await original.evaluate('''() => {
                                const popup=window.open('about:blank');
                                popup.document.body.appendChild(popup.document.adoptNode(document.querySelector('form')));
                            }''', isolated_context=False)
                        survivor = await popup.value
                    elif invalidation == 'detached':
                        await original.evaluate("() => document.querySelector('form').remove()",
                                                isolated_context=False)
                    elif invalidation == 'type_button':
                        await original.evaluate("() => document.querySelector('button').type='button'",
                                                isolated_context=False)
                    elif invalidation == 'other_form':
                        await original.evaluate('''() => {
                            const other=document.createElement('form'); other.id='other';
                            document.body.appendChild(other);
                            document.querySelector('button').setAttribute('form','other');
                        }''', isolated_context=False)
                    elif invalidation == 'closed':
                        await original.close()
                    elif invalidation == 'replaced':
                        await original.close()
                        replacement = await mgr.session.context.new_page()
                        await replacement.goto(arg['same_origin_page'], wait_until='domcontentloaded')
                        await survivor.close()
                        survivor = replacement
                    elif invalidation == 'wrong_session':
                        sid = 'wrong'
                    elif invalidation == 'closed_session':
                        await credential_driver.close_session(sid)
                    elif invalidation == 'generation':
                        mgr.session.generation += 1
                    elif invalidation == 'manager_session':
                        old = mgr.session
                        mgr.session = BrowserSession(old.kind, old.site_id, old.context,
                                                     old.generation, old.view_generation)
                    else:
                        raise ValueError('unknown binding invalidation')
                    request = dict(op=arg['op'], driver_session=sid, page_generation=found['page_generation'])
                    if arg['op'] == 'fill':
                        request.update(field_handle=found['roles']['password'], value=arg['value'])
                    else:
                        request.update(submit_handle=found['submit'])
                    reply = await credential_driver.dispatch(request)
                    effects = []
                    for live_page in (original, survivor):
                        if not live_page.is_closed():
                            effects.append(await live_page.evaluate(
                                "() => ({value: document.querySelector('#field')?.value || '', clicks: window.clicks || 0})",
                                isolated_context=False))
                    out.append({'reply': reply, 'effects': effects})
                elif action == 'find_login_fields':
                    out.append(await credential_driver.dispatch({'op': 'find_login_fields',
                        'driver_session': credential_session, 'page_generation': mgr.session.view_generation}))
                elif action == 'attempt_fill':
                    found = await credential_driver.dispatch({'op': 'find_login_fields',
                        'driver_session': credential_session, 'page_generation': mgr.session.view_generation})
                    if arg.get('navigate_frame'):
                        frame = mgr.session.page.frames[-1]
                        await frame.goto(arg['navigate_frame'])
                    reply = await credential_driver.dispatch({'op': 'fill', 'driver_session': credential_session,
                        'page_generation': found['page_generation'], 'field_handle': found['roles']['password'],
                        'value': arg['value']})
                    out.append(reply)
                elif action == 'driver':
                    request = dict(arg['request'], driver_session=credential_session)
                    out.append(await credential_driver.dispatch(request))
                else:
                    raise ValueError('unknown credential instruction')
            elif op == "reading_pair":
                # The reading a sign_in verdict is held to, at the look, after ``wait`` seconds
                # and once more, compared as the verdict's final read compares them.
                from casa_browser import gate
                page = mgr.session.page
                pair_policy = decode_policy(arg['policy'])
                async def bound():
                    _whole, reading = await gate.PageOps().read_page_and_bound(
                        page, (), policy=pair_policy)
                    return dict(reading.data)
                first = await bound()
                await asyncio.sleep(arg['wait'])
                second = await bound()
                third = await bound()
                out.append({'after_solve': gate.same_reading(first, second),
                            'after_that': gate.same_reading(second, third)})
            elif op == "chrome_alive":
                out.append({'pages': [p.url for p in mgr.session.context.pages],
                            'generation': mgr.session.generation})
            elif op == "open":
                s = await mgr.open(arg.get("site_id"), arg.get("mode", "account"), locale)
                out.append({"generation": s.generation, "kind": s.kind})
            elif op == "new_page":
                created = await mgr.session.context.new_page()
                await created.goto(arg, wait_until='domcontentloaded')
                out.append('new_page')
            elif op == "close_page":
                await page.close()
                out.append('closed_page')
            elif op == "close":
                await mgr.close()
                out.append("closed")
            elif op == "forget":
                await mgr.forget(arg)
                out.append("forgotten")
            elif op == "goto":
                try:
                    r = await page.goto(arg, wait_until="domcontentloaded", timeout=30000)
                    out.append(r.status if r else None)
                except Exception as e:
                    out.append(f"error:{type(e).__name__}")
            elif op == "frame_value":
                frame = next(f for f in page.frames if f.url.startswith(arg['origin'] + '/'))
                out.append(await frame.locator(arg['selector']).input_value())
            elif op == "text":
                out.append(await page.evaluate(DEEP_TEXT, isolated_context=False))
            elif op == "login_js":
                # Slice 5v: one of the driver's fixed scripts (login.py), in Patchright's isolated
                # world as the driver runs it, against this page's measured-shape DOM.
                from casa_browser import login as login_module
                script = getattr(login_module, arg["name"])
                target = page.main_frame
                if arg.get("frame"):
                    target = next(f for f in page.frames if f.url.endswith(arg["frame"]))
                if arg.get("selector"):
                    element = await target.query_selector(arg["selector"])
                    out.append(await element.evaluate(script, arg.get("arg")))
                elif arg.get("handle"):
                    found = (await target.evaluate_handle(script, arg.get("arg"))).as_element()
                    out.append(None if found is None else await found.evaluate("e => e.textContent.trim()"))
                else:
                    out.append(await target.evaluate(script, arg.get("arg")))
            elif op == "eval":
                try:
                    out.append(await page.evaluate(arg, isolated_context=False))
                except Exception as e:
                    out.append(f"error:{type(e).__name__}")
            elif op == "wait":
                await asyncio.sleep(arg)
                out.append("waited")
            elif op == "extension":
                await page.goto("chrome://extensions")
                info = None
                deadline = time.monotonic() + arg
                while time.monotonic() < deadline:
                    info = await page.evaluate(EXT_INFO, ONEPASSWORD_EXTENSION, isolated_context=False)
                    if info:
                        break
                    await asyncio.sleep(3)
                out.append(info)
            elif op == "cookies":
                out.append(sorted(c["name"] for c in await mgr.session.context.cookies(arg)))
            elif op == "click_hidden_duplicate":
                started = time.monotonic()
                try:
                    await Pacer(page).click(page.locator("button", has_text=arg).nth(1))
                    code = "clicked"
                except ActionError as e:
                    code = e.code
                out.append({"code": code, "seconds": round(time.monotonic() - started, 2)})
            elif op == "observe":
                from casa_browser.observe import ObservationRefused
                try:
                    obs = await get_observer().observe(run_, mgr.session, decode_policy(arg))
                    out.append(obs.to_wire())
                except ObservationRefused as e:
                    out.append(f"refused:{e.code}")
            elif op == "gate_read":            # slice 5j: the gate's own reading (the check's facts)
                from casa_browser import gate
                out.append((await gate.read(page.main_frame)).data)
            elif op == "frame_urls":           # slice 5m: the addresses Playwright reports
                out.append([f.url for f in page.frames])
            elif op == "gate_read_page":       # slice 5m: the check's whole-page reading
                from casa_browser import gate
                out.append((await gate.PageOps().read_page(page, None, policy=decode_policy(arg))).data)
            elif op == "screenshot":
                import base64
                observer = get_observer()
                sid = await observer.capture_operator(run_, mgr.session, decode_policy(arg))
                path = Path(os.environ["HOME"]) / "artifacts" / "screenshots" / f"{sid}.png"
                # the capture's trim: "box" answers in the image's own coordinates
                shot_clip = {"x": observer.crop_origin[0], "y": observer.crop_origin[1]}
                out.append({"id": sid, "png": base64.b64encode(path.read_bytes()).decode() if sid else None,
                            "files": sorted(x.name for x in path.parent.iterdir()) if sid else [],
                            "clip": shot_clip})
            elif op == "question_shot":
                import base64
                observer = get_observer()
                target = await page.query_selector(arg["target"])
                sid = await observer.capture_question(run_, mgr.session,
                                                      decode_policy(arg["policy"]), target)
                path = Path(os.environ["HOME"]) / "artifacts" / "screenshots" / f"{sid}.png"
                shot_clip = {"x": observer.crop_origin[0], "y": observer.crop_origin[1]}
                out.append({"id": sid, "png": base64.b64encode(path.read_bytes()).decode()
                            if sid else None, "clip": shot_clip})
            elif op == "box":
                out.append(await page.evaluate(
                    "(s) => { let n = document; for (const part of s.split('|')) {"
                    " n = (n.shadowRoot || n).querySelector(part); }"
                    " const r = n.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; }",
                    arg, isolated_context=False))
                if shot_clip is not None and isinstance(out[-1], list):
                    out[-1] = [out[-1][0] - shot_clip["x"], out[-1][1] - shot_clip["y"],
                               *out[-1][2:]]
            elif op == "policy_rows":
                await page.goto("chrome://policy")
                await asyncio.sleep(2)
                out.append(await page.evaluate("""() => { const out = []; const walk = (n) => {
                    if (n.shadowRoot) walk(n.shadowRoot); for (const c of n.children || []) walk(c);
                    if (n.classList && n.classList.contains('row')) {
                      const t = n.innerText.replace(/\\s+/g, ' ').trim();
                      if (t.includes(' Platform ')) out.push(t); } };
                    walk(document.body); return out; }""", isolated_context=False))
            elif op == "capture_download":
                from casa_browser.downloads import DownloadRefused
                dl = get_downloader()
                async with page.expect_download(timeout=30000) as info:
                    await page.click(arg["click"], timeout=5000)
                download = await info.value
                landed = await download.path()
                st = os.stat(landed)
                seen = {"landed_mode": oct(st.st_mode & 0o777), "landed_uid": st.st_uid,
                        "landed_gid": st.st_gid, "landed_dir": str(Path(landed).parent)}
                try:
                    rec = await dl.capture_browser_download(run_, download, mgr.session,
                                                            decode_policy(arg["policy"]))
                    seen["file"] = file_out(rec)
                except DownloadRefused as e:
                    seen["refused"] = e.code
                seen["left_in_downloads"] = sorted(p.name for p in Path(landed).parent.iterdir())
                out.append(seen)
            elif op == "fetch_link":
                from casa_browser.downloads import DownloadRefused
                pol = decode_policy(arg["policy"])
                obs = await get_observer().observe(run_, mgr.session, pol)
                handle = next(e.handle for e in obs.elements if e.label_masked == arg["label"])
                try:
                    rec = await get_downloader().fetch(run_, handle, mgr.session, pol)
                    out.append({"file": file_out(rec), "pages": len(mgr.session.context.pages)})
                except DownloadRefused as e:
                    out.append({"refused": e.code})
            elif op == "click":
                await page.click(arg, timeout=5000)
                out.append("clicked")
            elif op == "type":                 # real keystrokes into a field
                await page.type(arg["selector"], arg["text"], delay=5)
                out.append("typed")
            elif op == "press":                # one real key press in a field
                await page.press(arg["selector"], arg["key"])
                out.append("pressed")
            else:
                out.append(f"unknown:{op}")
    finally:
        if credential_driver is not None:
            if credential_session is not None:
                await credential_driver.close_session(credential_session)
            await credential_driver.close()
        await mgr.shutdown()
    return out



async def forced_close_action(driver, sid, manager, proxy, arg, locale):
    """Queue a real element evaluation behind ordinary main-world page JavaScript."""
    from casa_browser import login
    from casa_browser.launcher import process_group_running
    from casa_browser.proxy_client import decode_policy

    browser = manager.session
    page = browser.page
    found = await driver.find_login_fields(sid, browser.view_generation)
    assert found['roles']['password'] and found['submit']
    # Clear only before the attempted action: reopening must preserve any late marker.
    await page.evaluate("() => localStorage.removeItem('late-effect')", isolated_context=False)
    handle = found['roles']['password'] if arg['op'] == 'fill' else found['submit']
    element = driver._sessions[sid].handles[handle][0]
    evaluate = element.evaluate
    queued = asyncio.Event()
    stalls = []
    proxy = driver._sessions[sid].proxy
    busy_seconds = 2
    cdp = await browser.context.new_cdp_session(page)
    order = []
    pgids = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / 'cmdline').read_bytes().split(b'\0')
            if cmd[0] == b'/opt/google/chrome/chrome' and b'--user-data-dir=/data/profiles/s1' in cmd:
                pgids.append(int((entry / 'stat').read_text().rsplit(')', 1)[1].split()[2]))
        except FileNotFoundError:
            pass
    assert len(set(pgids)) == 1
    pgid = pgids[0]
    assert process_group_running(pgid)

    async def blocked_evaluate(expression, value=None):
        if expression in (login._SET, login._SUBMIT):
            stall = asyncio.create_task(cdp.send('Runtime.evaluate', {
                'expression': '{const end=Date.now()+' + str(busy_seconds * 1000) +
                              ';while(Date.now()<end){}}'}))
            stalls.append(stall)
            # Let the ordinary page evaluation enter its busy loop, without awaiting its end.
            await asyncio.sleep(0.1)
            queued.set()
        return await evaluate(expression, value)

    revoke = proxy.revoke

    async def recorded_revoke(run_id):
        running = process_group_running(pgid)
        order.extend(['chrome_running' if running else 'browser_exited', 'revoke'])
        await revoke(run_id)
        if running:
            # A reversed-order mutation must let the queued action show its durable effect.
            # Chrome batches localStorage commits; allow a late marker to reach disk.
            await asyncio.sleep(busy_seconds + 6)

    proxy.revoke = recorded_revoke
    driver.cleanup_timeout = 0.05
    request = dict(op=arg['op'], driver_session=sid, page_generation=found['page_generation'])
    if arg['op'] == 'fill':
        request.update(field_handle=handle, value=arg['value'])
    else:
        request.update(submit_handle=handle)
    if arg.get('stall', True):
        element.evaluate = blocked_evaluate
        action = asyncio.create_task(driver.dispatch(request))
        await asyncio.wait_for(queued.wait(), 5)
        await driver.close_session(sid)
        # A killed CDP target may leave the test's raw protocol waiter unresolved.
        for stall in stalls:
            stall.cancel()
        result = (await asyncio.gather(action, *stalls, return_exceptions=True))[0]
        assert isinstance(result, (dict, asyncio.CancelledError))
    else:
        assert await driver.dispatch(request) == {'ok': True}
        await page.evaluate('() => window.effectDone', isolated_context=False)
        await driver.close_session(sid)
        await manager.close()
    assert not browser.login_active
    assert not process_group_running(pgid)
    await proxy.install(decode_policy(arg['policy']))
    reopened = await manager.open('s1', 'account', locale)
    await reopened.page.goto(arg['url'], wait_until='domcontentloaded')
    marker = await reopened.page.evaluate(
        "() => localStorage.getItem('late-effect')", isolated_context=False)
    return dict(marker=marker, order=order)

def run_scenario(steps: list[dict], timeout: int = 300, gpu: bool = False, broker_fixture=None) -> list:
    """Run ``steps`` as casa-controller; returns the result list (raises with stderr on failure)."""
    home = Path("/tmp/casa-it-controller")
    home.mkdir(exist_ok=True)
    os.chown(home, 1103, 1103)
    broker = None
    if broker_fixture is not None:
        broker = _start_test_broker(broker_fixture)
    try:
        return _run_controller(steps, timeout, gpu, home)
    finally:
        if broker is not None:
            broker.terminate()
            try:
                broker.wait(10)
            except subprocess.TimeoutExpired:
                broker.kill()
                broker.wait()


def _run_controller(steps, timeout, gpu, home):
    r = subprocess.run(
        ["setpriv", "--reuid=casa-controller", "--regid=casa-controller", "--clear-groups",
         "/opt/venv/bin/python", __file__],
        input=json.dumps(steps), capture_output=True, text=True, timeout=timeout,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt:/opt/casa_browser_tests/tests", "HOME": str(home),
             "CASA_IT_GPU": "1" if gpu else "0"})
    lines = [l for l in r.stdout.splitlines() if l.startswith("RESULT ")]
    if r.returncode != 0 or not lines:
        raise RuntimeError(f"scenario failed ({r.returncode}): {r.stderr[-3000:]}")
    return json.loads(lines[-1][len("RESULT "):])




def _start_test_broker(config):
    """Only the image test harness can start this fake-SDK broker process."""
    import selectors
    proc = subprocess.Popen(
        ['setpriv', '--reuid=casa-broker', '--regid=casa-broker', '--clear-groups',
         '/opt/venv/bin/python', '/opt/casa_browser_tests/tests/brokerfakes.py'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': '/opt:/opt/casa_browser_tests/tests',
             'HOME': config['root']})
    proc.stdin.write(json.dumps(config) + '\n')
    proc.stdin.flush()
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        ready = selector.select(15)
    if not ready or proc.stdout.readline().strip() != 'READY':
        proc.terminate()
        _, err = proc.communicate(timeout=5)
        raise RuntimeError('test broker failed: ' + err[-2000:])
    return proc

if __name__ == "__main__":
    result = asyncio.run(_scenario(json.loads(sys.stdin.read())))
    print("RESULT " + json.dumps(result), flush=True)
