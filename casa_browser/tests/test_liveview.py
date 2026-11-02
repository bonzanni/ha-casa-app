"""Task 13: the live view — grants, claims, the one viewer, the screen lock, the window, input,
the probe — on the real controller over Task 12's doubles and a fake screen."""
from __future__ import annotations

import asyncio
import json
import logging
import re

import aiohttp
import pytest

from casa_browser import liveview as lv
from casa_browser.liveview import LiveRefused, parse_input, viewer_state

TO = {"fi": 0, "doc": 7, "id": 1}     # the fake screen's focused field
from casa_browser.protocol import ProtocolError
from uihelpers import Operator, operator, rig, waiting_run  # noqa: F401


async def _until(cond, n=200):
    for _ in range(n):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


# --------------------------------------------------------------------------- grants and claims


async def test_live_grant_wrong_run_replay_expired(rig):
    run_id = await waiting_run(rig)
    grant = rig.live.grants[run_id]
    other = await rig.ctl.start_run(__import__("controllerfakes").spec(request_id="other"))
    other_id = other[0].run_id
    with pytest.raises(LiveRefused):                       # another run's route
        await rig.live.claim(other_id, grant.grant_id)
    with pytest.raises(LiveRefused):                       # an id that was never issued
        await rig.live.claim(run_id, "g-forged")
    token = await rig.live.claim(run_id, grant.grant_id)
    assert token and rig.state(run_id) == "live"
    with pytest.raises(LiveRefused):                       # replay: the grant is spent
        await rig.live.claim(run_id, grant.grant_id)
    # expired: a fresh run's grant 121 s later is refused by its own page
    await rig.ctl.live_done(run_id)
    await rig.ctl.request_live(run_id, "login")
    later = rig.live.grants[run_id]
    rig.clock.advance(121)
    with pytest.raises(LiveRefused):
        await rig.live.claim(run_id, later.grant_id)
    assert rig.state(run_id) == "waiting_live"
    assert sum(1 for e in rig.log if e[:2] == ("broker", "begin_capture")) == 1


async def test_claim_never_rebinds(rig):
    run_id = await waiting_run(rig)
    grant_id = rig.live.grants[run_id].grant_id
    first = await rig.live.claim(run_id, grant_id)
    session = rig.live.sessions[run_id]
    with pytest.raises(LiveRefused):
        await rig.live.claim(run_id, grant_id)
    rig.clock.advance(500)
    with pytest.raises(LiveRefused):
        await rig.live.claim(run_id, grant_id)
    assert rig.live.sessions[run_id] is session and session.token_sha256 == lv._sha(first)


async def test_open_after_expiry_regrants(rig):
    run_id = await waiting_run(rig)
    old = rig.live.grants[run_id].grant_id
    rig.clock.advance(121)
    token = await rig.live.open_from_home(run_id)
    assert token and rig.state(run_id) == "live" and run_id in rig.live.sessions
    assert rig.live.grant_run(old) is None
    with pytest.raises(LiveRefused):
        await rig.live.claim(run_id, old)
    # the run stays paused: no model step while live
    with pytest.raises(ProtocolError, match="not_ready"):
        await rig.ctl.observe(run_id)


async def test_open_keeps_established_session(rig):
    run_id = await waiting_run(rig)
    first = await rig.live.open_from_home(run_id)
    session = rig.live.sessions[run_id]
    begun = rig.log.count(("broker", "begin_capture", run_id, "sa"))
    second = await rig.live.open_from_home(run_id)
    assert second != first and rig.live.sessions[run_id] is session
    assert rig.log.count(("broker", "begin_capture", run_id, "sa")) == begun == 1
    assert run_id not in rig.live.grants                   # no grant was issued for it
    await rig.ctl.live_done(run_id)
    done = [e for e in rig.events(run_id) if e.kind == "live_done"]
    assert len(done) == 1 and rig.state(run_id) == "running"
    with pytest.raises(ProtocolError):
        await rig.ctl.live_done(run_id)                    # a second Done resumes nothing
    assert len([e for e in rig.events(run_id) if e.kind == "live_done"]) == 1


async def test_a_device_taken_over_is_told_elsewhere(rig):
    """casa-test F12: another device's Open (its own cookie) takes the view over; the first
    device's page is told the view was opened elsewhere, not that it ended. A cookie the view
    never had is still "ended", and so is the old device once the view itself is over."""
    run_id = await waiting_run(rig)
    first = await rig.live.open_from_home(run_id)
    phase, viewer = await rig.live.serve_viewer(run_id, first)
    assert phase == "viewing" and rig.live.view_state(run_id, first, viewer) == "viewing"
    second = await rig.live.open_from_home(run_id)          # another device
    assert rig.live.view_state(run_id, first, viewer) == "elsewhere"
    assert rig.live.view_state(run_id, "never-a-cookie", viewer) == "ended"   # control
    assert not rig.live.is_viewer(run_id, first, viewer)     # never a way back in
    assert rig.live.authorize(run_id, first, viewer) is None
    tokens = [second] + [await rig.live.open_from_home(run_id) for _ in range(lv.MAX_REPLACED + 2)]
    assert len(rig.live.sessions[run_id].replaced) == lv.MAX_REPLACED    # bounded
    assert rig.live.view_state(run_id, tokens[-2], None) == "elsewhere"
    assert rig.live.view_state(run_id, first, viewer) == "ended"          # the oldest, forgotten
    await rig.ctl.live_done(run_id)
    assert rig.live.view_state(run_id, tokens[-2], None) == "ended"


async def test_claim_serialised(rig):
    """Two Opens at once: one begin_live, one view; the second reconnects to it."""
    run_id = await waiting_run(rig)
    tokens = await asyncio.gather(rig.live.open_from_home(run_id), rig.live.open_from_home(run_id))
    assert len(set(tokens)) == 2
    assert rig.log.count(("broker", "begin_capture", run_id, "sa")) == 1
    assert [e.kind for e in rig.events(run_id)].count("live_unavailable") == 0
    assert rig.live.sessions[run_id].token_sha256 == lv._sha(tokens[1])


async def test_capture_armed_before_input(rig):
    """No session (hence no socket, no input) exists until the broker acknowledged the
    capture; a capture failure offers no view at all."""
    run_id = await waiting_run(rig)
    seen = {}
    real = rig.broker.call

    async def call(op, **args):
        if op == "begin_capture":
            seen["session_before_ack"] = run_id in rig.live.sessions
        reply = await real(op, **args)
        if op == "begin_capture":
            seen["session_at_ack"] = run_id in rig.live.sessions
        return reply
    rig.broker.call = call
    await rig.live.open_from_home(run_id)
    assert seen == {"session_before_ack": False, "session_at_ack": False}
    assert run_id in rig.live.sessions
    await rig.ctl.live_done(run_id)
    # a refused capture: no session, the run back to running, the grant gone
    await rig.ctl.request_live(run_id, "login")
    rig.broker.capture_reply = {"error": "conflict"}
    with pytest.raises(LiveRefused):
        await rig.live.open_from_home(run_id)
    assert run_id not in rig.live.sessions and run_id not in rig.live.grants
    assert rig.state(run_id) == "running"


# --------------------------------------------------------------------------- the viewer and sockets


async def _vnc_server(path):
    got = []

    async def handle(reader, writer):
        writer.write(b"RFB 003.008\n")
        await writer.drain()
        got.append(await reader.read(100))
        writer.close()
    server = await asyncio.start_unix_server(handle, path=str(path))
    return server, got


async def test_ctl_socket_gated_like_vnc(rig, operator):
    server, _ = await _vnc_server(rig.tmp / "vnc.sock")
    run_id = await waiting_run(rig)
    for kind in ("ctl", "websockify"):                     # no session yet
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await operator.ws(run_id, kind, "v-none")
        assert err.value.status == 403
    viewer = await operator.open_view(run_id)
    stranger = Operator(operator.client)                   # HA-authenticated, but not this view
    for kind in ("ctl", "websockify"):
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await stranger.ws(run_id, kind, viewer)
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await operator.ws(run_id, kind, "v-wrong")
    ctl = await operator.ws(run_id, "ctl", viewer)
    vnc = await operator.ws(run_id, "websockify", viewer)
    assert (await vnc.receive_bytes(timeout=2)) == b"RFB 003.008\n"
    await rig.ctl.live_done(run_id)                        # Done closes both
    for ws in (ctl, vnc):
        msg = await ws.receive(timeout=2)
        while msg.type not in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
            msg = await ws.receive(timeout=2)
    assert ctl.close_code in (lv.CLOSE_DONE, lv.CLOSE_ENDED) and vnc.closed
    # revocation (re-pair) closes a view as well
    await rig.ctl.request_live(run_id, "login")
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    rig.ctl.pairing.arm()
    msg = await ctl.receive(timeout=2)
    while msg.type not in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
        msg = await ctl.receive(timeout=2)
    assert ctl.close_code == lv.CLOSE_ENDED
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await operator.ws(run_id, "ctl", viewer)
    server.close()


async def test_socket_disconnect_not_resume(rig, operator):
    """A dropped socket (an app switch) changes nothing: the run stays live and paused, the same
    viewer reconnects, the session is the same; an ended view's cookie opens nothing."""
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    session = rig.live.sessions[run_id]
    ctl = await operator.ws(run_id, "ctl", viewer)
    await ctl.close()
    await asyncio.sleep(0.05)
    assert rig.state(run_id) == "live" and rig.live.sessions[run_id] is session
    with pytest.raises(ProtocolError, match="not_ready"):
        await rig.ctl.act(run_id, __import__("casa_browser.protocol", fromlist=["Action"]).Action(
            "click", "h", 1, None, None, None, None))
    again = await operator.ws(run_id, "ctl", viewer)       # reconnect: same viewer id
    assert not again.closed
    assert rig.log.count(("broker", "begin_capture", run_id, "sa")) == 1
    await again.close()
    await rig.ctl.cancel(run_id)
    assert run_id not in rig.live.sessions
    other = await waiting_run(rig, request_id="next")
    with pytest.raises(aiohttp.WSServerHandshakeError):    # the old cookie, another run's view
        await operator.ws(other, "ctl", viewer)


async def test_stale_viewer_refused(rig, operator):
    """d1/d2: another page (a second Open, a reload) becomes the viewer; the old page's upgrade
    is refused, and an input it sent that was still waiting for the screen is dropped."""
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    await _until(lambda: rig.live.sessions[run_id].state is not None)
    await rig.live._screen.acquire()                       # a probe holds the screen
    await ctl.send_str(json.dumps({"t": "text", "s": "queued", "to": TO}))
    await asyncio.sleep(0.05)                              # waiting for the screen lock
    session = rig.live.sessions[run_id]
    session.ctl[ctl_server(session)] = viewer              # still registered: only the id differs
    second = Operator(operator.client)
    await second.get("/")
    opening = asyncio.ensure_future(second.post(f"/runs/{run_id}/open"))   # a second Open
    await asyncio.sleep(0.05)
    assert session.viewer_id is None and not opening.done()   # retired at once; then it drains
    rig.live._screen.release()
    status, _, resp = await opening
    assert status == 303
    await asyncio.sleep(0.05)
    assert not any(e[:2] == ("injected", "text") for e in rig.log)
    # Before the new page is served, the old page (same browser, the new cookie) cannot reconnect.
    same_browser = Operator(operator.client)
    same_browser.cookie = second.cookie
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await same_browser.ws(run_id, "ctl", viewer)
    newer = re.search(r'data-viewer="([^"]+)"', (await second.follow(resp))[1]).group(1)
    assert newer != viewer
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await same_browser.ws(run_id, "ctl", viewer)
    assert rig.live.view_state(run_id, second.cookie, viewer) == "elsewhere"


def ctl_server(session):
    """The server side of the session's one /ctl socket."""
    return next(iter(session.ctl))


# --------------------------------------------------------------------------- window and input


async def test_window_restored_before_resume(rig, operator):
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    await _until(lambda: rig.live.sessions[run_id].state is not None)
    await ctl.send_str(json.dumps({"t": "fit", "w": 390, "h": 700}))
    assert await _until(lambda: ("fit", 500, 700) in rig.log)    # Chrome's 500 px floor
    await rig.ctl.live_done(run_id)
    restore = rig.log.index(("restore", True))
    resumed = max(i for i, e in enumerate(rig.log) if e[:2] == ("broker", "end_capture"))
    assert restore < resumed and rig.screen.bounds == (0, 0, 1920, 1080)
    assert rig.state(run_id) == "running"


async def test_live_complete_route_restores_window(rig):
    """/v1 …/live-complete never passes through the ingress Done: the controller restores."""
    run_id = await waiting_run(rig)
    await rig.live.open_from_home(run_id)
    await rig.live._screen.acquire()
    rig.live._screen.release()
    rig.screen.bounds = (0, 0, 500, 700)
    await rig.ctl.live_complete(run_id, "op")
    assert ("restore", True) in rig.log and rig.screen.bounds == (0, 0, 1920, 1080)


async def test_restore_failure_interrupts(rig):
    run_id = await waiting_run(rig)
    await rig.live.open_from_home(run_id)
    rig.screen.restore_ok = False
    with pytest.raises(ProtocolError, match="interrupted"):
        await rig.ctl.live_done(run_id)
    assert rig.state(run_id) == "interrupted"
    assert [e.outcome for e in rig.events(run_id, "run_interrupted")] == ["window_unrestored"]


async def test_no_input_after_close_view(rig, operator):
    """Done's restore runs after the one injection in flight, and nothing queued behind it."""
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    await _until(lambda: rig.live.sessions[run_id].state is not None)
    rig.screen.inject_gate = asyncio.Event()
    await ctl.send_str(json.dumps({"t": "tap", "x": 50, "y": 210}))
    assert await _until(lambda: ("inject", "tap") in rig.log)
    await ctl.send_str(json.dumps({"t": "key", "k": "Enter"}))
    closing = asyncio.ensure_future(rig.live.close_view(run_id))
    await asyncio.sleep(0.02)
    assert not closing.done()                              # waits for the injection in flight
    rig.screen.inject_gate.set()
    assert await closing
    kinds = [e for e in rig.log if e[0] in ("injected", "restore")]
    assert kinds == [("injected", "tap", None), ("restore", True)]
    rig.screen.inject_gate = None


@pytest.mark.parametrize("ender", ["cancel", "repair", "proxy_restart"])
async def test_end_stops_input_at_once(rig, operator, ender):
    """d2 (Terra S1, Astra S1): a cancel, a re-pair or an interruption ends the view before it
    waits for the command lock: an input still waiting for the screen is dropped."""
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    await _until(lambda: rig.live.sessions[run_id].state is not None)
    session = rig.live.sessions[run_id]
    await rig.ctl._lock.acquire()       # a command holds the lock: the end cannot have it yet
    await rig.live._screen.acquire()    # a probe holds the screen: the input waits for it
    try:
        await ctl.send_str(json.dumps({"t": "text", "s": "after", "to": TO}))
        await asyncio.sleep(0.05)
        socket = ctl_server(session)
        if ender == "cancel":
            ending = asyncio.ensure_future(rig.ctl.cancel(run_id))
        elif ender == "repair":
            rig.ctl.pairing.arm()
            ending = None
        else:
            ending = asyncio.ensure_future(rig.ctl._interrupt(run_id, "proxy_restarted"))
        await asyncio.sleep(0.02)
        assert rig.live.sessions.get(run_id) is None and session.phase == "ended"
        session.ctl[socket] = viewer    # even with the socket still registered
        rig.live._screen.release()
        await asyncio.sleep(0.05)
        assert not any(e[:2] == ("injected", "text") for e in rig.log)
    finally:
        if rig.live._screen.locked():
            rig.live._screen.release()
        rig.ctl._lock.release()
    if ending is not None:
        await ending
    await _until(lambda: rig.state(run_id) in ("cancelled", "interrupted"))


async def test_end_drains_input_before_capture_release(rig, operator):
    """The teardown waits for the one injection in flight before it ends the broker's
    capture: no operator keystroke reaches Chrome after the capture stopped."""
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    await _until(lambda: rig.live.sessions[run_id].state is not None)
    rig.screen.inject_gate = asyncio.Event()
    await ctl.send_str(json.dumps({"t": "text", "s": "typing", "to": TO}))
    assert await _until(lambda: ("inject", "text") in rig.log)
    ending = asyncio.ensure_future(rig.ctl.cancel(run_id))
    await asyncio.sleep(0.05)
    assert not any(e[:2] == ("broker", "end_capture") for e in rig.log)   # still waiting
    rig.screen.inject_gate.set()
    await ending
    injected = rig.log.index(("injected", "text", "typing"))
    ended = next(i for i, e in enumerate(rig.log) if e[:2] == ("broker", "end_capture"))
    assert injected < ended and rig.state(run_id) == "cancelled"
    rig.screen.inject_gate = None


async def test_type_box_sends_keystrokes(rig, operator):
    """The typing box's text reaches the field as typed text, and the controller logs nothing
    of it."""
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    await _until(lambda: rig.live.sessions[run_id].state is not None)
    await ctl.send_str(json.dumps({"t": "text", "s": "Corr3ct-Horse€ü!", "to": TO}))
    await ctl.send_str(json.dumps({"t": "text", "s": "", "to": TO}))              # nothing: dropped
    await ctl.send_str(json.dumps({"t": "key", "k": "CtrlBackspace", "to": TO}))
    await ctl.send_str(json.dumps({"t": "key", "k": "Tab"}))
    await ctl.send_str(json.dumps({"t": "key", "k": "rm -rf"}))            # not a key: dropped
    await ctl.send_str(json.dumps({"t": "text", "s": "x" * 257}))   # too long: dropped
    assert await _until(lambda: ("injected", "key", None) in rig.log)
    await asyncio.sleep(0.05)
    injected = [e for e in rig.log if e[0] == "injected"]
    assert injected == [("injected", "text", "Corr3ct-Horse€ü!"), ("injected", "key", None),
                        ("injected", "key", None)]
    events = json.dumps([e.text_masked for e in rig.events(run_id)])
    assert "Corr3ct" not in events


def test_parse_input_is_fixed_and_clamped():
    state = {"win": [0, 0, 500, 800]}
    assert parse_input({"t": "tap", "x": 9999, "y": -5}, state) == {"t": "tap", "x": 499, "y": 0}
    assert parse_input({"t": "tap", "x": 1, "y": 1}, None) is None      # nothing before a probe
    assert parse_input({"t": "eval", "js": "1"}, state) is None
    assert parse_input({"t": "scroll", "x": 1, "y": 1, "n": 999}, state)["n"] == lv.MAX_NOTCHES
    assert parse_input({"t": "fit", "w": 100, "h": 99999}, state) == {"t": "fit", "w": 500, "h": 1080}
    assert parse_input({"t": "key", "k": "F12"}, state) is None


def test_viewer_never_gets_a_field_value():
    """The probe reads a password field's length only, and the viewer's message carries no
    field's value at all (option C: nothing on the phone mirrors a field), nor a password
    field's autocomplete hint."""
    assert "secret ? null" in lv.PROBE and "ac: secret ? ''" in lv.PROBE
    probe = {"win": [0, 0, 500, 800], "origin": [0, 80], "view": [500, 720], "host": "a",
             "fields": [{"type": "password", "val": "hunter2-canary", "len": 14, "ml": 64,
                         "ac": "current-password", "id": 3, "fi": 0, "doc": 7},
                        {"type": "email", "val": "me@example.com", "len": 14, "id": 4, "fi": 0,
                         "doc": 7, "lb": "Email"}]}
    message = viewer_state(probe, 41, [39], [40])
    text = json.dumps(message)
    assert "hunter2-canary" not in text and "me@example.com" not in text
    assert all(set(f) == set(lv.VIEWER_FIELD) | {"tx"} for f in message["fields"])
    # `tx` (the strip's arrows): whether the field holds text — a boolean, never its length
    assert [f["tx"] for f in message["fields"]] == [True, True] and "14" not in text
    empty = viewer_state(dict(probe, fields=[dict(probe["fields"][0], len=0, val="")]))
    assert empty["fields"][0]["tx"] is False
    assert message["fields"][0]["ac"] == "" and message["fields"][1]["lb"] == "Email"
    assert message["ack"] == 41 and message["refused"] == [39] and message["unsure"] == [40]
    assert message["fields"][1]["id"] == 4


async def test_probe_output_not_logged(rig, operator, caplog):
    rig.screen.fields[0]["val"] = "probe-canary-value"
    rig.screen.fields[0]["lb"] = "label-canary"
    caplog.set_level(logging.DEBUG)
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    msg = await ctl.receive_json(timeout=2)
    while msg.get("t") != "state":
        msg = await ctl.receive_json(timeout=2)
    assert msg["fields"][0]["lb"] == "label-canary"            # it reaches the viewer only
    assert "probe-canary-value" not in json.dumps(msg)          # a value never does
    await ctl.send_str(json.dumps({"t": "text", "s": "typed-canary", "to": TO}))
    await _until(lambda: ("injected", "text", "typed-canary") in rig.log)
    records = " ".join(r.getMessage() for r in caplog.records)
    assert "probe-canary-value" not in records and "typed-canary" not in records
    assert "label-canary" not in records
    assert "probe-canary-value" not in json.dumps([e.text_masked for e in rig.events(run_id)])


def test_parse_input_target():
    """Text and keys name the field they are meant for by frame index, document and id (never
    clamped: a wrong key drops the message); a tap is a click (live view K: no focus op, no
    ``focusfield``)."""
    state = {"win": [0, 0, 500, 800]}
    key = {"fi": 1, "doc": 1730000000123456, "id": 9}
    assert parse_input({"t": "tap", "x": 9999, "y": 5, "q": 3}, state) == {"t": "tap", "x": 499, "y": 5}
    for bad in ({"fi": -1}, {"doc": 2 ** 53}, {"id": 0}, {"id": True}, {"id": "9"}, {"doc": 1.5}):
        assert parse_input({"t": "text", "s": "a", "to": {**key, **bad}}, state) is None, bad
    for gone in ("focus", "focusfield"):
        assert parse_input({"t": gone, **key, "x": 1, "y": 1}, state) is None
    assert parse_input({"t": "text", "s": "a", "to": key}, state)["to"] == key
    assert parse_input({"t": "key", "k": "Enter", "to": key}, state) == {"t": "key", "k": "Enter", "to": key}
    assert parse_input({"t": "text", "s": "a", "to": {"fi": 0}}, state) is None   # a bad target drops it
    assert parse_input({"t": "text", "s": "a", "to": None}, state) is None       # no target: dropped
    assert parse_input({"t": "text", "s": "a"}, state) is None
    assert parse_input({"t": "text", "s": "a", "bar": 1}, state) is None         # bar means True only
    assert parse_input({"t": "text", "s": "a", "bar": True}, state) == {"t": "text", "s": "a"}
    assert "to" not in parse_input({"t": "key", "k": "Tab"}, state)              # a key alone is fine
    assert lv.message_number({"t": "key", "q": 17}) == 17
    assert lv.message_number({"t": "scroll", "n": 3}) is None                    # notches are not q
    assert lv.message_number({"q": True}) is None and lv.message_number({"q": -1}) is None


async def _state(ctl, pred=lambda m: True):
    while True:
        msg = await ctl.receive_json(timeout=2)
        if msg.get("t") == "state" and pred(msg):
            return msg


async def test_states_acknowledge_the_last_message(rig, operator):
    """Every state carries ``ack``: the last ``q`` the controller took, so the viewer knows a
    state was probed after its tap — taken counts, run or dropped."""
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    first = await _state(ctl)
    assert first["ack"] == 0
    await ctl.send_str(json.dumps({"t": "tap", "x": 50, "y": 210, "q": 101}))
    assert (await _state(ctl, lambda m: m["ack"] == 101))["ack"] == 101
    assert ("injected", "tap", None) in rig.log
    await ctl.send_str(json.dumps({"t": "key", "k": "F12", "q": 102}))          # dropped, still taken
    assert (await _state(ctl, lambda m: m["ack"] == 102))["ack"] == 102


async def test_text_for_a_field_types_only_while_chrome_focuses_it(rig, operator):
    """The guard: a text (or key) naming its field is typed only while Chrome's focus is that
    field; otherwise nothing is typed and every later state lists the text's q as refused. Text
    naming no field is never typed, except address-bar text."""
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    await _state(ctl)
    to = {"fi": 0, "doc": 7, "id": 1}
    await ctl.send_str(json.dumps({"t": "text", "s": "here", "q": 5, "to": to}))
    assert await _until(lambda: ("injected", "text", "here") in rig.log)
    rig.screen.focus_ok = False
    await ctl.send_str(json.dumps({"t": "text", "s": "moved-canary", "q": 6, "to": to}))
    await ctl.send_str(json.dumps({"t": "key", "k": "Enter", "to": to}))
    told = await _state(ctl, lambda m: m["ack"] >= 6)
    assert told["refused"] == [6]                    # the outcome travels in the states
    await asyncio.sleep(0.1)
    injected = [e for e in rig.log if e[0] == "injected"]
    assert injected == [("injected", "text", "here")]                # nothing after the move
    assert [e for e in rig.log if e[0] == "focused_is"] == [("focused_is", 1, True),
                                                           ("focused_is", 1, False),
                                                           ("focused_is", 1, False)]
    # every refusal stays in the states until the viewer says it read it (``ok``): nine
    # refused pieces of one paste, then one state, lose none (d5)
    for q in range(7, 16):
        await ctl.send_str(json.dumps({"t": "text", "s": "p", "q": q, "ok": 5, "to": to}))
    told = await _state(ctl, lambda m: m["ack"] >= 15)
    assert told["refused"] == list(range(6, 16))
    await ctl.send_str(json.dumps({"t": "key", "k": "Tab", "q": 16, "ok": 11}))   # read up to 11
    await ctl.send_str(json.dumps({"t": "text", "s": "p", "q": 17, "ok": 11, "to": to}))
    told = await _state(ctl, lambda m: m["ack"] >= 17)
    assert told["refused"] == [12, 13, 14, 15, 17]
    # a text whose typing failed part-way is unsure; one dropped (no target) is refused
    rig.screen.focus_ok = True
    rig.screen.inject_error = RuntimeError("xdotool")
    await ctl.send_str(json.dumps({"t": "text", "s": "half", "q": 18, "ok": 17, "to": to}))
    await _state(ctl, lambda m: m["ack"] >= 18)
    rig.screen.inject_error = None
    await ctl.send_str(json.dumps({"t": "text", "s": "nowhere", "q": 19, "ok": 17}))
    told = await _state(ctl, lambda m: m["ack"] >= 19)
    assert told["unsure"] == [18] and told["refused"] == [19]
    # a numbered key for a field (an empty-box Backspace) is refused and told like a text
    rig.screen.focus_ok = False
    await ctl.send_str(json.dumps({"t": "key", "k": "Backspace", "q": 20, "ok": 17, "to": to}))
    told = await _state(ctl, lambda m: m["ack"] >= 20)
    assert 20 in told["refused"]
    rig.screen.focus_ok = False
    # a new socket starts clean: its page gives back whatever the old one left unresolved
    again = await operator.ws(run_id, "ctl", viewer)
    fresh = await _state(again)
    assert fresh["refused"] == [] and fresh["unsure"] == []
    ctl = again
    rig.screen.focus_ok = True
    # the rule: text without a checked field is never typed — only address-bar text (``bar``)
    await ctl.send_str(json.dumps({"t": "text", "s": "free-canary"}))
    await ctl.send_str(json.dumps({"t": "text", "s": "bar-text", "bar": True}))
    assert await _until(lambda: ("injected", "text", "bar-text") in rig.log)
    assert ("injected", "text", "free-canary") not in rig.log


async def test_selection_offered_once_and_cleared(rig, operator):
    rig.screen.selections = ["K7QF-29XM"]
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    offers = []
    for _ in range(20):
        msg = await ctl.receive_json(timeout=2)
        if msg.get("t") == "sel":
            offers.append(msg["text"])
            break
    assert offers == ["K7QF-29XM"]
    clears = [i for i, e in enumerate(rig.log) if e == ("clear_selection",)]
    assert len(clears) >= 2          # when the view opened, and after the offer


async def test_the_viewers_own_select_all_is_no_copy_offer(rig, operator):
    """casa-test F32: the viewer's SelectAll (before an address typed for Chrome's address bar)
    selects the old address, which Chrome puts in the X selection: that selection is read and
    cleared, never offered. A selection made later is offered again (control)."""
    from casa_browser import liveview as lv_mod
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    msg = await ctl.receive_json(timeout=2)
    while msg.get("t") != "state":
        msg = await ctl.receive_json(timeout=2)
    real = rig.screen.inject

    async def inject(bs, op):
        await real(bs, op)
        if op["t"] == "key" and op["k"] == "SelectAll":
            rig.screen.selections.append("about:blank")      # what Chrome selected
    rig.screen.inject = inject
    await ctl.send_str(json.dumps({"t": "key", "k": "SelectAll"}))
    await _until(lambda: ("injected", "key", None) in rig.log)
    got = []
    for _ in range(6):
        try:
            got.append(await ctl.receive_json(timeout=0.5))
        except asyncio.TimeoutError:
            pass
    assert rig.screen.selections == [] and not [m for m in got if m.get("t") == "sel"]
    rig.live.sessions[run_id].quiet_selection_until = 0.0     # the quiet time is over
    rig.screen.selections.append("K7QF-29XM")
    offers = []
    for _ in range(10):
        try:
            m = await ctl.receive_json(timeout=0.5)
        except asyncio.TimeoutError:
            continue
        if m.get("t") == "sel":
            offers.append(m["text"])
            break
    assert offers == ["K7QF-29XM"]
    assert lv_mod.SELECTALL_QUIET_S >= 1.0


@pytest.mark.parametrize("when", ["before", "during", "away", "left", "unicode", "cleared"])
async def test_a_selection_in_a_password_field_is_never_offered(rig, operator, when):
    """Found by the image lane: Tab into a filled password field selects its value, which Chrome
    puts in the X selection; the viewer must never get it as a copy offer. Whether the focus is
    in the password field before the read or moves there while it runs, the selection is
    cleared and not offered; the same selection from an email field is (control)."""
    password_field = {"id": 2, "x": 40, "y": 260, "w": 300, "h": 30, "type": "password", "im": "",
                      "ac": "", "ml": 0, "focused": True, "len": 12, "lb": "Password",
                      "val": None, "fi": 0, "doc": 7}
    email = dict(rig.screen.fields[0], focused=False)
    run_id = await waiting_run(rig)
    if when == "before":
        rig.screen.fields = [email, password_field]
    elif when == "cleared":
        # the page empties its password field and moves the focus out during the read
        rig.screen.fields = [email, password_field]
        real = rig.screen.selection

        async def clearing():
            rig.screen.fields = [dict(email, focused=True), dict(password_field, focused=False, len=0)]
            return await real()
        rig.screen.selection = clearing
    elif when == "left":
        # in the password field before the read, the page moves the focus out during it
        rig.screen.fields = [email, password_field]
        real = rig.screen.selection

        async def leaving():
            rig.screen.fields = [dict(email, focused=True), dict(password_field, focused=False)]
            return await real()
        rig.screen.selection = leaving
    elif when in ("away", "unicode"):
        # r12: the page moves the focus back out of the password field before either look;
        # the selection is as long as the password field's value (len 12 above)
        rig.screen.fields = [dict(email, focused=True), dict(password_field, focused=False)]
    else:
        # the probe before the read sees the email field; the read moves the focus first
        real = rig.screen.selection

        async def moving():
            rig.screen.fields = [email, password_field]
            return await real()
        rig.screen.selection = moving
    rig.screen.selections = [{"away": "pw-canary-12", "left": "pw-c",
                              "unicode": "pw-canary-\U0001f511"}.get(when, "pw-canary-value")]
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    got = []
    for _ in range(8):
        try:
            msg = await ctl.receive_json(timeout=0.5)
        except asyncio.TimeoutError:
            continue
        got.append(msg.get("t"))
    assert "sel" not in got and "state" in got
    assert rig.screen.selections == []                       # it was read
    assert sum(1 for e in rig.log if e == ("clear_selection",)) >= 2   # and cleared
    from casa_browser.liveview import selection_offerable
    assert selection_offerable({"fields": [dict(email, focused=True)]})       # control
    with_password = {"fields": [dict(email, focused=True), dict(password_field, focused=False)]}
    assert not selection_offerable(with_password)            # a typed password on the page
    empty = {"fields": [dict(email, focused=True), dict(password_field, focused=False, len=0)]}
    assert selection_offerable(empty)                        # control: an empty password field
    assert not selection_offerable({"fields": [], "focus_in_frame": True})
    assert not selection_offerable(None)


async def test_vnc_bridge_stops_for_a_stale_viewer(rig, operator):
    """r1 Terra S1: a picture socket accepted just before another page became the viewer
    forwards no frame after it, and one whose viewer is already stale gets none at all."""
    sent = []

    async def handle(reader, writer):
        for n in range(200):                       # x11vnc keeps sending frames
            writer.write(b"F%03d" % n)
            await writer.drain()
            sent.append(n)
            await asyncio.sleep(0.01)
        writer.close()
    server = await asyncio.start_unix_server(handle, path=str(rig.tmp / "vnc.sock"))
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    vnc = await operator.ws(run_id, "websockify", viewer)
    assert (await vnc.receive_bytes(timeout=2)).startswith(b"F")
    second = Operator(operator.client)
    await second.open_view(run_id)                 # another page is the viewer now
    got_after = []
    try:
        while True:
            msg = await vnc.receive(timeout=1)
            if msg.type != aiohttp.WSMsgType.BINARY:
                break
            got_after.append(msg.data)
    except asyncio.TimeoutError:
        pass
    assert len(got_after) <= 1                     # at most the frame already in flight
    # a stale viewer id at the bridge itself (authorised, then replaced): nothing is sent
    session = rig.live.sessions[run_id]

    class Sink:
        def __init__(self):
            self.frames = []
            self.closed = None

        async def send_bytes(self, data):
            self.frames.append(data)

        async def close(self, code=1000):
            self.closed = code

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(0.2)
            raise StopAsyncIteration
    sink = Sink()
    await rig.live.vnc_socket(sink, session, viewer)    # viewer is the old, replaced id
    assert sink.frames == [] and sink.closed == lv.CLOSE_ELSEWHERE
    # a bridge whose viewer is replaced while it runs (even before its socket is closed)
    # forwards nothing more: the check runs before every frame
    current = session.viewer_id

    class LongSink(Sink):
        async def __anext__(self):
            await asyncio.sleep(5)
            raise StopAsyncIteration
    live = LongSink()
    bridge = asyncio.ensure_future(rig.live.vnc_socket(live, session, current))
    assert await _until(lambda: len(live.frames) >= 2)
    session.viewer_id = "v-someone-else"
    await asyncio.sleep(0.05)
    count = len(live.frames)
    await asyncio.sleep(0.3)
    assert len(live.frames) == count
    bridge.cancel()
    server.close()


async def test_timed_out_injection_is_stopped(tmp_path):
    """r1 Astra S1: an input command that outlives its bound is stopped — it does not go on
    typing after the screen lock was released (our own child process only)."""
    import sys
    marker = tmp_path / "typed-late"
    screen = lv.ChromeScreen(xdotool=sys.executable)
    code = f"import time, pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('x')"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(screen.xdo("-c", code), 0.3)
    await asyncio.sleep(2.0)
    assert not marker.exists()
    await asyncio.wait_for(screen.xdo("-c", code), 5)         # the positive control
    assert marker.exists()


async def test_viewer_change_waits_for_the_old_viewers_injection(rig, operator):
    """r3 Terra S1 (generalised): a second Open — like every end — returns only after the old
    page's injection in flight finished; nothing of the old page runs after it."""
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    await _until(lambda: rig.live.sessions[run_id].state is not None)
    rig.screen.inject_gate = asyncio.Event()
    await ctl.send_str(json.dumps({"t": "tap", "x": 50, "y": 210}))
    assert await _until(lambda: ("inject", "tap") in rig.log)
    reopening = asyncio.ensure_future(rig.live.open_from_home(run_id))
    await asyncio.sleep(0.05)
    assert not reopening.done()                    # waits for the old page's injection
    rig.screen.inject_gate.set()
    await reopening
    assert ("injected", "tap", None) in rig.log
    rig.screen.inject_gate = None


async def test_replacement_viewer_gets_the_current_state(rig, operator):
    """r3 Astra S2: a new viewer page gets the current field state at once, even when nothing
    changed since the old page's last message."""
    run_id = await waiting_run(rig)
    viewer = await operator.open_view(run_id)
    ctl = await operator.ws(run_id, "ctl", viewer)
    msg = await ctl.receive_json(timeout=2)
    while msg.get("t") != "state":
        msg = await ctl.receive_json(timeout=2)
    status, html, _ = await operator.get(f"/view/{run_id}")          # a reload: a new page
    newer = re.search(r'data-viewer="([^"]+)"', html).group(1)
    again = await operator.ws(run_id, "ctl", newer)
    msg = await again.receive_json(timeout=2)
    while msg.get("t") == "ping":
        msg = await again.receive_json(timeout=2)
    assert msg["t"] == "state"


class _Frame:
    """A Patchright frame for ``ChromeScreen.probe``: its probe result, its element's box and
    whether that element is its parent's focused element."""

    def __init__(self, result, box=None, fail=False, active=False, parent=None):
        self.result, self.box, self.fail, self.active = result, box, fail, active
        self.parent_frame = parent

    async def evaluate(self, js, *a):
        if self.fail:
            raise RuntimeError("detached")
        return self.result

    async def frame_element(self):
        frame = self

        class El:
            async def bounding_box(self):
                return frame.box

            async def evaluate(self, js, *a):
                assert js == lv.IS_ACTIVE
                return frame.active
        return El()


class _Page:
    url = "https://a.example/"

    def __init__(self, main, frames):
        self.main_frame = _Frame(main)
        for f in [self.main_frame, *frames]:
            f.parent_frame = f.parent_frame if f is not self.main_frame else None
            if f is not self.main_frame:
                f.parent_frame = f.parent_frame or self.main_frame
            f.page = self
        self.frames = [self.main_frame] + frames
        self._main = main
        self.fronted = 0

    def is_closed(self):
        return False

    async def bring_to_front(self):
        self.fronted += 1

    async def evaluate(self, js, *a):
        return self._main


async def _probe(main, frames):
    page = _Page(main, frames)
    bs = type("BS", (), {"context": type("C", (), {"pages": [page]})()})()
    return await lv.ChromeScreen().probe(bs)


MAIN = {"fields": [], "iw": 500, "ih": 800, "sx": 0, "sy": 0, "ow": 500, "oh": 880,
        "doc": 1, "host": "a", "title": "t", "frame": False, "pw": False}


async def test_probe_looks_into_hidden_frames():
    """r3 Astra S1: a password field in a hidden frame (no box) still marks the page; a frame
    that cannot be looked into leaves the probe unchecked."""
    hidden = _Frame(dict(MAIN, pw=True), box=None)
    for frames, password, unchecked in (([hidden], True, False),
                                         ([_Frame(MAIN, fail=True)], False, True)):
        out = await _probe(MAIN, frames)
        assert (out["password"], out["unchecked"]) == (password, unchecked)


async def test_only_the_focused_frame_has_a_focused_field():
    """Option C (Astra d1): two sibling frames each keep a focused element of their own; only
    the one whose element holds the top document's focus reports a focused field — the box's
    label and the guard follow that one. Positive control: with the top's focus not in a frame,
    neither does."""
    def field(ident, lb):
        return {"id": ident, "x": 1, "y": 1, "w": 100, "h": 20, "type": "text", "im": "", "ac": "",
                "ml": 0, "focused": True, "len": 0, "lb": lb, "val": ""}
    top = dict(MAIN, frame=True)

    def frames(nested=False):
        a = _Frame(dict(MAIN, doc=2, fields=[field(1, "Username")]), box={"x": 0, "y": 0})
        b = _Frame(dict(MAIN, doc=3, fields=[field(1, "Password")]), box={"x": 0, "y": 100},
                   active=True)
        inner = [_Frame(dict(MAIN, doc=4, fields=[field(1, "Code")]), box={"x": 0, "y": 0},
                        active=True, parent=a)] if nested else []
        return [a, b, *inner]
    out = await _probe(top, frames())
    assert [(f["lb"], f["focused"]) for f in out["fields"]] == [("Username", False), ("Password", True)]
    out = await _probe(MAIN, frames())                      # the top's focus is no frame
    assert [f["focused"] for f in out["fields"]] == [False, False]
    # nested: a frame inside an unfocused frame is not focused, however its own element says
    out = await _probe(top, frames(nested=True))
    assert [(f["lb"], f["focused"]) for f in out["fields"]] == \
        [("Username", False), ("Password", True), ("Code", False)]


async def test_guard_follows_the_focus_chain():
    """The guard's frame chain: a field in a frame counts as Chrome's focus only while every
    frame on the way down is its parent's focused element — whatever the frame's own document
    says (real Chrome clears it when a frame loses the focus; the chain does not rely on that).
    Positive control: the same field with its frame active passes."""
    class Frame(_Frame):
        def __init__(self, active, parent=None, focused=True):
            super().__init__(None, active=active, parent=parent)
            self.focused = focused

        def is_detached(self):
            return False

        async def evaluate(self, js, *a):
            assert js == lv.IS_FOCUSED
            return self.focused
    for active, expected in ((True, True), (False, False)):
        page = _Page(MAIN, [])
        frame = Frame(active, parent=page.main_frame)
        frame.page = page
        bs = type("BS", (), {"context": type("C", (), {"pages": [page]})()})()
        screen = lv.ChromeScreen()
        screen._docs = {(1, 9): (frame, 0, 0)}
        assert await screen.focused_is(bs, {"fi": 1, "doc": 9, "id": 3}) is expected
        assert page.fronted == 1                     # its page got Chrome's keys before the check
        assert await screen.focused_is(bs, {"fi": 1, "doc": 8, "id": 3}) is False   # another document
        # r1 (Astra): a newer page now shown — the field's page is not where the keys would go
        newer = _Page(MAIN, [])
        bs.context.pages.append(newer)
        assert await screen.focused_is(bs, {"fi": 1, "doc": 9, "id": 3}) is False
        assert newer.fronted == 0


def test_reveal_message_is_accepted_and_carries_nothing():
    """casa-test F6: the viewer asks the controller to bring the focused remote field into
    sight; the message carries no coordinates or text, and the page reads nothing back."""
    from casa_browser.liveview import REVEAL, parse_input
    state = {"win": [0, 0, 500, 800]}
    assert parse_input({"t": "reveal"}, state) == {"t": "reveal"}
    assert parse_input({"t": "reveal", "x": 5, "s": "secret"}, state) == {"t": "reveal"}
    assert parse_input({"t": "reveal"}, None) is None
    # it scrolls and tells only whether the focus is a frame (to follow it there)
    assert "scrollIntoView" in REVEAL and "value" not in REVEAL and "textContent" not in REVEAL


def test_parse_input_plain_keys():
    """Live view K, plain keyboard: the phone's delete keys are keys (Backspace, Ctrl+Backspace,
    Delete), bound to the field like text; a text is never empty, and the measured correction
    (`del`) and the counted backspaces (`back`) of earlier revisions are gone."""
    state = {"win": [0, 0, 500, 800]}
    for k, xdo in (("Backspace", "BackSpace"), ("CtrlBackspace", "ctrl+BackSpace"), ("Delete", "Delete"),
                   ("Left", "Left"), ("Right", "Right")):        # the strip's ← → (revision 7)
        assert parse_input({"t": "key", "k": k, "to": TO}, state) == {"t": "key", "k": k, "to": TO}
        assert lv.KEYS[k] == xdo
    assert parse_input({"t": "text", "s": "", "to": TO}, state) is None
    assert parse_input({"t": "text", "s": "x", "del": 3, "back": 2, "to": TO}, state) == \
        {"t": "text", "s": "x", "to": TO}
    assert not hasattr(lv, "FIELD_LEN") and not hasattr(lv, "CorrectionFailed")
