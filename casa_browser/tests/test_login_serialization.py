"""Session operations drain before cleanup removes their network authority."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brokerfakes import canary
from casa_browser import login
from casa_browser.broker import BrokerError
from test_login import driver

pytestmark = pytest.mark.asyncio


async def action_driver(tmp_path, op):
    server, manager, browser, page, proxy = driver(tmp_path)
    origin = 'https://login.example'
    sid = await server.open_session('login', 'run', 'site', browser, proxy, (origin,))
    frame = SimpleNamespace(evaluate=AsyncMock(return_value=origin))
    element = SimpleNamespace(evaluate=AsyncMock(return_value=True),
                              is_visible=AsyncMock(return_value=True),
                              is_enabled=AsyncMock(return_value=True))
    s = server._sessions[sid]
    s.handles['handle'] = (element, frame, 'password', origin, 7,
                           'password' if op == 'fill' else 'submit')
    proxy.arm_login_close = AsyncMock()
    request = dict(op=op, driver_session=sid, page_generation=7)
    if op == 'fill':
        request.update(field_handle='handle', value=canary())
    else:
        request.update(submit_handle='handle')
    return server, manager, browser, page, proxy, sid, element, request


@pytest.mark.parametrize('op', ['fill', 'submit'])
async def test_close_drains_final_page_action(tmp_path, op):
    server, _, browser, _, proxy, sid, element, request = await action_driver(tmp_path, op)
    entered, release = asyncio.Event(), asyncio.Event()

    async def evaluate(*args):
        entered.set()
        await release.wait()
        assert not any(call[0] == 'end_login' for call in proxy.calls)
        proxy.calls.append(('page_effect',))
        return True

    element.evaluate.side_effect = evaluate
    action = asyncio.create_task(server.dispatch(request))
    await asyncio.wait_for(entered.wait(), 1)
    cleanup = asyncio.create_task(server.close_session(sid))
    try:
        done, _ = await asyncio.wait((cleanup,), timeout=0.02)
        assert not done
        assert browser.login_active
    finally:
        release.set()
    assert await action == {'ok': True}
    await asyncio.wait_for(cleanup, 1)
    names = [call[0] for call in proxy.calls]
    assert names.index('page_effect') < names.index('end_login') < names.index('close_window')
    assert not browser.login_active


@pytest.mark.parametrize('op', ['fill', 'submit'])
async def test_action_after_close_has_no_page_effect(tmp_path, op):
    server, _, _, _, _, sid, element, request = await action_driver(tmp_path, op)
    await server.close_session(sid)
    assert await server.dispatch(request) == {'error': 'refused'}
    element.is_visible.assert_not_called()
    element.evaluate.assert_not_called()


@pytest.mark.parametrize('op', ['fill', 'submit'])
@pytest.mark.parametrize('change', ['closed', 'kind', 'page', 'generation', 'lease'])
async def test_queued_action_revalidates_after_lock(tmp_path, op, change):
    server, _, browser, page, _, sid, element, request = await action_driver(tmp_path, op)
    s = server._sessions[sid]
    await s.lock.acquire()
    action = asyncio.create_task(server.dispatch(request))
    # An event-loop turn places the action at the admission boundary.
    await asyncio.wait((action,), timeout=0.01)
    assert not action.done()
    if change == 'closed':
        s.closed = True
    elif change == 'kind':
        s.kind = 'guest'
    elif change == 'page':
        page.closed = True
    elif change == 'generation':
        browser.view_generation += 1
    else:
        s.lease = None
    s.lock.release()
    # Slice 5u: a page that only moved is told apart (the broker reads it again); nothing acts.
    expected = 'page_changed' if change == 'generation' else 'refused'
    assert await asyncio.wait_for(action, 1) == {'error': expected}
    element.is_visible.assert_not_called()
    element.evaluate.assert_not_called()
    await server.close_session(sid)


@pytest.mark.parametrize('op', ['fill', 'submit'])
@pytest.mark.parametrize('suppress_cancel', [False, True])
async def test_hung_action_revokes_within_cleanup_bound(tmp_path, monkeypatch, op, suppress_cancel):
    server, _, browser, _, proxy, sid, element, request = await action_driver(tmp_path, op)
    monkeypatch.setattr(login, '_CLEANUP_TIMEOUT', 0.02)
    entered, release = asyncio.Event(), asyncio.Event()

    async def evaluate(*args):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            if not suppress_cancel:
                raise
            await release.wait()
        return True

    element.evaluate.side_effect = evaluate
    action = asyncio.create_task(server.dispatch(request))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await asyncio.wait_for(server.close_session(sid), 0.5)
        assert ('revoke', 'run') in proxy.calls
        assert sid not in server._sessions and not browser.login_active
        # No page cleanup or window close overlaps the stuck action.
        assert not any(call[0] == 'close_window' for call in proxy.calls)
    finally:
        release.set()
    result = await asyncio.gather(action, return_exceptions=True)
    if suppress_cancel:
        assert result == [{'error': 'refused'}]
    else:
        assert isinstance(result[0], asyncio.CancelledError)
    assert element.evaluate.await_count == 1


@pytest.mark.parametrize('op,kind,args', [
    ('settle', 'login', (1,)), ('find_login_fields', 'login', (7,)),
    ('choose_passkey', 'login', (7, 't', 'u')), ('replace_login_lease', 'login', ()),
    ('guest_state', 'guest', ()), ('guest_sign_in', 'guest', ('my.1password.eu', '', '', '')),
    ('guest_unlock', 'guest', ('',)), ('begin_live_capture', 'capture', ()),
    ('take_capture', 'capture', ('capture',)),
])
async def test_all_direct_operations_share_cleanup_lock(tmp_path, op, kind, args):
    server, _, browser, page, proxy = driver(tmp_path)
    sid = await server.open_session(kind, 'run', 'site', browser, proxy, ())
    s = server._sessions[sid]
    await s.lock.acquire()
    operation = asyncio.create_task(getattr(server, op)(sid, *args))
    await asyncio.wait((operation,), timeout=0.01)
    assert not operation.done()
    # Simulate cleanup winning admission before the queued operation validates.
    s.closed = True
    s.lock.release()
    with pytest.raises(BrokerError, match='^refused$'):
        await asyncio.wait_for(operation, 1)
    assert page.calls == []
    await server.close_session(sid)


@pytest.mark.parametrize('op', ['fill', 'submit'])
async def test_hung_validation_cannot_continue_to_page_action(tmp_path, monkeypatch, op):
    server, _, _, _, proxy, sid, element, request = await action_driver(tmp_path, op)
    monkeypatch.setattr(login, '_CLEANUP_TIMEOUT', 0.02)
    entered, release = asyncio.Event(), asyncio.Event()

    async def visible():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return True

    element.is_visible.side_effect = visible
    action = asyncio.create_task(server.dispatch(request))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await asyncio.wait_for(server.close_session(sid), 0.5)
        assert ('revoke', 'run') in proxy.calls
    finally:
        release.set()
    assert await asyncio.wait_for(action, 1) == {'error': 'refused'}
    element.is_enabled.assert_not_called()
    element.evaluate.assert_not_called()


async def capture_driver(tmp_path):
    from unittest.mock import Mock
    server, _, browser, page, proxy = driver(tmp_path)
    origin = 'https://login.example'
    frame = SimpleNamespace(evaluate=AsyncMock())
    page.frames = [frame]
    page.on = Mock()
    page.remove_listener = Mock()
    browser.context.on = Mock()
    browser.context.remove_listener = Mock()
    browser.context.expose_binding = AsyncMock(return_value=SimpleNamespace(dispose=AsyncMock()))
    proxy.status = AsyncMock(return_value=dict(run_id='run', site_id='site', site_origins=[origin]))
    sid = await server.open_session('capture', 'run', 'site', browser, proxy, (origin,))

    async def evaluate(script, arg=None):
        return origin if script == '() => location.origin' else None

    frame.evaluate.side_effect = evaluate
    capture = await server.begin_live_capture(sid)
    return server, browser, page, frame, sid, capture['capture_id']


async def test_capture_final_delivery_does_not_deadlock_session_lock(tmp_path):
    server, browser, _, frame, sid, capture_id = await capture_driver(tmp_path)
    receive = browser.context.expose_binding.call_args.args[1]
    install = next(call for call in frame.evaluate.call_args_list if call.args[0] == login._CAPTURE_INSTALL)
    nonce, origin = install.args[1]['nonce'], install.args[1]['origin']
    value = canary()

    async def evaluate(script, arg=None):
        if script == '() => location.origin':
            return origin
        if script.startswith('async key'):
            # The final isolated-world read waits for this callback's acknowledgement.
            await receive({}, nonce, origin, 'password', value)

    frame.evaluate.side_effect = evaluate
    result = await asyncio.wait_for(server.take_capture(sid, capture_id), 1)
    assert result == dict(values=[dict(role='password', value=value)], incomplete=False,
                          attempt=None)
    assert sid not in server._sessions


@pytest.mark.parametrize('event', ['framenavigated', 'page'])
async def test_capture_background_installation_drains_before_close(tmp_path, event):
    from unittest.mock import Mock
    server, browser, page, frame, sid, _ = await capture_driver(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    installed = 0

    async def evaluate(script, arg=None):
        nonlocal installed
        if script == '() => location.origin':
            return 'https://login.example'
        if script == login._CAPTURE_INSTALL:
            entered.set()
            await release.wait()
            installed += 1

    frame.evaluate.side_effect = evaluate
    if event == 'framenavigated':
        page.on.call_args.args[1](frame)
    else:
        new_page = SimpleNamespace(frames=[frame], on=Mock(), remove_listener=Mock())
        browser.context.on.call_args.args[1](new_page)
    await asyncio.wait_for(entered.wait(), 1)
    cleanup = asyncio.create_task(server.close_session(sid))
    try:
        done, _ = await asyncio.wait((cleanup,), timeout=0.02)
        assert not done and installed == 0
    finally:
        release.set()
    await asyncio.wait_for(cleanup, 1)
    assert installed == 1 and sid not in server._sessions


async def test_hung_capture_collection_cannot_read_another_frame(tmp_path, monkeypatch):
    server, _, page, frame, sid, capture_id = await capture_driver(tmp_path)
    monkeypatch.setattr(login, '_CLEANUP_TIMEOUT', 0.02)
    monkeypatch.setattr(login, '_CAPTURE_YIELD_S', 0.02)   # an op that will not be cancelled
    entered, release = asyncio.Event(), asyncio.Event()
    other = SimpleNamespace(evaluate=AsyncMock())
    page.frames.append(other)

    async def evaluate(script, arg=None):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    frame.evaluate.side_effect = evaluate
    action = asyncio.create_task(server.dispatch(dict(
        op='take_capture', driver_session=sid, capture_id=capture_id)))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await asyncio.wait_for(server.close_session(sid), 0.5)
    finally:
        release.set()
    # Told to stop before the forced close too, so this op, ignoring that, is cancelled twice.
    reply = (await asyncio.wait_for(asyncio.gather(action, return_exceptions=True), 1))[0]
    assert reply == {'error': 'refused'} or isinstance(reply, asyncio.CancelledError)
    other.evaluate.assert_not_called()


async def test_hung_cleanup_collection_has_a_hard_bound(tmp_path, monkeypatch):
    server, _, page, frame, sid, _ = await capture_driver(tmp_path)
    proxy = server._sessions[sid].proxy
    monkeypatch.setattr(login, '_CLEANUP_TIMEOUT', 0.02)
    entered, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    other = SimpleNamespace(evaluate=AsyncMock())
    page.frames.append(other)

    async def evaluate(script, arg=None):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        returned.set()

    frame.evaluate.side_effect = evaluate
    cleanup = asyncio.create_task(server.close_session(sid))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await asyncio.wait_for(cleanup, 0.5)
        assert ('revoke', 'run') in proxy.calls
        assert sid not in server._sessions
    finally:
        release.set()
    await asyncio.wait_for(returned.wait(), 1)
    other.evaluate.assert_not_called()


@pytest.mark.parametrize('op', ['fill', 'submit'])
async def test_forced_close_confirms_browser_exit_before_revoke(tmp_path, op):
    server, manager, browser, _, proxy, sid, element, request = await action_driver(tmp_path, op)
    server.cleanup_timeout = 0.02
    entered, killed, confirm, release = (asyncio.Event() for _ in range(4))

    async def evaluate(*args):
        entered.set()
        await release.wait()

    async def close(*, session, force):
        assert session is browser and force
        proxy.calls.append(('browser_close',))
        killed.set()
        await confirm.wait()
        proxy.calls.append(('browser_exited',))

    manager.close = close
    element.evaluate.side_effect = evaluate
    action = asyncio.create_task(server.dispatch(request))
    await asyncio.wait_for(entered.wait(), 1)
    cleanup = asyncio.create_task(server.close_session(sid))
    try:
        await asyncio.wait_for(killed.wait(), 1)
        assert browser.login_active
        assert [c[0] for c in proxy.calls] == ['begin_login', 'browser_close']
        confirm.set()
        await asyncio.wait_for(cleanup, 1)
        assert [c[0] for c in proxy.calls] == [
            'begin_login', 'browser_close', 'browser_exited', 'revoke']
        assert not browser.login_active and sid not in server._sessions
    finally:
        confirm.set()
        release.set()
        await asyncio.gather(action, cleanup, return_exceptions=True)


@pytest.mark.parametrize('failure', ['error', 'timeout', 'ignores_cancel'])
async def test_unconfirmed_browser_exit_keeps_login_guard_and_window(tmp_path, failure):
    server, manager, browser, _, proxy, sid, element, request = await action_driver(tmp_path, 'fill')
    server.cleanup_timeout = 0.02
    server.browser_close_timeout = 0.02
    entered, release = asyncio.Event(), asyncio.Event()

    async def evaluate(*args):
        entered.set()
        await release.wait()

    async def close(**kwargs):
        if failure == 'error':
            raise RuntimeError('unavailable')
        try:
            await release.wait()
        except asyncio.CancelledError:
            if failure != 'ignores_cancel':
                raise
            await release.wait()

    manager.close = close
    element.evaluate.side_effect = evaluate
    action = asyncio.create_task(server.dispatch(request))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        with pytest.raises(BrokerError, match='^unavailable$'):
            await asyncio.wait_for(server.close_session(sid), 0.5)
        assert browser.login_active
        assert sid in server._sessions
        assert not server._sessions[sid].closed
        assert [c[0] for c in proxy.calls] == ['begin_login']
        assert await server.dispatch(request) == {'error': 'refused'}
        async def confirmed_close(**kwargs):
            proxy.calls.append(('browser_exited',))
        manager.close = confirmed_close
        await asyncio.wait_for(server.close_session(sid), 0.5)
        assert [c[0] for c in proxy.calls] == ['begin_login', 'browser_exited', 'revoke']
        assert not browser.login_active and sid not in server._sessions
    finally:
        release.set()
        await asyncio.gather(action, return_exceptions=True)


@pytest.mark.parametrize('confirmed', [True, False])
async def test_stuck_cleanup_requires_browser_exit(tmp_path, confirmed):
    server, manager, browser, _, proxy = driver(tmp_path)
    server.cleanup_timeout = 0.02
    entered, release = asyncio.Event(), asyncio.Event()
    sid = await server.open_session('guest', 'run', 'site', browser, proxy, ())
    s = server._sessions[sid]

    async def page_close():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    async def browser_close(**kwargs):
        proxy.calls.append(('browser_close',))
        if not confirmed:
            raise RuntimeError('unavailable')
        proxy.calls.append(('browser_exited',))

    s.guest_page = SimpleNamespace(is_closed=lambda: False, close=page_close)
    manager.close = browser_close
    try:
        if confirmed:
            await asyncio.wait_for(server.close_session(sid), 0.5)
            assert proxy.calls == [('browser_close',), ('browser_exited',), ('revoke', 'run')]
            assert not browser.login_active and sid not in server._sessions
        else:
            with pytest.raises(BrokerError, match='^unavailable$'):
                await asyncio.wait_for(server.close_session(sid), 0.5)
            assert browser.login_active and not s.closed
            assert proxy.calls == [('browser_close',)]
    finally:
        release.set()
    # Let cancellation-resistant cleanup finish; it must not revoke behind the guard.
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0)
    assert [c[0] for c in proxy.calls].count('revoke') == int(confirmed)


async def test_aborted_lease_replacement_cannot_revoke_before_browser_exit(tmp_path):
    server, manager, browser, _, proxy = driver(tmp_path)
    server.cleanup_timeout = 0.02
    sid = await server.open_session('login', 'run', 'site', browser, proxy, ())
    entered, release, closing, confirm = (asyncio.Event() for _ in range(4))

    async def begin_login(lease):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return ('boot', 2)

    async def close(**kwargs):
        closing.set()
        await confirm.wait()
        proxy.calls.append(('browser_exited',))

    proxy.begin_login = begin_login
    manager.close = close
    operation = asyncio.create_task(server.replace_login_lease(sid))
    await asyncio.wait_for(entered.wait(), 1)
    cleanup = asyncio.create_task(server.close_session(sid))
    try:
        await asyncio.wait_for(closing.wait(), 1)
        release.set()
        with pytest.raises(BrokerError, match='^refused$'):
            await operation
        assert [c[0] for c in proxy.calls] == ['begin_login']
        confirm.set()
        await asyncio.wait_for(cleanup, 1)
        assert [c[0] for c in proxy.calls] == ['begin_login', 'browser_exited', 'revoke']
    finally:
        release.set()
        confirm.set()
        await asyncio.gather(operation, cleanup, return_exceptions=True)


@pytest.mark.parametrize('after', ['browser_gone', 'other_browser'])
async def test_unconfirmed_session_retired_after_launcher_proof(tmp_path, after):
    """A session kept after an unconfirmed forced close is retired once its browser is gone
    from the manager, on the launcher's exit proof alone (Task 12 teardown); while another
    browser runs it stays, with its guard and lease (fail closed)."""
    server, manager, browser, _, proxy, sid, element, request = await action_driver(tmp_path, 'fill')
    server.cleanup_timeout = 0.02
    server.browser_close_timeout = 0.02
    entered, release = asyncio.Event(), asyncio.Event()

    async def evaluate(*args):
        entered.set()
        await release.wait()

    async def unconfirmed(**kwargs):
        raise RuntimeError('unavailable')

    manager.close = unconfirmed
    element.evaluate.side_effect = evaluate
    action = asyncio.create_task(server.dispatch(request))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        with pytest.raises(BrokerError, match='^unavailable$'):
            await asyncio.wait_for(server.close_session(sid), 0.5)
        closes = []

        async def proof(**kwargs):
            closes.append(kwargs)
            proxy.calls.append(('browser_exited',))
        manager.close = proof
        manager.session = None if after == 'browser_gone' else SimpleNamespace(generation=9)
        if after == 'browser_gone':
            await asyncio.wait_for(server.close_session(sid), 0.5)
            assert closes == [{'force': True}]  # the launcher's proof, no session named
            assert [c[0] for c in proxy.calls] == ['begin_login', 'browser_exited', 'revoke']
            assert sid not in server._sessions and not browser.login_active
        else:
            with pytest.raises(BrokerError, match='^unavailable$'):
                await asyncio.wait_for(server.close_session(sid), 0.5)
            assert closes == [] and sid in server._sessions and browser.login_active
            assert [c[0] for c in proxy.calls] == ['begin_login']
    finally:
        release.set()
        await asyncio.gather(action, return_exceptions=True)


async def test_take_hands_the_attempt_to_the_broker_and_the_pages_to_the_controller(tmp_path):
    """Slice 5: the take returns the last attempt (values and flags) to the broker, and keeps
    for the controller the pages read after it plus the page at the take, once; no value
    reaches the controller's copy."""
    server, browser, page, frame, sid, capture_id = await capture_driver(tmp_path)
    receive = browser.context.expose_binding.call_args.args[1]
    install = next(call for call in frame.evaluate.call_args_list
                   if call.args[0] == login._CAPTURE_INSTALL)
    nonce, origin = install.args[1]['nonce'], install.args[1]['origin']
    user, password = 'alice-' + canary()[8:14], canary()
    seen = []

    async def reader(p):
        seen.append(p)
        return {'title': 'Account', 'blocks': ['Log out'], 'controls': [],
                'password_field': False, '_origin': 'https://a.example'}
    server._sessions[sid].reader = reader
    await receive({}, nonce, origin, 'username', user, 'd1:1')
    await receive({}, nonce, origin, 'attempt',
                  json.dumps({'username': user, 'password': password}), 'd1')
    result = await asyncio.wait_for(server.take_capture(sid, capture_id), 2)
    assert result['attempt'] == {'username': user, 'password': password, 'failed': False,
                                 'typed_after': False}
    got = server.take_readings(capture_id)
    assert got['at_done']['title'] == 'Account' and seen == [page]
    assert user not in json.dumps(got) and password not in json.dumps(got)
    assert server.take_readings(capture_id) == {}


# -- opening a live view: a failed open leaves Chrome running ---------------------------------

def _never_answers(origin, hung, entered=None):
    async def evaluate(script, arg=None):
        if entered is not None:
            entered.set()
        if hung():
            await asyncio.Event().wait()
        return origin if script == '() => location.origin' else None
    return evaluate


async def _open_capture(tmp_path, frames):
    from unittest.mock import Mock
    server, manager, browser, page, proxy = driver(tmp_path)
    page.frames = frames
    page.on, page.remove_listener = Mock(), Mock()
    browser.context.on, browser.context.remove_listener = Mock(), Mock()
    browser.context.expose_binding = AsyncMock(return_value=SimpleNamespace(dispose=AsyncMock()))
    proxy.status = AsyncMock(return_value=dict(run_id='run', site_id='site',
                                               site_origins=['https://login.example']))
    manager.close = AsyncMock()
    sid = await server.open_session('capture', 'run', 'site', browser, proxy,
                                    ('https://login.example',))
    return server, manager, browser, sid


async def test_a_frame_elsewhere_is_left_alone_by_its_address_at_open_and_at_stop(tmp_path):
    """A frame whose address is off the capture origins, and an extension's, are never asked
    anything: 0 evaluations, the view opens, the close is a normal one."""
    here = SimpleNamespace(url='https://login.example/signin', evaluate=AsyncMock(
        side_effect=_never_answers('https://login.example', lambda: False)))
    widget = SimpleNamespace(url='https://challenges.cloudflare.com/turnstile?k=1',
                             evaluate=AsyncMock())
    extension = SimpleNamespace(url='chrome-extension://abc/inline/x.html', evaluate=AsyncMock())
    server, manager, _, sid = await _open_capture(tmp_path, [here, widget, extension])
    assert 'capture_id' in await server.dispatch(dict(op='begin_live_capture', driver_session=sid))
    await server.close_session(sid)
    assert widget.evaluate.await_count == 0 and extension.evaluate.await_count == 0
    assert here.evaluate.await_count >= 2 and sid not in server._sessions
    assert manager.close.await_count == 0


async def test_a_frame_with_no_web_address_is_still_asked(tmp_path):
    """about:blank can inherit a capture origin: the reader goes in."""
    blank = SimpleNamespace(url='about:blank', evaluate=AsyncMock(
        side_effect=_never_answers('https://login.example', lambda: False)))
    server, _, _, sid = await _open_capture(tmp_path, [blank])
    await server.dispatch(dict(op='begin_live_capture', driver_session=sid))
    assert login._CAPTURE_INSTALL in [c.args[0] for c in blank.evaluate.call_args_list]
    await server.close_session(sid)


async def test_a_frame_that_never_answers_marks_the_capture_and_the_view_opens(tmp_path, monkeypatch):
    """The stall is the frame's: the capture says it is incomplete, the other frames take the
    reader, the view opens, and the close (its stop bound too) does not close Chrome."""
    monkeypatch.setattr(login, 'CAPTURE_FRAME_S', 0.02)
    monkeypatch.setattr(login, 'CAPTURE_STOP_S', 0.1)
    stuck = SimpleNamespace(url='about:blank', evaluate=AsyncMock(
        side_effect=_never_answers('https://login.example', lambda: True)))
    next_one = SimpleNamespace(url='https://login.example/a', evaluate=AsyncMock(
        side_effect=_never_answers('https://login.example', lambda: False)))
    server, manager, _, sid = await _open_capture(tmp_path, [stuck, next_one])
    reply = await server.dispatch(dict(op='begin_live_capture', driver_session=sid))
    assert 'capture_id' in reply, reply
    s = server._sessions[sid]
    assert s.buffer.take().incomplete is True
    assert login._CAPTURE_INSTALL in [c.args[0] for c in next_one.evaluate.call_args_list]
    await asyncio.wait_for(server.close_session(sid), 1)
    assert manager.close.await_count == 0 and sid not in server._sessions


async def test_an_open_past_its_bound_answers_first_and_the_close_is_a_normal_one(tmp_path, monkeypatch):
    """The whole open past its bound: ``timeout`` from the driver, the session's lock free, and
    the close that follows does not close Chrome by force (the casa-test failure's SIGKILL)."""
    monkeypatch.setattr(login, '_CAPTURE_OPEN_S', 0.05)
    frame = SimpleNamespace(url='https://login.example/a', evaluate=AsyncMock(
        side_effect=_never_answers('https://login.example', lambda: False)))
    server, manager, browser, sid = await _open_capture(tmp_path, [frame])

    async def stalls(*_a, **_k):
        await asyncio.Event().wait()
    browser.context.expose_binding = AsyncMock(side_effect=stalls)
    assert await asyncio.wait_for(
        server.dispatch(dict(op='begin_live_capture', driver_session=sid)), 2) == {'error': 'timeout'}
    assert not server._sessions[sid].lock.locked()
    await asyncio.wait_for(server.close_session(sid), 1)
    assert manager.close.await_count == 0 and sid not in server._sessions


@pytest.mark.parametrize('queued', [0, 8])
async def test_a_close_under_a_stuck_capture_stops_the_op_not_chrome(tmp_path, monkeypatch, queued):
    """A close while a capture op holds the session past the bound (also with queued reader
    work that would take the lock next): the op is cancelled, the session closes normally, no
    forced close."""
    from unittest.mock import Mock
    monkeypatch.setattr(login, '_CLEANUP_TIMEOUT', 0.2)
    monkeypatch.setattr(login, 'CAPTURE_FRAME_S', 30)
    monkeypatch.setattr(login, 'CAPTURE_STOP_S', 0.02)
    server, _, browser, page, proxy = driver(tmp_path)
    entered = asyncio.Event()
    frame = SimpleNamespace(url='https://login.example/a', evaluate=AsyncMock(
        side_effect=_never_answers('https://login.example', lambda: True, entered)))
    page.frames = [frame]
    page.on, page.remove_listener = Mock(), Mock()
    browser.context.on, browser.context.remove_listener = Mock(), Mock()
    browser.context.expose_binding = AsyncMock(return_value=SimpleNamespace(dispose=AsyncMock()))
    proxy.status = AsyncMock(return_value=dict(run_id='run', site_id='site',
                                               site_origins=['https://login.example']))
    server.manager.close = AsyncMock()
    sid = await server.open_session('capture', 'run', 'site', browser, proxy,
                                    ('https://login.example',))
    action = asyncio.create_task(server.dispatch(dict(op='begin_live_capture', driver_session=sid)))
    await asyncio.wait_for(entered.wait(), 1)
    for _ in range(queued):            # navigations seen while the open held the session
        page.on.call_args.args[1](frame)
    await asyncio.sleep(0)
    await asyncio.wait_for(server.close_session(sid), 2)
    assert server.manager.close.await_count == 0 and sid not in server._sessions
    action.cancel()
    await asyncio.gather(action, return_exceptions=True)


async def test_a_close_ends_the_readers_queued_work_and_none_starts(tmp_path):
    """A close with navigations queued behind the reader's work in hand: the one in hand
    finishes, the queued ones never reach the page (each would take the session in its turn,
    under the close), and a navigation seen after the close began starts nothing."""
    server, _, page, frame, sid, _ = await capture_driver(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    installs = 0

    async def evaluate(script, arg=None):
        nonlocal installs
        if script == '() => location.origin':
            return 'https://login.example'
        if script == login._CAPTURE_INSTALL:
            installs += 1
            entered.set()
            await release.wait()

    frame.evaluate.side_effect = evaluate
    schedule = page.on.call_args.args[1]
    s = server._sessions[sid]
    for _ in range(8):
        schedule(frame)
    await asyncio.wait_for(entered.wait(), 1)
    cleanup = asyncio.create_task(server.close_session(sid))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    in_flight = len(s.tasks)
    schedule(frame)
    assert len(s.tasks) <= in_flight
    release.set()
    await asyncio.wait_for(cleanup, 2)
    assert installs == 1 and sid not in server._sessions
