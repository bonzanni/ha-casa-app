"""Runs one run-controller scenario as the controller user, in the app image (``run_lifecycle``).

The production ``RunController`` with its production components — ``BrowserManager`` (Chrome
as the browser user through the launcher), ``ProxyClient`` on the production control socket
(the production proxy, or the test harness standing in for it), ``Observer``, ``Downloader``,
the login ``DriverServer`` — and, when a broker fixture is given, the
uid-separated test broker (fake SDK). The store lives under ``HOME`` and survives between
two invocations, so a scenario can kill the controller and start another. Steps are test
instructions, never shipped behaviour. Results: one ``RESULT`` JSON line; ``READY`` lines
mark a step the parent waits for.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path


# A frame's own address and every request it completed (a resource timing entry appears once
# the response is in), each as path and query: a fetch's request, or a form's navigation.
_ARRIVED = ("() => [location.pathname + location.search, ...performance.getEntriesByType('resource')"
            ".map(e => { const u = new URL(e.name); return u.pathname + u.search; })]")


async def _landed(page, path: str) -> bool:
    for frame in page.frames:
        try:
            if path in await asyncio.wait_for(frame.evaluate(_ARRIVED), 2):
                return True
        except Exception:       # a frame navigating or gone: the next poll reads it again
            continue
    return False


async def _scenario(steps: list[dict], broker: dict | None) -> list:
    sys.path.insert(0, "/opt")
    from casa_browser.broker_client import BrokerClient
    from casa_browser.browser import BrowserManager
    from casa_browser.controller import Components, RunController
    from casa_browser.downloads import Downloader
    from casa_browser.handles import HandleRegistry
    from casa_browser.login import DriverServer
    from casa_browser.observe import Observer
    from casa_browser.pacing import Pacer
    from casa_browser.protocol import (Action, Admission, EffectiveLocale,
                                       ProtocolError, ResultManifest, RunSpec, Site, SiteGrant,
                                       spec_digest)
    from casa_browser.proxy_client import ProxyClient
    from casa_browser.store import Store

    home = Path(os.environ["HOME"])
    # The log goes to a file the test scans for canaries: libraries at the production level
    # (the controller service logs warnings), the app's own modules at every level.
    import logging
    logging.basicConfig(filename=home / "controller.log", level=logging.WARNING,
                        format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("casa_browser").setLevel(logging.DEBUG)
    store = Store(home / "state.db")
    registry = HandleRegistry()
    browser = BrowserManager(placeholder_dir=home / "placeholder", gpu=False)
    driver_path = Path(broker["driver_socket"]) if broker else home / "driver.sock"
    driver = DriverServer(browser, driver_path, allowed_uid=1102)
    if broker and broker.get("debug"):
        # The live passkey pin's diagnostics (5v): a driver handler's unexpected error as its
        # type and code places only, on stderr.
        import traceback
        for name in ("guest_state", "guest_sign_in", "guest_unlock", "choose_passkey",
                     "find_login_fields", "settle"):
            original = getattr(driver, name)

            async def traced(*a, _original=original, **k):
                try:
                    return await _original(*a, **k)
                except Exception as error:
                    frames = traceback.extract_tb(error.__traceback__)
                    print("driver error", type(error).__name__,
                          [(f.name, f.lineno) for f in frames][-8:], file=sys.stderr, flush=True)
                    raise
            setattr(driver, name, traced)
    broker_client = BrokerClient(Path(broker["root"]) / "rpc.sock" if broker else home / "none.sock",
                                 store=store)
    ctl = RunController(Components(
        store=store, browser=browser, proxy=ProxyClient(), broker=broker_client, driver=driver,
        observer=Observer(registry, home / "artifacts", store), registry=registry,
        downloader=Downloader(store, registry, home / "artifacts"),
        pacer=Pacer,
        locale=EffectiveLocale("en-GB", "Europe/Amsterdam", "option"), configured=True))
    await ctl.start()
    if broker:
        os.chmod(driver_path, 0o666)
    runs: dict[str, str] = {}
    last: dict[str, object] = {}
    seen: list = []          # every model-facing reply (observations, action results) as sent
    out: list = []

    def run_id(name):
        return runs[name]

    def handle(name, label, nth=0):
        obs = last[name]
        if label == "__page__":
            return obs.page_handle, obs.generation
        return [e.handle for e in obs.elements if label in e.label_masked][nth], obs.generation

    gate_last: dict = {}        # run name -> the last reply that carried a judgement
    gate_question: dict = {}    # run name -> the last question id asked

    def track(name, reply):
        if isinstance(reply, dict):
            if "judgement" in reply:
                gate_last[name] = reply
            if reply.get("question_id"):
                gate_question[name] = reply["question_id"]

    async def settle(name, reply):
        """A checked step outside the gate's own tests: the plugin's relay with a scripted
        ``go`` (the tests of other behaviour run the step as before); a refusal before the
        dispatch reads as the error it names."""
        if not isinstance(reply, dict) or reply.get("outcome") != "judgement_required":
            return reply
        import hashlib
        from casa_browser.protocol import ERROR_CODES
        j = reply["judgement"]
        out_ = await attempt(ctl.apply_judgement(run_id(name), j["id"], {
            "packet_sha256": hashlib.sha256(j["packet"].encode("utf-8")).hexdigest(),
            "verdict": {"verdict": "go", "category": "reading", "reason": "scripted"}}))
        if isinstance(out_, dict) and out_.get("outcome") == "not_run" \
                and out_.get("reason") in ERROR_CODES:
            return {"error": out_["reason"]}
        return out_

    async def attempt(coro):
        try:
            return await coro
        except ProtocolError as err:
            return {"error": err.code}

    try:
        for step in steps:
            op, arg = next(iter(step.items()))
            if op == "site":
                store.put_site(Site(arg["site_id"], arg["name"], tuple(arg["origins"]),
                                    tuple(arg.get("login", ())), arg.get("item"), (),
                                    arg.get("start"), 1))
                out.append(None)
            elif op == "broker":
                reply = await broker_client.call(arg.pop("op"), **arg)
                out.append(reply)
            elif op == "run":
                grants = tuple(SiteGrant(s, a) for s, a in arg.get("sites", ()))
                requester = arg.get("requester", "it")
                members = {"request_id": arg["request_id"], "requester_id": requester,
                           "mode": arg.get("mode", "account"), "task_text": "synthetic",
                           "task_kind": "it", "sites": [g.to_wire() for g in grants],
                           "result_expectation": ""}
                if arg.get("start_url"):           # slice 5b: an open-web run's first page
                    members["start_url"] = arg["start_url"]
                spec = RunSpec(arg["request_id"], requester, members["mode"], "synthetic", "it",
                               grants, "", Admission("operator_telegram", "m",
                                                     spec_digest(members)),
                               start_url=arg.get("start_url"))
                run, created = await ctl.start_run(spec)
                runs[arg["name"]] = run.run_id
                out.append({"created": created})
            elif op == "wait_state":
                deadline = time.monotonic() + arg.get("timeout", 60)
                states = arg["state"] if isinstance(arg["state"], list) else [arg["state"]]
                while store.get_run(run_id(arg["name"])).state not in states:
                    if time.monotonic() > deadline:
                        raise RuntimeError("state " + store.get_run(run_id(arg["name"])).state)
                    await asyncio.sleep(0.1)
                out.append(store.get_run(run_id(arg["name"])).state)
            elif op == "observe":
                obs = await attempt(ctl.observe(run_id(arg["name"]), sign_in=arg.get("sign_in", False),
                                                find=arg.get("find")))
                if isinstance(obs, dict):
                    out.append(obs)
                    continue
                last[arg["name"]] = obs
                seen.append(obs.to_wire())
                out.append({"text": obs.text, "labels": [e.label_masked for e in obs.elements],
                            "source_id": obs.source_id, "generation": obs.generation})
                if obs.text_cut:
                    out[-1]["text_cut"] = True
                if arg.get("find"):
                    out[-1]["found"] = [e.label_masked for e in obs.elements if e.found]
                    out[-1]["find"] = obs.to_wire()["find"]
                    out[-1]["controls_left"] = obs.controls_left
                if arg.get("values"):          # slice 5m: what each field shows the model
                    out[-1]["values"] = {e.label_masked: e.value_masked for e in obs.elements}
            elif op == "look":
                # Signing in off the start page: observe with ``sign_in`` (Fido 0.1.21), timed;
                # a judgement it returns is the run's last one (``verdict`` applies it).
                started = time.monotonic()
                result = await attempt(ctl.observe(run_id(arg["name"]), sign_in=True))
                took = round(time.monotonic() - started, 3)
                if isinstance(result, dict):
                    out.append(result)
                    continue
                wire = result.to_wire()
                if "page_handle" in wire:
                    last[arg["name"]] = result
                    out.append({"observation": True, "text": result.text, "seconds": took,
                                "labels": [e.label_masked for e in result.elements]})
                else:
                    out.append({**wire, "seconds": took})
                    track(arg["name"], wire)
            elif op == "act":
                h, gen = handle(arg["name"], arg["label"], arg.get("nth", 0))
                choice = handle(arg["name"], arg["choice"])[0] if arg.get("choice") else None
                if arg.get("raw"):
                    # Through the wire parser, as the API route parses a body (S2-4 rule 1).
                    from casa_browser.protocol import parse_action
                    body = {"kind": arg["kind"], "handle": h, "generation": gen}
                    for key in ("text", "enter", "plan"):
                        if key in arg:
                            body[key] = arg[key]
                    try:
                        action_ = parse_action(body)
                    except ProtocolError as err:
                        out.append({"error": err.code})
                        continue
                else:
                    action_ = Action(arg["kind"], h, gen, text=arg.get("text"),
                                     choice_handle=choice, enter=arg.get("enter", False),
                                     direction=arg.get("direction"), amount=arg.get("amount"),
                                     plan=arg.get("plan"))
                result = await attempt(ctl.act(run_id(arg["name"]), action_))
                reply = result if isinstance(result, dict) else result.to_wire()
                if not arg.get("hold"):
                    reply = await settle(arg["name"], reply)
                out.append(reply)
                seen.append(out[-1])
                track(arg["name"], out[-1])
            elif op == "verdict":
                # The plugin's relay, scripted: the test's verdict for the run's last judgement,
                # bound to that packet's digest (``digest`` overrides it), or no verdict.
                import hashlib
                j = gate_last[arg["name"]]["judgement"]
                v = arg.get("verdict")
                body = {"no_verdict": "check_unavailable"} if v is None else {
                    "packet_sha256": arg.get("digest") or hashlib.sha256(
                        j["packet"].encode("utf-8")).hexdigest(), "verdict": v, "latency_ms": 1}
                reply = await attempt(ctl.apply_judgement(run_id(arg["name"]), j["id"], body))
                if arg.get("keep"):
                    gate_last[arg["name"]] = {"judgement": j}
                out.append(reply)
                track(arg["name"], reply)
            elif op == "picture":
                # What the relay fetches for the run's last judgement.
                j = gate_last[arg["name"]]["judgement"]
                got = await attempt(ctl.judgement_picture(run_id(arg["name"]), j["id"]))
                out.append(got if isinstance(got, dict) else {"png_bytes": len(got),
                                                              "png": got[:8] == b"\x89PNG\r\n\x1a\n"})
            elif op == "run_look":
                # What ``run_look`` gets: the page as a picture for a model, or None.
                out.append(await attempt(ctl.look(run_id(arg["name"]), None)))
            elif op == "read":
                # ``run_read``'s windows (``rows``: of the page's rows), followed to their end.
                parts, offset = [], 0
                while True:
                    got = await attempt(ctl.read(run_id(arg["name"]), None, None, offset,
                                                 rows=arg.get("rows", False)))
                    if "text" not in got:
                        parts.append(got)
                        break
                    parts.append(got)
                    if got["next_offset"] is None:
                        break
                    offset = got["next_offset"]
                out.append({"text": "".join(p.get("text", "") for p in parts),
                            "windows": len(parts), "last": {k: v for k, v in parts[-1].items()
                                                            if k != "text"}})
            elif op == "tap":
                qid = gate_question[arg["name"]]
                q = await ctl.get_question(run_id(arg["name"]), qid)
                reply = await attempt(ctl.answer_question(run_id(arg["name"]), qid, {
                    "revision": q["revision"], "digest": q["digest"],
                    "choice": arg.get("choice", "confirm"), "operator_ref": "it-operator"}))
                out.append(reply)
                track(arg["name"], reply)
            elif op == "sign_in":
                # Slice 5n: the model's sign-in card request, then (``verdict``) the plugin's
                # relay of the vault check, scripted.
                result = await attempt(ctl.ask_sign_in(run_id(arg["name"]), arg["site_name"]))
                reply = result if isinstance(result, dict) else result.to_wire()
                if reply.get("outcome") == "judging":
                    import hashlib
                    pending = (await ctl.pending_judgements(run_id(arg["name"])))["judgements"]
                    j = next(p for p in pending if p["kind"] == "login_item")
                    v = arg.get("verdict")
                    body = {"no_verdict": "check_unavailable"} if v is None else {
                        "packet_sha256": hashlib.sha256(j["packet"].encode()).hexdigest(),
                        "verdict": v}
                    reply = await attempt(ctl.apply_judgement(run_id(arg["name"]), j["id"], body))
                out.append(reply)
                track(arg["name"], reply)
            elif op == "question":
                out.append(await attempt(ctl.get_question(run_id(arg["name"]),
                                                          gate_question[arg["name"]])))
            elif op == "js":
                # Test instrumentation: the page's own (main-world) script changes or reports
                # the page, as a site's script would.
                page = browser.session.page
                out.append(await page.evaluate(arg["code"], isolated_context=False))
            elif op == "landed":
                # Test instrumentation: wait, bounded, until a request to ``path`` (path and
                # query) has completed in any frame of the page, cross-site frames included. A
                # wait proves nothing by itself: the test still counts what the fixture received.
                page = browser.session.page
                deadline = time.monotonic() + arg.get("timeout", 10)
                while not await _landed(page, arg["path"]) and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                out.append(None)
            elif op == "search":
                obs = await attempt(ctl.search(run_id(arg["name"]), arg["query"]))
                if isinstance(obs, dict):
                    out.append(obs)
                    continue
                if not hasattr(obs, "page_handle"):    # an address the check judges first
                    out.append(await settle(arg["name"], obs.to_wire()))
                    continue
                last[arg["name"]] = obs
                seen.append(obs.to_wire())
                out.append({"text": obs.text, "labels": [e.label_masked for e in obs.elements],
                            "source_id": obs.source_id})
            elif op == "questions":
                # every question the run asked the operator (its step log)
                out.append([[e.handle, e.text_masked, e.outcome]
                            for e in store.events_after(run_id(arg["name"]), 0, 1000)
                            if e.kind == "question"])
            elif op == "bind_provider":
                # A provider binding as a login live view's Done records it (tags only), under
                # the proxy key the controller read at its start.
                from dataclasses import replace as _replace
                from casa_browser.protocol import ProviderBinding
                site = store.get_site(arg["site_id"])
                binding = ProviderBinding("https://idp.provider.example", "a" * 64, "b" * 64, "c" * 64)
                store.put_site(_replace(site, provider_bindings=(binding,), revision=site.revision + 1))
                store.put_binding_key(site.site_id, ctl._binding_key_id)
                out.append(None)
            elif op == "bindings_lost":
                out.append(sorted(ctl.bindings_lost()))
            elif op == "sites":
                if arg:                            # slice 5n: each site's addresses too
                    out.append(sorted([s.site_id, list(s.origins), list(s.login_origins),
                                       s.start_page_private, s.vault_item_id]
                                      for s in store.list_sites()))
                else:
                    out.append(sorted(s.site_id for s in store.list_sites()))
            elif op == "forget":
                out.append(await attempt(ctl.forget(arg["site_id"], "it-operator")))
            elif op == "sources":
                # the trusted sources route (never a model surface: not added to ``seen``)
                out.append([[s.source_id, s.url, s.title_masked]
                            for s in await ctl.sources(run_id(arg["name"]))])
            elif op == "records":
                from datetime import datetime, timezone
                recs = await ctl.records(arg["requester"], datetime(2000, 1, 1, tzinfo=timezone.utc))
                out.append([r.to_wire() for r in recs])
            elif op == "archive":
                files = [f.file_id for f in store.files_for_run(run_id(arg["name"]))]
                rec = await attempt(ctl.archive(run_id(arg["name"]), tuple(files)))
                if isinstance(rec, dict):
                    out.append(rec)
                    continue
                import zipfile
                seen.append(rec.to_wire())
                with zipfile.ZipFile(rec.private_path) as z:
                    out.append({"file_id": rec.file_id, "names": sorted(z.namelist()),
                                "mime": rec.mime, "inputs": len(files)})
            elif op == "surfaces":
                # Everything a model or a log consumer could see for the run: every model-route
                # reply it was sent in full (observations, action results, verdict replies,
                # file records, archives), its events, the requester's records and the notices.
                from datetime import datetime, timezone
                rid = run_id(arg["name"])
                out.append({
                    "replies": seen,
                    "events": [e.to_wire() for e in store.events_after(rid, 0, 10_000)],
                    "records": [r.to_wire() for r in store.records_for(
                        arg.get("requester", "it"), datetime(2000, 1, 1, tzinfo=timezone.utc))],
                    "notices": [n.to_wire() for n in store.notices_after(0)]})
            elif op == "download":
                h, _ = handle(arg["name"], arg["label"])
                coro = ctl.download(run_id(arg["name"]), h)
                if arg.get("judged"):
                    reply = await attempt(coro)
                    reply = reply if isinstance(reply, dict) else reply.to_wire()
                    out.append(reply)
                    track(arg["name"], reply)
                    continue

                async def settled(coro=coro, name=arg["name"]):
                    reply = await coro
                    if getattr(reply, "outcome", None) == "judgement_required":
                        done = await settle(name, reply.to_wire())
                        if isinstance(done, dict) and "error" in done:
                            raise ProtocolError(done["error"])
                        if isinstance(done, dict) and done.get("outcome") != "done":
                            raise ProtocolError("blocked")
                        from casa_browser.protocol import FileRecord
                        f = done["file"]
                        return FileRecord(f["file_id"], f["run_id"], f["requester_id"],
                                          f["safe_name"], f["mime"], f["size"], f["sha256"],
                                          store.get_file(f["file_id"]).private_path,
                                          f["source_handle"])
                    return reply
                coro = settled()
                if arg.get("background"):
                    last["download"] = asyncio.ensure_future(attempt(coro))
                    await asyncio.sleep(arg.get("after", 1.0))
                    out.append(None)
                    continue
                rec = await attempt(coro)
                if isinstance(rec, dict):
                    out.append(rec)
                    continue
                seen.append(rec.to_wire())
                data = Path(rec.private_path).read_bytes()
                out.append({"file_id": rec.file_id, "head": data[:5].decode("latin-1"),
                            "size": rec.size, "mime": rec.mime})
            elif op == "await_download":
                rec = await last.pop("download")
                if not isinstance(rec, dict):
                    seen.append(rec.to_wire())
                out.append(rec if isinstance(rec, dict) else {"file_id": rec.file_id})
            elif op == "cancel":
                run = await ctl.cancel(run_id(arg["name"]))
                out.append(run.state)
            elif op == "finish":
                run = await attempt(ctl.finish(run_id(arg["name"]), ResultManifest(
                    "done", tuple(arg.get("file_ids", ())), tuple(arg.get("source_ids", ())),
                    "matched")))
                out.append(run if isinstance(run, dict) else run.state)
            elif op == "events":
                out.append([[e.kind, e.outcome] for e in store.events_after(run_id(arg["name"]), 0, 1000)])
            elif op == "state":
                out.append(store.get_run(run_id(arg["name"])).state)
            elif op == "states":
                out.append({r.run_id: r.state for r in store.list_runs(
                    ("queued", "starting", "running", "waiting_approval", "waiting_live", "live",
                     "finishing", "completed", "failed", "cancelled", "interrupted"))})
            elif op == "notices":
                out.append([[n.kind, n.run_id] for n in store.notices_after(0)])
            elif op == "files":
                out.append(sorted(p.name for p in (home / "artifacts" / "files").glob("*"))
                           if (home / "artifacts" / "files").exists() else [])
            elif op == "proxy_status":
                st = await ProxyClient().status()
                out.append({"run_id": st.get("run_id"), "window_open": st.get("window_open")})
            elif op == "browser":
                out.append(browser.session is not None)
            elif op == "tabs":
                # Slice 5w: Chrome's own tab list (Patchright lists neither a tab Chrome refuses
                # it the debugger on nor one an extension opened) and whether the site's page is
                # the one shown.
                page = browser.session.page
                cdp = await browser.session.context.new_cdp_session(page)
                try:
                    targets = (await cdp.send("Target.getTargets"))["targetInfos"]
                finally:
                    await cdp.detach()
                out.append({"tabs": len([t for t in targets if t["type"] == "page"]),
                            "front": await page.evaluate("() => document.visibilityState")})
            elif op == "stall_login":
                # Test instrumentation: the broker's first settle makes the renderer busy for
                # good with ordinary main-world page JavaScript and then does not return within
                # the driver's drain bound (the K1 shape).
                real_settle = driver.settle
                stall_started = asyncio.Event()

                async def stalled_settle(driver_session, timeout_s):
                    stall_started.set()
                    page = browser.session.page
                    await page.evaluate("() => { setTimeout(() => { for (;;) {} }, 0); }",
                                        isolated_context=False)
                    # A browser call that answers only to the operation's own 30 s timeout:
                    # longer than the driver's 10 s drain bound.
                    await asyncio.sleep(60)
                    return await real_settle(driver_session, timeout_s)
                driver.settle = stalled_settle
                out.append(None)
            elif op == "wait_stall":
                # The stalled settle began: the run is past its guest unlock and inside the
                # broker's sign-in (slice 5v: the guest's real waits make a fixed sleep unsafe).
                await asyncio.wait_for(stall_started.wait(), arg)
                await asyncio.sleep(1)
                out.append("stalled")
            elif op == "plant_log":
                # A planted canary on every output stream of this process (the logging file,
                # stderr, stdout): the secret scans must find it, which proves they cover them.
                import logging as _logging
                _logging.getLogger("casa_browser.it").warning("planted %s", arg["marker"])
                print("planted " + arg["marker"], file=sys.stderr, flush=True)
                print("planted " + arg["marker"], flush=True)
                out.append(None)
            elif op == "clock":
                out.append(time.monotonic())
            elif op == "sleep":
                await asyncio.sleep(arg)
                out.append(None)
            elif op == "ready":
                print("READY " + json.dumps(out), flush=True)
                await asyncio.sleep(arg.get("hold", 3600))
                out.append(None)
            elif op == "restart_proxy":
                # The parent restarts the service (root); the probe waits until it answers.
                print("READY restart", flush=True)
                await asyncio.sleep(arg.get("wait", 5))
                out.append(None)
            else:
                raise RuntimeError("unknown step " + op)
    finally:
        await ctl.shutdown()
        store.close()
    return out


def controller_home(tmp_path: Path) -> Path:
    """A home for the controller user under ``tmp_path`` (its ancestors made traversable)."""
    home = tmp_path / "controller-home"
    home.mkdir(exist_ok=True)
    os.chown(home, 1103, 1103)
    for p in [tmp_path, *tmp_path.parents]:
        if str(p).startswith("/tmp/"):
            p.chmod(p.stat().st_mode | 0o711)
    return home


def run_lifecycle(steps: list[dict], *, home: Path, broker_fixture=None, timeout: int = 300,
                  on_ready=None, killed: bool = False, logs: dict | None = None,
                  start_broker=None) -> list | None:
    """Run ``steps`` as casa-controller; ``on_ready(proc, line)`` is called at each READY.
    ``logs``, when given, receives every output stream of the run's processes, by name: the
    controller's stdout (all but the harness's own READY/RESULT lines) and stderr, and, when a
    broker fixture ran, the test broker's stdout and stderr."""
    import selectors
    sys.path.insert(0, str(Path(__file__).parent))
    from browserprobe import _start_test_broker

    # ``start_broker``: another broker process for the fixture (the live passkey pin's, 5v).
    broker = ((start_broker or _start_test_broker)(broker_fixture)
              if broker_fixture is not None else None)
    try:
        proc = subprocess.Popen(
            ["setpriv", "--reuid=casa-controller", "--regid=casa-controller", "--clear-groups",
             "/opt/venv/bin/python", __file__],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt:/opt/casa_browser_tests/tests",
                 "HOME": str(home)})
        proc.stdin.write(json.dumps({"steps": steps, "broker": broker_fixture}) + "\n")
        proc.stdin.close()
        deadline = time.monotonic() + timeout
        result = None
        stdout_lines = []
        with selectors.DefaultSelector() as sel:
            sel.register(proc.stdout, selectors.EVENT_READ)
            while True:
                if time.monotonic() > deadline:
                    proc.kill()
                    raise RuntimeError("lifecycle scenario timed out")
                if not sel.select(1):
                    if proc.poll() is not None:
                        break
                    continue
                line = proc.stdout.readline()
                if not line:
                    break
                if line.startswith("READY") and on_ready is not None:
                    on_ready(proc, line[len("READY "):].strip())
                elif line.startswith("RESULT "):
                    result = json.loads(line[len("RESULT "):])
                elif not line.startswith("READY"):
                    stdout_lines.append(line)
        proc.wait(30)
        stderr = proc.stderr.read()
        if logs is not None:
            logs["controller stdout"] = "".join(stdout_lines) + (proc.stdout.read() or "")
            logs["controller stderr"] = stderr
        if result is None and killed:
            return None
        if result is None:
            raise RuntimeError(f"scenario failed ({proc.returncode}): {stderr[-3000:]}")
        return result
    finally:
        if broker is not None:
            broker.terminate()
            try:
                broker.wait(10)
            except subprocess.TimeoutExpired:
                broker.kill()
                broker.wait()
            if logs is not None:
                logs["broker stdout"] = broker.stdout.read() if broker.stdout else ""
                logs["broker stderr"] = broker.stderr.read() if broker.stderr else ""


if __name__ == "__main__":
    payload = json.loads(sys.stdin.readline())
    result = asyncio.run(_scenario(payload["steps"], payload["broker"]))
    print("RESULT " + json.dumps(result), flush=True)
