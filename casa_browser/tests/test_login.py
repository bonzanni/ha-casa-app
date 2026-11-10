"""Page-free driver ownership and provider-learning contracts."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from brokerfakes import Rig, canary
from casa_browser.browser import BrowserSession
from casa_browser.login import CaptureBuffer, DriverServer, learn_provider_binding
from casa_browser.policy import Lease
from casa_browser.protocol import Site
from casa_browser.proxy_client import PrivateRequestEvent, encode_policy, decode_policy
from casa_browser.store import Store
import proxyharness as h

pytestmark = pytest.mark.asyncio


class Page:
    """Only lifecycle checks use this double; no DOM behavior is simulated."""
    def __init__(self):
        self.closed = False
        self.calls = []
        self.listeners = {}

    def is_closed(self):
        return self.closed

    def on(self, event, callback):           # a login session's frame watch (slice 5u)
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def emit(self, event, frame):
        for callback in tuple(self.listeners.get(event, ())):
            callback(frame)

    async def bring_to_front(self):
        self.calls.append('bring_to_front')


class Proxy:
    def __init__(self):
        self.calls = []
        self.counter = 0
        self.failure = None

    async def begin_login(self, lease):
        self.counter += 1
        token = ('boot', self.counter)
        self.calls.append(('begin_login', lease, token))
        return token

    async def end_login(self, lease_id):
        self.calls.append(('end_login', lease_id))
        if self.failure:
            raise self.failure

    async def revoke(self, run_id):
        self.calls.append(('revoke', run_id))

    async def close_window(self, token):
        self.calls.append(('close_window', token))


def driver(tmp_path):
    page = Page()
    context = SimpleNamespace(pages=[page])
    session = BrowserSession('account', 'site', context, 4, 7)
    async def close(**kwargs):
        page.closed = True

    manager = SimpleNamespace(session=session, close=close)
    return DriverServer(manager, tmp_path / 'driver.sock', allowed_uid=os.getuid()), manager, session, page, Proxy()


async def driver_rpc(server, request):
    """Reach the production handler through a real Unix socket, including peer credentials."""
    listener = await asyncio.start_unix_server(server.handle, path=str(server.path), limit=64 * 1024)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.path))
        try:
            writer.write(json.dumps(request).encode() + b'\n')
            await writer.drain()
            return json.loads(await asyncio.wait_for(reader.readline(), 1))
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        listener.close()
        await listener.wait_closed()
        server.path.unlink(missing_ok=True)


def refusal_requests(sid, generation):
    """Opaque handles suffice for page-free refusal; valid DOM handles are image-tested."""
    return [
        {'op': 'fill', 'driver_session': sid, 'page_generation': generation,
         'field_handle': 'opaque-field', 'value': canary()},
        {'op': 'submit', 'driver_session': sid, 'page_generation': generation,
         'submit_handle': 'opaque-submit'},
        {'op': 'guest_state', 'driver_session': sid},
    ]


@pytest.mark.parametrize('case', ['wrong_session', 'closed_session', 'generation', 'manager_session'])
async def test_driver_session_binding(tmp_path, case):
    """Every op refuses invalid session ownership before inspecting a page or handle."""
    server, manager, session, page, proxy = driver(tmp_path)
    sid = await server.open_session('login', 'run', 'site', session, proxy, ('https://login.example',))
    if case == 'wrong_session':
        sid = 'wrong'
    elif case == 'closed_session':
        await server.close_session(sid)
    elif case == 'generation':
        session.generation += 1
    else:
        manager.session = BrowserSession('account', 'site', session.context, 5, 8)
    for request in refusal_requests(sid, session.view_generation):
        assert await driver_rpc(server, request) == {'error': 'refused'}
    assert page.calls == []


@pytest.mark.parametrize('case', ['closed', 'replaced'])
async def test_driver_session_bound_to_page(tmp_path, case):
    """A surviving same-origin tab never substitutes for the captured Page object."""
    server, _, session, page, proxy = driver(tmp_path)
    sid = await server.open_session('login', 'run', 'site', session, proxy, ('https://login.example',))
    other = Page()
    session.context.pages.append(other)
    if case == 'closed':
        page.closed = True
    else:
        session.context.pages[:] = [other, page]
    for request in refusal_requests(sid, session.view_generation):
        assert await driver_rpc(server, request) == {'error': 'refused'}
    assert page.calls == other.calls == []


@pytest.mark.parametrize('kind', ['login', 'guest'])
async def test_close_session_order(tmp_path, kind):
    """Lease acknowledgement precedes the owned window close; unlock-only owns no window."""
    server, _, session, page, proxy = driver(tmp_path)
    sid = await server.open_session(kind, 'run', 'site', session, proxy, ('https://login.example',))
    await server.close_session(sid)
    if kind == 'login':
        token = proxy.calls[0][2]
        lease = proxy.calls[0][1]
        assert proxy.calls == [('begin_login', lease, token), ('end_login', lease.lease_id), ('close_window', token)]
    else:
        assert proxy.calls == []
    assert session.login_active is False
    assert await driver_rpc(server, {'op': 'guest_state', 'driver_session': sid}) == {'error': 'refused'}
    assert page.calls == []


@pytest.mark.parametrize('finish', ['success', 'error', 'cancel', 'timeout'])
async def test_close_session_ends_the_current_lease(tmp_path, finish):
    """Cleanup ends the latest replacement lease and falls back to revoke on timeout."""
    server, _, session, _, proxy = driver(tmp_path)
    sid = await server.open_session('login', 'run', 'site', session, proxy, ('https://login.example',))
    # The session's replacement seam is page-free; real replacement-on-navigation is image-tested.
    await server.replace_login_lease(sid)
    current, token = proxy.calls[-1][1:]
    assert proxy.calls[0][1].lease_id != current.lease_id
    if finish == 'timeout':
        proxy.failure = TimeoutError()
    try:
        if finish == 'error':
            raise RuntimeError('unavailable')
        if finish == 'cancel':
            raise asyncio.CancelledError()
    except (RuntimeError, asyncio.CancelledError):
        pass
    finally:
        await server.close_session(sid)
    tail = [('end_login', current.lease_id)]
    if finish == 'timeout':
        tail.append(('revoke', 'run'))
    tail.append(('close_window', token))
    assert proxy.calls[-len(tail):] == tail
    assert session.login_active is False


@pytest.mark.parametrize('switch', ['run', 'site'])
async def test_site_origins_bound_to_driver_session(tmp_path, switch):
    """A policy switch cannot supply another run's or site's capture origins."""
    from casa_browser.broker import BrokerError
    from casa_browser.proxy_addon import EnforcingAddon
    from casa_browser.proxy_main import Proxy as ProxyServer
    server, _, session, _, proxy = driver(tmp_path)
    addon = EnforcingAddon()
    policy = h.account_policy(sites=(h.site(site_id='site'),))
    policy = replace(policy, run_id='run', active_session=replace(
        policy.active_session, site_id='site', generation=session.generation))
    addon.install(policy)
    control = ProxyServer(addon=addon, confdir=tmp_path, control_path=tmp_path / 'control',
                          controller_uid=os.getuid())

    async def status():
        return control.dispatch(b'{"op":"status"}')

    proxy.status = status
    sid = await server.open_session('capture', 'run', 'site', session, proxy,
                                    ('https://login.example',))
    try:
        assert await server._site_origins(server._sessions[sid]) == ('https://site.example',)
        if switch == 'run':
            policy = replace(policy, run_id='other')
        else:
            policy = replace(policy, active_session=replace(policy.active_session, site_id='other'),
                             sites=(replace(policy.sites[0], site_id='other',
                                            origins=('https://other.example',)),))
        addon.install(policy)
        with pytest.raises(BrokerError, match='^refused$'):
            await server._site_origins(server._sessions[sid])
    finally:
        await server.close_session(sid)


async def test_capture_overflow_keeps_latest():
    """The bounded capture retains the newest eight distinct values and reports loss."""
    buffer = CaptureBuffer()
    values = [canary() for _ in range(9)]
    for value in values:
        buffer.record('password', value)
    buffer.record('password', values[-1])
    buffer.record('password', '')
    result = buffer.take()
    assert [v.value for v in result.values] == values[-8:]
    assert result.incomplete is True
    assert buffer.take().values == ()


async def test_capture_keeps_each_fields_latest_value():
    """casa-test F3: typing reports every prefix; a field's latest value replaces its earlier
    ones, so one typed password is one candidate. Two fields keep one value each (equal values
    once), and more than eight fields drop the oldest and say so."""
    buffer = CaptureBuffer()
    password, user = canary(), 'someone@example.com'
    for n in range(1, len(password) + 1):
        buffer.record('password', password[:n], 'd1:2')
    for n in range(1, len(user) + 1):
        buffer.record('username', user[:n], 'd1:1')
    buffer.record('password', password, 'd2:1')          # the same value on the next page
    result = buffer.take()
    assert [(v.role, v.value) for v in result.values] == [('password', password), ('username', user)]
    assert result.incomplete is False
    for n in range(9):
        buffer.record('password', canary(), f'd3:{n}')
    assert buffer.take().incomplete is True


async def test_a_value_seen_as_a_password_is_never_a_username():
    """Operator ruling E-T14-3 = Q1: a value seen as a password anywhere in the capture is never a
    username candidate, whatever field carried it — the same field read as text later (a "show
    password" toggle), read in either order, or a text clone that replaced it (another field
    id). The real username still is one."""
    secret = canary()
    shapes = {
        "toggle_after": [('password', secret, 'd1:2'), ('username', secret, 'd1:2')],
        "toggle_before": [('username', secret, 'd1:2'), ('password', secret, 'd1:2')],
        "clone": [('password', secret, 'd1:2'), ('username', secret, 'd1:7')],
        "other_page": [('password', secret, 'd1:2'), ('username', secret, 'd2:1')],
    }
    for name, reads in shapes.items():
        buffer = CaptureBuffer()
        for role, value, field_id in reads:
            buffer.record(role, value, field_id)
        buffer.record('username', 'me@example.com', 'd1:1')
        result = buffer.take()
        assert sorted((v.role, v.value) for v in result.values) == [
            ('password', secret), ('username', 'me@example.com')], name


async def test_a_password_value_seen_once_stays_known_for_the_whole_capture():
    """r8 S1: a password value whose candidate was replaced (the field took another value) or
    dropped for room (more than eight fields) still never comes back as a username."""
    first, second = canary(), canary()
    replaced = CaptureBuffer()
    replaced.record('password', first, 'd1:2')
    replaced.record('plain_username', first, 'd1:9')     # a text clone carried it
    replaced.record('password', second, 'd1:2')          # the field changed afterwards
    replaced.record('username', 'me@example.com', 'd1:1')
    assert sorted((v.role, v.value) for v in replaced.take().values) == [
        ('password', second), ('username', 'me@example.com')]
    evicted = CaptureBuffer()
    evicted.record('password', first, 'd1:2')
    for n in range(9):
        evicted.record('password', canary(), f'd2:{n}')   # the first is dropped for room
    evicted.record('plain_username', first, 'd3:1')
    result = evicted.take()
    assert first not in [v.value for v in result.values if v.role == 'username'], result


async def test_seen_password_overflow_offers_no_username(monkeypatch):
    """r8/r9 S1, fail closed: once the seen-password set is full, no username is offered at all
    — not a plain text field's, not one the page marked as a username (either may hold a
    password no longer remembered) — and the capture says it is incomplete."""
    import casa_browser.login as login_mod
    monkeypatch.setattr(login_mod, 'MAX_SEEN_PASSWORDS', 3)
    buffer = CaptureBuffer()
    for n in range(4):
        buffer.record('password', canary(), f'd1:{n}')
    buffer.record('plain_username', 'plain-user', 'd2:1')
    buffer.record('username', 'marked@example.com', 'd2:2')
    result = buffer.take()
    users = [v.value for v in result.values if v.role == 'username']
    assert users == [] and result.incomplete is True
    assert len([v for v in result.values if v.role == 'password']) <= 8
    control = CaptureBuffer()                               # below the bound: offered
    control.record('password', canary(), 'd1:1')
    control.record('plain_username', 'plain-user', 'd2:1')
    assert 'plain-user' in [v.value for v in control.take().values]


async def test_a_password_field_counts_from_first_sight():
    """Operator 2026-10-23: a field reported as a password field stays one for the whole
    capture — a value it reports later as a (plain) username is a password value, offered as a
    password and never as a username, also when the password was typed only after the field
    was shown as text."""
    shown_later = canary()
    buffer = CaptureBuffer()
    buffer.record('password', 'x', 'd1:2')                 # first sight: a password field
    buffer.record('plain_username', shown_later, 'd1:2')   # shown before the real value
    buffer.record('username', 'me@example.com', 'd1:1')
    result = buffer.take()
    assert sorted((v.role, v.value) for v in result.values) == [
        ('password', shown_later), ('username', 'me@example.com')]


async def test_capture_canonicalises_new_password(tmp_path):
    """New and confirm roles become distinct password candidates saved to the password field."""
    buffer = CaptureBuffer()
    value, other = canary(), canary()
    for role, content in [('new_password', value), ('confirm_password', value), ('confirm_password', other)]:
        buffer.record(role, content)
    result = buffer.take()
    assert [(v.role, v.value) for v in result.values] == [('password', value), ('password', other)]
    from brokerfakes import attempt_of, item_value
    r = await Rig(tmp_path).setup()
    r.overwritable()
    capture = await r.capture([(v.role, v.value) for v in result.values],
                              attempt=attempt_of([('password', value)]))
    assert sorted(capture.candidates.values()) == sorted([('password', value), ('password', other)])
    receipt = await r.save()
    assert receipt['saved']
    assert item_value(r.sdk.saved['vault', 'item'], 'password') == value


def event(seq, binding, origin='https://login.example'):
    return PrivateRequestEvent(seq, 'run', 1, 'browser', True, 'manual_lease', 'request_allowed',
                               'GET', origin, 'document', True, 1.0, binding=binding)


@pytest.mark.parametrize('case', ['good', 'no_callback', 'two', 'wrong_callback', 'callback_before', 'not_signed_in', 'off_origin', 'callback_off_site'])
async def test_learn_provider_binding(tmp_path, case):
    """Only one tagged candidate followed by its callback on one of the site's own origins
    (r4 Astra, Terra) is learned."""
    import hashlib
    tags = {name: hashlib.sha256(canary().encode()).hexdigest() for name in ('path_tag', 'client_id_tag', 'redirect_uri_tag')}
    s = Site('site', 'Site', ('https://site.example',), ('https://login.example',), None, (), None)
    events = [event(1, {'candidate': 1, **tags}), event(2, {'callback_of': 1}, 'https://site.example')]
    outcome = 'signed_in'
    if case == 'no_callback':
        events.pop()
    elif case == 'two':
        events.insert(1, event(2, {'candidate': 2, **tags}))
    elif case == 'wrong_callback':
        events[-1] = event(2, {'callback_of': 2}, 'https://site.example')
    elif case == 'callback_before':
        events.reverse()
    elif case == 'not_signed_in':
        outcome = 'unknown'
    elif case == 'off_origin':
        events[0] = replace(events[0], origin='https://other.example')
    elif case == 'callback_off_site':                 # back on the provider, never the site
        events[1] = replace(events[1], origin='https://login.example')
    result = learn_provider_binding(events, s, outcome)
    if case != 'good':
        assert result is None
        return
    assert result.provider_origin == 'https://login.example'
    assert result.__dict__ == {'provider_origin': 'https://login.example', **tags}
    store = Store(tmp_path / 'state.db')
    try:
        store.put_site(replace(s, provider_bindings=(result,)))
        assert store.get_site('site').provider_bindings == (result,)
        row = store._db.execute('SELECT provider_bindings FROM sites').fetchone()[0]
        assert json.loads(row) == [result.__dict__]
        p = h.account_policy(sites=(replace(s, provider_bindings=(result,)),))
        assert decode_policy(encode_policy(p)).sites[0].provider_bindings == (result,)
    finally:
        store.close()


async def test_capture_binding_rejects_wrong_nonce(tmp_path):
    """Knowing the exposed binding name cannot forge isolated-world capture values."""
    from unittest.mock import AsyncMock, Mock
    from casa_browser.login import _CAPTURE_INSTALL

    server, _, session, page, proxy = driver(tmp_path)
    frame = SimpleNamespace(evaluate=AsyncMock(return_value='https://login.example'))
    page.frames = [frame]
    page.on = Mock()
    page.remove_listener = Mock()
    session.context.on = Mock()
    session.context.expose_binding = AsyncMock()
    proxy.status = AsyncMock(return_value={'run_id': 'run', 'site_id': 'site',
                                           'site_origins': ['https://site.example']})
    sid = await server.open_session('capture', 'run', 'site', session, proxy,
                                    ('https://login.example',))
    await server.begin_live_capture(sid)
    _, receive = session.context.expose_binding.call_args.args
    script, args = frame.evaluate.call_args.args
    assert script == _CAPTURE_INSTALL
    assert args['nonce']
    source = {'frame': frame}
    await receive(source, 'wrong', 'https://site.example', 'password', canary())
    assert server._sessions[sid].buffer.take().values == ()
    value = canary()
    await receive(source, args['nonce'], 'https://site.example', 'password', value)
    captured = server._sessions[sid].buffer.take()
    assert [(v.role, v.value) for v in captured.values] == [('password', value)]
    await server.close_session(sid)


async def test_capture_binding_survives_destroyed_context(tmp_path):
    """Authenticated capture delivery needs no further evaluation in the old document."""
    from unittest.mock import AsyncMock, Mock

    server, _, session, page, proxy = driver(tmp_path)
    origin = 'https://login.example'
    frame = SimpleNamespace(evaluate=AsyncMock(return_value=origin))
    page.frames = [frame]
    page.on = Mock()
    page.remove_listener = Mock()
    session.context.on = Mock()
    session.context.expose_binding = AsyncMock()
    proxy.status = AsyncMock(return_value={'run_id': 'run', 'site_id': 'site',
                                           'site_origins': ['https://site.example']})
    sid = await server.open_session('capture', 'run', 'site', session, proxy, (origin,))
    await server.begin_live_capture(sid)
    _, receive = session.context.expose_binding.call_args.args
    _, args = frame.evaluate.call_args.args
    frame.evaluate.side_effect = RuntimeError('Execution context was destroyed')
    value = canary()
    await receive({'frame': frame}, args['nonce'], origin, 'password', value)
    captured = server._sessions[sid].buffer.take()
    assert [(v.role, v.value) for v in captured.values] == [('password', value)]
    await receive({'frame': frame}, args['nonce'], 'https://other.example', 'password', value)
    await receive({'frame': frame}, 'wrong', origin, 'password', value)
    assert server._sessions[sid].buffer.take().values == ()
    await server.close_session(sid)


@pytest.mark.parametrize('op', ['settle', 'find_login_fields', '_on_site'])
@pytest.mark.parametrize('persistent', [False, True])
@pytest.mark.parametrize('gone', ['Execution context was destroyed, most likely because of a navigation',
                                  'Frame.evaluate: Frame was detached'])   # as Patchright says them
async def test_driver_reads_retry_navigation(tmp_path, op, persistent, gone):
    """A read the page moved under (its document destroyed, one of its frames gone) is retried;
    a page that keeps moving ends with a fixed timeout, never an error that ends the run."""
    from unittest.mock import AsyncMock
    from casa_browser.broker import BrokerError

    server, _, session, page, proxy = driver(tmp_path)
    origin = 'https://my.1password.eu'
    destroyed = RuntimeError(gone)
    frame = SimpleNamespace(url=origin + '/', evaluate=AsyncMock(
        side_effect=destroyed if persistent else [destroyed, origin, 'unlocked']),
        query_selector_all=AsyncMock(return_value=[]))
    page.frames = [frame]
    page.main_frame = frame
    page.wait_for_load_state = AsyncMock()
    proxy.status = AsyncMock(return_value={'run_id': 'run', 'site_id': 'site',
                                           'site_origins': [origin]})
    sid = await server.open_session('guest', 'run', 'site', session, proxy, (origin,))
    if op == 'settle':
        call = lambda: server.settle(sid, 2)
    elif op == 'find_login_fields':
        frame.evaluate.side_effect = destroyed if persistent else [destroyed, origin, origin]
        call = lambda: server.find_login_fields(sid, session.view_generation)
    else:
        call = lambda: server._on_site(server._sessions[sid], sid)
    try:
        if persistent:
            with pytest.raises(BrokerError, match='^timeout$'):
                await call()
            assert frame.evaluate.call_count == 3
        else:
            result = await call()
            if op == 'settle':
                assert result['on_site_origin'] is True
            elif op == 'find_login_fields':
                assert result['origin_ok'] is True
            else:
                assert result is True
        page.wait_for_load_state.assert_awaited()
    finally:
        await server.close_session(sid)


async def test_driver_retries_whole_document_read(tmp_path):
    """Navigation during a DOM read restarts discovery rather than reusing stale handles."""
    from unittest.mock import AsyncMock

    server, _, session, page, proxy = driver(tmp_path)
    origin = 'https://my.1password.eu'
    destroyed = RuntimeError('Execution context was destroyed')
    frame = SimpleNamespace(url=origin + '/', evaluate=AsyncMock(),
                            query_selector_all=AsyncMock(return_value=[]))
    page.frames = [frame]
    page.main_frame = frame
    page.wait_for_load_state = AsyncMock()
    proxy.status = AsyncMock(return_value={'run_id': 'run', 'site_id': 'site',
                                           'site_origins': [origin]})
    sid = await server.open_session('guest', 'run', 'site', session, proxy, (origin,))
    try:
        frame.evaluate.return_value = origin
        frame.query_selector_all.side_effect = [destroyed, []]
        result = await server.find_login_fields(sid, session.view_generation)
        assert result['origin_ok'] and result['roles'] == {}
        assert frame.query_selector_all.await_count == 2
        page.wait_for_load_state.assert_awaited_once_with('domcontentloaded', timeout=10000)
    finally:
        await server.close_session(sid)


async def test_navigation_load_failure_returns_fixed_code(tmp_path):
    """A load wait failure after document teardown cannot export browser exception text."""
    from unittest.mock import AsyncMock

    server, _, session, page, proxy = driver(tmp_path)
    page.frames = [SimpleNamespace(url='https://login.example/', evaluate=AsyncMock(
        side_effect=RuntimeError('Execution context was destroyed')))]
    page.wait_for_load_state = AsyncMock(side_effect=RuntimeError('Timeout exceeded'))
    sid = await server.open_session('login', 'run', 'site', session, proxy, ())
    try:
        assert await driver_rpc(server, {'op': 'find_login_fields', 'driver_session': sid,
                                         'page_generation': session.view_generation}) == \
            {'error': 'timeout'}
    finally:
        await server.close_session(sid)


async def test_navigation_read_deadline(tmp_path):
    """A load that never finishes is bounded by the settling caller's deadline."""
    from casa_browser.broker import BrokerError

    server, _, session, page, proxy = driver(tmp_path)
    pending = asyncio.Event()
    async def wait_for_load_state(*args, **kwargs):
        await pending.wait()
    page.wait_for_load_state = wait_for_load_state
    sid = await server.open_session('guest', 'run', 'site', session, proxy, ())
    try:
        with pytest.raises(BrokerError, match='^timeout$'):
            await server.settle(sid, 0.01)
    finally:
        await server.close_session(sid)


async def test_navigation_retry_rechecks_generation(tmp_path):
    """A generation change during load prevents the retry from reading another document."""
    from unittest.mock import AsyncMock
    from casa_browser.broker import BrokerError

    server, _, session, page, proxy = driver(tmp_path)
    page.frames = [SimpleNamespace(url='https://login.example/', evaluate=AsyncMock(
        side_effect=RuntimeError('Execution context was destroyed')))]
    async def wait_for_load_state(*args, **kwargs):
        session.generation += 1
    page.wait_for_load_state = wait_for_load_state
    sid = await server.open_session('guest', 'run', 'site', session, proxy, ())
    try:
        with pytest.raises(BrokerError, match='^refused$'):
            await server.find_login_fields(sid, session.view_generation)
        assert page.frames[0].evaluate.await_count == 1
    finally:
        await server.close_session(sid)


async def test_settle_load_failure_returns_fixed_code(tmp_path):
    """A browser load timeout is a fixed timeout even before the first origin read."""
    from unittest.mock import AsyncMock
    from casa_browser.broker import BrokerError

    server, _, session, page, proxy = driver(tmp_path)
    page.wait_for_load_state = AsyncMock(side_effect=RuntimeError('Timeout exceeded'))
    sid = await server.open_session('guest', 'run', 'site', session, proxy, ())
    try:
        with pytest.raises(BrokerError, match='^timeout$'):
            await server.settle(sid, 1)
    finally:
        await server.close_session(sid)


KIND_REQUESTS = [
    ('login', {'op': 'settle', 'timeout_s': 1}),
    ('login', {'op': 'find_login_fields', 'page_generation': 7}),
    ('login', {'op': 'fill', 'page_generation': 7, 'field_handle': 'field', 'value': None}),
    ('login', {'op': 'submit', 'page_generation': 7, 'submit_handle': 'submit'}),
    ('login', {'op': 'choose_passkey', 'page_generation': 7, 'title': 't', 'username': 'u'}),
    ('guest', {'op': 'guest_state'}),
    ('guest', {'op': 'guest_sign_in', 'region_host': 'my.1password.eu',
               'email': None, 'secret_key': None, 'password': None}),
    ('guest', {'op': 'guest_unlock', 'password': None}),
    ('capture', {'op': 'begin_live_capture'}),
    ('capture', {'op': 'take_capture', 'capture_id': 'capture'}),
]


@pytest.mark.parametrize('required,payload,kind', [
    (required, request, kind) for required, request in KIND_REQUESTS
    for kind in ('login', 'guest', 'capture') if kind != required
], ids=[f"{request['op']}-{kind}" for required, request in KIND_REQUESTS
        for kind in ('login', 'guest', 'capture') if kind != required])
async def test_driver_operations_refuse_wrong_session_kind(tmp_path, required, payload, kind):
    """Every wrong-kind RPC refuses before any DOM, proxy or capture mutation."""
    from unittest.mock import AsyncMock, Mock
    server, _, session, page, proxy = driver(tmp_path)
    origin = 'https://login.example'
    frame = SimpleNamespace(url=origin + '/', evaluate=AsyncMock(return_value=origin),
                            query_selector_all=AsyncMock(return_value=[]))
    element = SimpleNamespace(evaluate=AsyncMock(return_value=True),
                              is_visible=AsyncMock(return_value=True),
                              is_enabled=AsyncMock(return_value=True))
    page.frames = [frame]
    page.main_frame = frame
    page.wait_for_load_state = AsyncMock()
    page.on = Mock()
    session.context.new_page = AsyncMock(return_value=page)
    session.context.on = Mock()
    session.context.expose_binding = AsyncMock()
    proxy.status = AsyncMock(return_value={'run_id': 'run', 'site_id': 'site',
                                           'site_origins': [origin]})
    sid = await server.open_session(kind, 'run', 'site', session, proxy, (origin,))
    page.on.reset_mock()     # a login session's own frame watch (slice 5u) is not the RPC's
    s = server._sessions[sid]
    s.handles = {'field': (element, frame, 'password', origin, 7, 'password'),
                 'submit': (element, frame, None, origin, 7, 'submit')}
    before = list(proxy.calls)
    args = {k: canary() if v is None else v for k, v in payload.items()}
    try:
        assert await driver_rpc(server, dict(args, driver_session=sid)) == {'error': 'refused'}
        for method in (frame.evaluate, frame.query_selector_all, element.evaluate,
                       element.is_visible, element.is_enabled, page.wait_for_load_state,
                       session.context.new_page, session.context.expose_binding, proxy.status):
            method.assert_not_called()
        page.on.assert_not_called()
        session.context.on.assert_not_called()
        assert proxy.calls == before
        assert s.capture_id is None and s.broker_origins is None
    finally:
        await server.close_session(sid)


@pytest.mark.parametrize('op', ['fill', 'submit'])
async def test_driver_credential_actions_require_current_lease(tmp_path, op):
    """A missing login lease refuses even a valid handle before inspecting its element."""
    from unittest.mock import AsyncMock
    server, _, session, _, proxy = driver(tmp_path)
    sid = await server.open_session('login', 'run', 'site', session, proxy, ('https://login.example',))
    s = server._sessions[sid]
    element = SimpleNamespace(evaluate=AsyncMock(), is_visible=AsyncMock(return_value=True),
                              is_enabled=AsyncMock(return_value=True))
    frame = SimpleNamespace(evaluate=AsyncMock(return_value='https://login.example'))
    s.handles['handle'] = (element, frame, 'password', 'https://login.example', 7,
                           'password' if op == 'fill' else 'submit')
    s.lease = None
    args = dict(op=op, driver_session=sid, page_generation=7)
    args.update(field_handle='handle', value=canary()) if op == 'fill' else args.update(submit_handle='handle')
    try:
        assert await driver_rpc(server, args) == {'error': 'refused'}
        frame.evaluate.assert_not_called()
        element.evaluate.assert_not_called()
        element.is_visible.assert_not_called()
        element.is_enabled.assert_not_called()
    finally:
        await server.close_session(sid)


@pytest.mark.parametrize('failure', [False, True])
async def test_close_session_waits_for_replacement_lease(tmp_path, failure):
    """Cleanup cannot end a replacement before the proxy has processed its begin/revoke."""
    from casa_browser.broker import BrokerError
    from casa_browser.proxy_client import ProxyClient

    async with h.Harness(tmp_path, ['site.example', 'login.example']) as harness:
        proxy = ProxyClient(harness.control_path)
        await proxy.install(h.account_policy())
        server, _, session, _, _ = driver(tmp_path)
        session.site_id = 's1'
        session.generation = 1
        sid = await server.open_session('login', 'r1', 's1', session, proxy,
                                        ('https://login.example',))
        entered, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
        begin = proxy.begin_login

        async def delayed(lease):
            entered.set()
            await release.wait()
            token = await begin(lease)
            if failure:
                raise TimeoutError()
            return token

        proxy.begin_login = delayed
        replacement = asyncio.create_task(server.replace_login_lease(sid))
        await asyncio.wait_for(entered.wait(), 2)

        async def close():
            closing.set()
            await server.close_session(sid)

        cleanup = asyncio.create_task(close())
        await asyncio.wait_for(closing.wait(), 2)
        try:
            # Yield enough turns for the old cleanup to send end_login before begin arrives.
            done, _ = await asyncio.wait((cleanup,), timeout=0.05)
            assert not done
        finally:
            release.set()
            results = await asyncio.gather(replacement, cleanup, return_exceptions=True)
        if failure:
            assert isinstance(results[0], BrokerError) and results[0].code == 'unavailable'
        else:
            assert results[0] is None
        assert results[1] is None
        assert harness.addon.policy is None or harness.addon.policy.login_lease is None
        assert session.login_active is False and server._sessions == {}
        if not failure:
            status, _ = await harness.fetch('POST', 'https://login.example/session',
                                             data=b'x', headers=h.FETCH)
            assert status != 200
            assert harness.addon.events[-1].reason != 'login_lease'


class _CaptureFrame:
    def __init__(self, url, fail=False):
        self.url, self.fail, self.detached, self.installed = url, fail, False, 0

    async def evaluate(self, script, arg=None):
        if self.fail:
            raise RuntimeError("Execution context was destroyed")
        if "location.origin" in script and arg is None:
            return "https://" + self.url.split("/")[2]
        self.installed += 1

    def is_detached(self):
        return self.detached


class _CapturePage(Page):
    def __init__(self, frame):
        super().__init__()
        self.frames, self.handlers = [frame], {}

    def on(self, event, cb):
        self.handlers[event] = cb


@pytest.mark.parametrize("shape", ["fails", "recovers", "foreign", "detached"])
async def test_capture_reinstall_failure_marks_incomplete(tmp_path, monkeypatch, shape):
    """casa-test F2: a new document of a capture origin that never takes the capture reader
    marks the capture incomplete (Done then offers the manual entry) instead of reporting that
    nothing was typed; a document that takes it after a retry, one of another origin and one
    that went away leave it complete."""
    import casa_browser.login as login_mod
    from casa_browser.proxy_addon import EnforcingAddon
    from casa_browser.proxy_main import Proxy as ProxyServer
    monkeypatch.setattr(login_mod, "INSTALL_RETRY_S", 0)
    first = _CaptureFrame("https://site.example/start")
    page = _CapturePage(first)

    async def expose_binding(name, fn):
        return name
    context = SimpleNamespace(pages=[page], expose_binding=expose_binding, on=lambda *a: None)
    session = BrowserSession("account", "site", context, 4, 7)
    manager = SimpleNamespace(session=session, close=None)
    server = DriverServer(manager, tmp_path / "driver.sock", allowed_uid=os.getuid())
    proxy = Proxy()
    addon = EnforcingAddon()
    policy = h.account_policy(sites=(h.site(site_id="site"),))
    addon.install(replace(policy, run_id="run", active_session=replace(
        policy.active_session, site_id="site", generation=session.generation)))
    control = ProxyServer(addon=addon, confdir=tmp_path, control_path=tmp_path / "control",
                          controller_uid=os.getuid())

    async def status():
        return control.dispatch(b'{"op":"status"}')
    proxy.status = status
    sid = await server.open_session("capture", "run", "site", session, proxy, ("https://login.example",))
    await server.begin_live_capture(sid)
    s = server._sessions[sid]
    host = "other.example" if shape == "foreign" else "site.example"
    frame = _CaptureFrame(f"https://{host}/signin", fail=shape != "recovers")
    if shape == "recovers":
        calls = {"n": 0}
        real = frame.evaluate

        async def flaky(script, arg=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("Execution context was destroyed")
            return await real(script, arg)
        frame.evaluate = flaky
    if shape == "detached":
        frame.detached = True
    page.handlers["framenavigated"](frame)
    await asyncio.gather(*list(s.tasks), return_exceptions=True)
    assert s.buffer.take().incomplete is (shape == "fails")
    if shape == "recovers":
        assert frame.installed == 1


# -- slice 5 (S5-4): the capture's sign-in attempts ----------------------------------------

def _sent(username, password):
    return json.dumps({'username': username, 'password': password})


async def test_an_attempt_holds_the_values_sent_and_repeats_are_one():
    """Enter then submit (or click then submit) in one document is one attempt; the typo then
    the correction leaves the corrected value; a username missing from the password page's
    form is the last one recorded (a two-page sign-in)."""
    b = CaptureBuffer()
    b.record('plain_username', 'alice', 'd1:1')
    assert b.event('attempt', _sent('', 'pw-final'), 'd2', login=True) is True
    assert b.event('attempt', _sent('', 'pw-final'), 'd2', login=True) is False   # submit after Enter
    attempt = b.take().attempt
    assert attempt == {'username': 'alice', 'password': 'pw-final', 'failed': False,
                       'typed_after': False}
    assert b.take().attempt is None                          # a take empties the buffer


async def test_a_password_field_seen_again_fails_the_attempt():
    b = CaptureBuffer()
    b.event('attempt', _sent('alice', 'wrong'), 'd1', login=True)
    b.event('shown', '', 'd1', login=True)                    # its own page, before it unloads
    b.event('shown', '', 'd3', login=False)                   # not a login origin
    assert b.take().attempt['failed'] is False
    b.event('attempt', _sent('alice', 'wrong'), 'd1', login=True)
    b.event('shown', '', 'd2', login=True)                    # the form again, a new document
    assert b.take().attempt['failed'] is True
    b.event('attempt', _sent('alice', 'wrong'), 'd1', login=True)
    b.event('shown_final', '', 'd1', login=True)              # a form that stayed (an SPA error)
    assert b.take().attempt['failed'] is True
    b.event('attempt', _sent('alice', 'wrong'), 'd1', login=True)
    b.event('shown', '', 'd2', login=True)
    b.event('attempt', _sent('alice', 'right'), 'd2', login=True)   # the second try is the attempt
    assert b.take().attempt == {'username': 'alice', 'password': 'right', 'failed': False,
                                'typed_after': False}


async def test_password_typing_after_an_attempt_is_flagged():
    b = CaptureBuffer()
    b.event('typed', '', 'd1', login=True)                    # before any attempt: nothing
    b.event('attempt', _sent('alice', 'secret-1'), 'd1', login=True)
    b.event('typed', '', 'd2', login=True)
    assert b.take().attempt['typed_after'] is True
    b.event('attempt', _sent('alice', 'secret-1'), 'd1', login=True)
    b.event('typed', '', 'd1', login=True)
    b.event('attempt', _sent('alice', 'secret-1'), 'd1', login=True)  # sent again after typing
    assert b.take().attempt['typed_after'] is False


async def test_an_identifier_only_attempt_holds_its_own_identifier():
    """Slice 5n (O2): an email sent with no password is an attempt; its identifier is the
    attempt's own (never remembered from another page), never a value seen as a password, and
    a later password attempt replaces it."""
    b = CaptureBuffer()
    b.record('plain_username', 'alice', 'd1:1')
    assert b.event('attempt', _sent('', ''), 'd2', login=True) is False      # nothing sent
    b.record('password', 'pw-seen', 'd1:2')
    assert b.event('attempt', _sent('pw-seen', ''), 'd2', login=True) is False   # a password
    assert b.event('attempt', _sent('432198', ''), 'd2', login=True) is False      # r2: a code
    assert b.event('attempt', _sent('alice', ''), 'd2', login=True) is False       # not an email
    assert b.event('attempt', _sent('me@example.com', ''), 'd2', login=True) is True
    assert b.event('attempt', _sent('me@example.com', ''), 'd2', login=True) is False   # repeat
    assert b.take().attempt == {'username': 'me@example.com', 'password': '', 'failed': False,
                                'typed_after': False}
    b.event('attempt', _sent('me@example.com', ''), 'd1', login=True)
    b.event('attempt', _sent('', 'pw-2'), 'd2', login=True)                  # two-page: email, then password
    assert b.take().attempt['password'] == 'pw-2'
    b.event('attempt', _sent('me@example.com', ''), 'd1', login=True)
    b.event('shown', '', 'd2', login=True)                    # a password page followed, unused
    assert b.take().attempt['failed'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('op', ['mark_document', 'document_marked'])
async def test_code_page_mark_ops_take_the_brokers_login_origins(tmp_path, op):
    """Slice 6: the broker sends its login origins with every driver call once a step has
    required them (`_login_flow.drive`); the mark operations accept them like the other login
    operations (found in real Chromium: a refusal turned every code page into an error)."""
    server = DriverServer(SimpleNamespace(session=None), tmp_path / 'driver.sock')
    request = {'op': op, 'driver_session': 'nope', 'page_generation': 1, 'mark': 'm' * 32,
               'login_origins': ['https://login.example']}
    assert await server.dispatch(request) == {'error': 'refused'}       # validated, then no session
    del request['login_origins']
    assert await server.dispatch(request) == {'error': 'refused'}
    request['extra'] = 1
    assert await server.dispatch(request) == {'error': 'unsupported'}


# --------------------------------------------------------------------------- slice 5u

def _approved(server, sid, origin='https://login.example'):
    """A login session whose current step's read approved one field in ``frame``."""
    from unittest.mock import AsyncMock
    s = server._sessions[sid]
    frame = SimpleNamespace(url=origin + '/signin', detached=False, evaluate=AsyncMock(return_value=origin))
    frame.is_detached = lambda: frame.detached
    element = SimpleNamespace(evaluate=AsyncMock(return_value=True),
                              is_visible=AsyncMock(return_value=True),
                              is_enabled=AsyncMock(return_value=True))
    generation = s.browser.view_generation
    s.addresses['field'] = origin
    s.handles['field'] = (element, frame, 'password', origin, generation, 'password')
    return s, frame, element, generation


async def test_a_page_that_only_moved_is_told_apart(tmp_path):
    """Slice 5u: another frame's navigation (or a route change of the approved frame within its
    origin) is page_changed — the broker reads the page again — and nothing is set."""
    server, _, session, page, proxy = driver(tmp_path)
    sid = await server.open_session('login', 'run', 'site', session, proxy, ('https://login.example',))
    s, frame, element, generation = _approved(server, sid)
    other = SimpleNamespace(url='https://www.google.com/recaptcha/anchor', is_detached=lambda: False)
    page.emit('framenavigated', other)
    frame.url = 'https://login.example/signin#step2'
    page.emit('framenavigated', frame)
    session.view_generation += 2
    reply = await server.dispatch({'op': 'fill', 'driver_session': sid, 'page_generation': generation,
                                   'field_handle': 'field', 'value': canary()})
    assert reply == {'error': 'page_changed'} and not s.departed
    element.evaluate.assert_not_called()
    await server.close_session(sid)


@pytest.mark.parametrize('where', ['check', 'set'])
@pytest.mark.parametrize('connected', [False, True])
async def test_an_element_the_page_re_rendered_away_is_a_page_change(tmp_path, where, connected):
    """The residual of v0.1.0: a field the page re-rendered away between the read and the fill
    (its handle no longer connected, no navigation) is page_changed — the broker reads the page
    again — never refused, which interrupted the run. A connected field that fails its check or
    its set is still refused."""
    from unittest.mock import AsyncMock
    server, _, session, page, proxy = driver(tmp_path)
    sid = await server.open_session('login', 'run', 'site', session, proxy, ('https://login.example',))
    s, frame, element, generation = _approved(server, sid)

    async def evaluate(script, arg=None):
        if script == 'e => e.isConnected':
            return connected
        return False                               # the set fails
    if where == 'check':
        element.is_visible = AsyncMock(return_value=False)
    element.evaluate = AsyncMock(side_effect=evaluate)
    reply = await server.dispatch({'op': 'fill', 'driver_session': sid, 'page_generation': generation,
                                   'field_handle': 'field', 'value': canary()})
    assert reply == ({'error': 'refused'} if connected else {'error': 'page_changed'})
    assert not s.departed
    await server.close_session(sid)


@pytest.mark.parametrize('how', ['round_trip', 'stays_away', 'detached'])
async def test_an_approved_frame_that_left_ends_the_sign_in(tmp_path, how):
    """Slice 5u (Astra p3/p4): once a frame holding a live handle left its origin or the page,
    every op of the session refuses — settle and discovery too, whatever generation they
    name — even after the frame came back with a form."""
    server, _, session, page, proxy = driver(tmp_path)
    sid = await server.open_session('login', 'run', 'site', session, proxy, ('https://login.example',))
    s, frame, element, generation = _approved(server, sid)
    if how == 'detached':
        frame.detached = True
        page.emit('framedetached', frame)
    else:
        frame.url = 'https://outside.example/collect'
        page.emit('framenavigated', frame)
        if how == 'round_trip':
            frame.url = 'https://login.example/signin'
            page.emit('framenavigated', frame)
    session.view_generation += 1
    assert s.departed
    for request in ({'op': 'fill', 'driver_session': sid, 'page_generation': generation,
                     'field_handle': 'field', 'value': canary()},
                    {'op': 'settle', 'driver_session': sid, 'timeout_s': 5},
                    {'op': 'find_login_fields', 'driver_session': sid,
                     'page_generation': session.view_generation}):
        assert await server.dispatch(request) == {'error': 'refused'}
    element.evaluate.assert_not_called()
    await server.close_session(sid)
    assert page.listeners == {'framenavigated': [], 'framedetached': []}   # the watch is gone


async def test_a_frame_without_a_live_handle_is_not_watched(tmp_path):
    """A navigation of the approved frame after its approval ended (the click cleared it) or of
    any other frame never marks the session."""
    server, _, session, page, proxy = driver(tmp_path)
    sid = await server.open_session('login', 'run', 'site', session, proxy, ('https://login.example',))
    s, frame, _, _ = _approved(server, sid)
    s.handles.clear()
    frame.url = 'https://site.example/home'
    page.emit('framenavigated', frame)
    assert not s.departed
    await server.close_session(sid)


async def test_submit_counts_once_armed_and_ends_the_approval_at_its_click(tmp_path):
    """Slice 5u: a submit stopped by a page change after arming the lease close counts as one
    (the next form found takes a fresh lease); the approval ends right before the click, so
    the landing navigation is never judged."""
    from unittest.mock import AsyncMock
    from casa_browser import login
    server, _, session, page, proxy = driver(tmp_path)
    sid = await server.open_session('login', 'run', 'site', session, proxy, ('https://login.example',))
    s, frame, element, generation = _approved(server, sid)
    s.handles['submit'] = (element, frame, None, 'https://login.example', generation, 'submit')
    s.addresses['submit'] = 'https://login.example'

    async def moved_while_arming(lease_id):
        session.view_generation += 1
    proxy.arm_login_close = moved_while_arming
    request = {'op': 'submit', 'driver_session': sid, 'page_generation': generation,
               'submit_handle': 'submit'}
    assert await server.dispatch(request) == {'error': 'page_changed'}
    assert s.submitted is True
    assert login._SUBMIT not in [c.args[0] for c in element.evaluate.call_args_list]

    seen = []

    async def click(script, arg=None):
        if script != login._SUBMIT:
            return True
        seen.append(dict(s.handles))
        frame.url = 'https://site.example/home'       # the landing, before the reply
        page.emit('framenavigated', frame)
        return True
    element.evaluate = AsyncMock(side_effect=click)
    proxy.arm_login_close = AsyncMock()
    s.handles['submit'] = (element, frame, None, 'https://login.example', session.view_generation, 'submit')
    s.addresses['submit'] = 'https://login.example'
    request['page_generation'] = session.view_generation
    assert await server.dispatch(request) == {'ok': True}
    assert seen == [{}] and not s.departed
    await server.close_session(sid)


async def test_settle_waits_for_a_quiet_page(tmp_path):
    """Slice 5u: settle returns once a quarter second passed without a frame navigation, and
    returns the generation read after that wait."""
    from unittest.mock import AsyncMock
    server, _, session, page, proxy = driver(tmp_path)
    origin = 'https://login.example'
    frame = SimpleNamespace(url=origin + '/', evaluate=AsyncMock(return_value=origin))
    page.main_frame, page.frames = frame, [frame]
    page.wait_for_load_state = AsyncMock()
    proxy.status = AsyncMock(return_value={'run_id': 'run', 'site_id': 'site', 'site_origins': [origin]})
    sid = await server.open_session('login', 'run', 'site', session, proxy, (origin,))

    async def churn():
        for _ in range(3):                         # a burst, like a reCAPTCHA's frames
            await asyncio.sleep(0.15)
            session.view_generation += 1
    task = asyncio.create_task(churn())
    reply = await server.dispatch({'op': 'settle', 'driver_session': sid, 'timeout_s': 5})
    await task
    assert reply['page_generation'] == session.view_generation
    assert page.wait_for_load_state.await_count >= 2   # waited again after a moved round
    await server.close_session(sid)


@pytest.mark.parametrize('loads_finish', [False, True])
async def test_settle_waits_for_a_quiet_page_at_most_its_bound(tmp_path, monkeypatch, loads_finish):
    """Astra r1: the quiet wait, its load waits included, ends at SETTLE_QUIET_S; the page is
    then read as it is (a page that keeps navigating, or a load that does not finish)."""
    import time
    from unittest.mock import AsyncMock
    from casa_browser import login
    monkeypatch.setattr(login, 'SETTLE_QUIET_S', 0.8)
    server, _, session, page, proxy = driver(tmp_path)
    origin = 'https://login.example'
    frame = SimpleNamespace(url=origin + '/', evaluate=AsyncMock(return_value=origin))
    page.main_frame, page.frames = frame, [frame]
    loads = []

    async def load(state, timeout):
        loads.append(timeout)
        if len(loads) > 1 and not loads_finish:    # the load after a move never finishes in time
            await asyncio.sleep(timeout / 1000)
            raise TimeoutError('load')
    page.wait_for_load_state = load
    proxy.status = AsyncMock(return_value={'run_id': 'run', 'site_id': 'site', 'site_origins': [origin]})
    sid = await server.open_session('login', 'run', 'site', session, proxy, (origin,))

    async def churn():
        while True:
            await asyncio.sleep(0.05)
            session.view_generation += 1
    task = asyncio.create_task(churn())
    started = time.monotonic()
    try:
        reply = await server.dispatch({'op': 'settle', 'driver_session': sid, 'timeout_s': 10})
    finally:
        task.cancel()
    # the bound holds to the read (Terra r2): no last quarter-second sleep past it
    assert time.monotonic() - started < 0.8 + 0.15 and 'page_generation' in reply, reply
    assert loads[1] <= 800                         # the load wait got what was left, not timeout_s
    await server.close_session(sid)


# -- slice 5v: the guest's measured pages and 1Password's passkey prompt -------------------------
# Doubles answer the driver's fixed scripts by name; the scripts themselves are pinned against
# the measured DOM in the image lane (integration/test_login.py) and the live pin.

class GuestElement:
    def __init__(self, page, name, accept=True):
        self.page, self.name, self.accept = page, name, accept

    async def evaluate(self, script, arg=None):
        from casa_browser.login import _GUEST_SET
        assert script == _GUEST_SET
        self.page.sets.append((self.name, arg['origin'], arg['path'], arg['type'], arg['value']))
        return self.accept and self.page.at == (arg['origin'], arg['path'])

    async def press(self, key):
        self.page.pressed.append((self.name, key))
        self.page.on_enter(self.name)


class GuestPage:
    """The driver's guest tab: a real page's lifecycle (goto, close), the measured sign-in steps
    and the popup's states answered by name."""
    def __init__(self, ctx):
        from casa_browser.login import EXTENSION_ORIGIN
        self.ctx, self.closed, self.url, self.at = ctx, False, 'about:blank', None
        self.target = f'tab{len(ctx.targets)}'
        ctx.targets[self.target] = self
        self.sets, self.pressed, self.step = ctx.sets, ctx.pressed, 0
        self.main_frame = self
        self.extension = EXTENSION_ORIGIN

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True
        self.ctx.targets.pop(self.target, None)

    async def goto(self, url, wait_until=None):
        from urllib.parse import urlsplit
        if self.ctx.debugger_refused and url.startswith(self.extension):
            # Measured on Chrome 156 (slice 5w): Chrome refuses the debugger on the extension's
            # page, Patchright drops the tab as closed, and the tab stays open in Chrome.
            self.closed = True
            raise RuntimeError('Page.goto: Target page, context or browser has been closed')
        if self.ctx.not_installed and url.startswith(self.extension):
            self.ctx.not_installed -= 1
            raise RuntimeError('net::ERR_BLOCKED_BY_CLIENT')
        self.ctx.visits.append(url)
        self.url = url.split('?')[0]
        parts = urlsplit(url)
        self.at = (f'{parts.scheme}://{parts.netloc}', parts.path)
        self.step = 0

    async def query_selector(self, selector):
        fields = {0: {'input[type=email]': 'email'},
                  1: {'#account-key': 'key', 'input[type=password]': 'password'}}
        if self.at[0] == self.extension:
            return GuestElement(self, 'unlock') if (selector == 'input[type=password]'
                                                    and self.ctx.popup == 'locked') else None
        name = fields.get(self.step, {}).get(selector)
        return GuestElement(self, name, self.ctx.accept) if name else None

    def on_enter(self, name):
        if name == 'email':
            self.step = 1 if self.ctx.second_step else 9
        elif name == 'password':
            self.url = self.extension + '/app/app.html#/page/ready'   # the measured landing
            self.at = (self.extension, '/app/app.html')
            self.ctx.popup = 'unlocked'
        elif name == 'unlock':
            self.ctx.popup = 'unlocked'

    async def evaluate(self, script, arg=None):
        from casa_browser.login import _POPUP_STATE
        assert script == _POPUP_STATE and arg == self.extension
        return self.ctx.popup


def login_module():
    from casa_browser import login
    return login


class ChromeTargets:
    """Chrome's own target list through a CDP session (5w): every tab, whether Patchright
    still lists it or not; closing a target closes its tab."""
    def __init__(self, ctx):
        self.ctx, self.detached = ctx, False

    async def send(self, method, params=None):
        if self.ctx.cdp_fails:
            raise RuntimeError('Protocol error')
        if self.ctx.cdp_fail_next:            # a passing failure (reviewers' r2)
            self.ctx.cdp_fail_next -= 1
            raise RuntimeError('Protocol error')
        self.ctx.cdp.append(method)
        if method == 'Target.getTargets':
            return {'targetInfos': [{'targetId': t, 'type': 'page', 'url': 'x'}
                                    for t in self.ctx.targets]}
        if method == 'Target.closeTarget':
            if self.ctx.close_fail_next:      # a close that did not happen (reviewers' r2)
                self.ctx.close_fail_next -= 1
                raise RuntimeError('Protocol error')
            page = self.ctx.targets.pop(params['targetId'])
            page.closed = True
            return {}
        raise AssertionError(method)

    async def detach(self):
        self.detached = True


class GuestContext:
    def __init__(self, main):
        self.pages = [main]
        self.targets = {'site': main}
        self.sets, self.pressed, self.visits, self.cdp = [], [], [], []
        self.popup = None            # None: the signed-out popup (no marker)
        self.not_installed = 0
        self.accept = True
        self.second_step = True
        self.debugger_refused = False
        self.cdp_fails = False
        self.cdp_fail_next = 0
        self.close_fail_next = 0

    async def new_page(self):
        page = GuestPage(self)
        self.pages.append(page)
        return page

    async def new_cdp_session(self, page):
        assert page is self.pages[0]          # always the site's page
        return ChromeTargets(self)


def guest_driver(tmp_path, monkeypatch):
    from casa_browser import login
    monkeypatch.setattr(login, 'GUEST_READ_S', 0.3)
    monkeypatch.setattr(login, 'GUEST_INSTALL_WAIT_S', 1)
    monkeypatch.setattr(login, 'GUEST_FIELD_WAIT_S', 0.3)
    monkeypatch.setattr(login, 'GUEST_LANDING_WAIT_S', 0.3)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(login.asyncio, 'sleep', lambda s: real_sleep(min(s, 0.01)))
    page = Page()
    ctx = GuestContext(page)
    session = BrowserSession('account', 'site', ctx, 4, 7)
    manager = SimpleNamespace(session=session)
    proxy = Proxy()

    async def open_window():
        proxy.calls.append(('open_window',))
        return ('boot', 99)
    proxy.open_window = open_window
    return DriverServer(manager, tmp_path / 'driver.sock', allowed_uid=os.getuid()), session, ctx, proxy


@pytest.mark.parametrize('popup, expected', [('locked', 'locked'), ('unlocked', 'unlocked'),
                                             (None, 'signed_out')])
async def test_the_guest_state_is_read_from_the_drivers_own_popup_tab(tmp_path, monkeypatch,
                                                                       popup, expected):
    """The popup in a tab the driver opens: its lock screen, its vault list, or neither (a
    signed-out extension renders nothing); the site's page is never read."""
    from casa_browser.login import EXTENSION_POPUP
    server, session, ctx, proxy = guest_driver(tmp_path, monkeypatch)
    ctx.popup = popup
    sid = await server.open_session('guest', 'run', 'site', session, proxy, ())
    try:
        assert await driver_rpc(server, {'op': 'guest_state', 'driver_session': sid}) == \
            {'state': expected}
        assert ctx.visits == [EXTENSION_POPUP] and ctx.pages[0].calls == []
    finally:
        await server.close_session(sid)
    assert ctx.pages[1].closed            # the guest tab ends with its session


async def test_an_extension_not_installed_yet_is_waited_for_then_unknown(tmp_path, monkeypatch):
    from casa_browser.login import EXTENSION_POPUP
    server, session, ctx, proxy = guest_driver(tmp_path, monkeypatch)
    ctx.popup, ctx.not_installed = 'locked', 2
    sid = await server.open_session('guest', 'run', 'site', session, proxy, ())
    try:
        assert await server.guest_state(sid) == {'state': 'locked'}
        ctx.not_installed = 10 ** 6
        assert await server.guest_state(sid) == {'state': 'unknown'}
    finally:
        await server.close_session(sid)


async def test_the_guest_signs_in_in_two_steps_only_on_1passwords_page(tmp_path, monkeypatch):
    """Measured: the email, Enter; the Secret Key and the password, Enter; the tab lands on the
    extension's page and the popup reads unlocked. Every value is set only where the page is
    exactly https://my.1password.<region>/signin, inside the credential window."""
    from casa_browser.login import EXTENSION_POPUP
    server, session, ctx, proxy = guest_driver(tmp_path, monkeypatch)
    values = dict(email=canary(), secret_key=canary(), password=canary())
    sid = await server.open_session('guest', 'run', 'site', session, proxy, ())
    try:
        reply = await driver_rpc(server, {'op': 'guest_sign_in', 'driver_session': sid,
                                          'region_host': 'acct.1password.eu', **values})
        assert reply == {'state': 'unlocked'}
        assert ctx.visits == ['https://my.1password.eu/signin?auth-only=1', EXTENSION_POPUP]
        assert [s[0] for s in ctx.sets] == ['email', 'key', 'password']
        assert {s[1:3] for s in ctx.sets} == {('https://my.1password.eu', '/signin')}
        assert [s[4] for s in ctx.sets] == [values['email'], values['secret_key'],
                                            values['password']]
        assert [s[3] for s in ctx.sets] == ['email', 'text', 'password']
        assert ctx.pressed == [('email', 'Enter'), ('password', 'Enter')]
        assert proxy.calls[0] == ('open_window',)
    finally:
        await server.close_session(sid)
    assert ('close_window', ('boot', 99)) in proxy.calls


async def test_a_sign_in_page_elsewhere_gets_no_value(tmp_path, monkeypatch):
    """The page's own check refuses (another origin or path at the moment of setting): the
    sign-in is refused before any value is set and nothing more is tried."""
    server, session, ctx, proxy = guest_driver(tmp_path, monkeypatch)
    ctx.accept = False
    sid = await server.open_session('guest', 'run', 'site', session, proxy, ())
    try:
        assert await driver_rpc(server, {'op': 'guest_sign_in', 'driver_session': sid,
                                         'region_host': 'acct.1password.eu', 'email': canary(),
                                         'secret_key': canary(), 'password': canary()}) == \
            {'error': 'refused'}
        assert len(ctx.sets) == 1 and ctx.pressed == []
    finally:
        await server.close_session(sid)


async def test_a_sign_in_without_its_second_step_is_unknown(tmp_path, monkeypatch):
    """A page after the email that is not the measured second step (a verification, a slow
    page) gets nothing more: unknown, which the run controller logs."""
    server, session, ctx, proxy = guest_driver(tmp_path, monkeypatch)
    ctx.second_step = False
    sid = await server.open_session('guest', 'run', 'site', session, proxy, ())
    try:
        assert await server.guest_sign_in(sid, 'acct.1password.eu', canary(), canary(),
                                          canary()) == {'state': 'unknown'}
        assert [s[0] for s in ctx.sets] == ['email']
    finally:
        await server.close_session(sid)


async def test_the_guest_unlocks_only_in_the_popup(tmp_path, monkeypatch):
    from casa_browser.login import EXTENSION_ORIGIN, EXTENSION_POPUP_PATH
    server, session, ctx, proxy = guest_driver(tmp_path, monkeypatch)
    ctx.popup = 'locked'
    password = canary()
    sid = await server.open_session('guest', 'run', 'site', session, proxy, ())
    try:
        assert await server.guest_unlock(sid, password) == {'state': 'unlocked'}
        assert ctx.sets == [('unlock', EXTENSION_ORIGIN, EXTENSION_POPUP_PATH, 'password',
                             password)]
        ctx.popup = 'unlocked'
        ctx.sets.clear()
        assert await server.guest_unlock(sid, password) == {'state': 'unlocked'}
        assert ctx.sets == []               # not locked: nothing typed
    finally:
        await server.close_session(sid)


async def test_chrome_refusing_the_debugger_on_the_popup_leaves_no_tab_behind(tmp_path,
                                                                              monkeypatch):
    """Measured on casa-test (Chrome 156, slice 5w): every popup tab the driver opened was
    dropped by Patchright yet stayed open in Chrome, and the live view landed on them. At the
    guest session's end every tab opened since its start is closed through Chrome's own target
    list — the driver's, the lost ones, one the extension opened — a tab that was there before
    is kept, and the site's page comes to the front."""
    server, session, ctx, proxy = guest_driver(tmp_path, monkeypatch)
    ctx.debugger_refused = True
    before = GuestPage(ctx)                    # a tab open before the guest session
    sid = await server.open_session('guest', 'run', 'site', session, proxy, ())
    try:
        assert await server.guest_state(sid) == {'state': 'unknown'}
        GuestPage(ctx)                         # a tab the extension opened on its own
        assert len(ctx.targets) >= 4           # site, before, lost popup tabs, the extension's
    finally:
        await server.close_session(sid)
    assert ctx.targets == {'site': ctx.pages[0], before.target: before}
    assert ctx.pages[0].calls == ['bring_to_front'] and not before.closed
    assert server.open_sessions() == () and session.login_active is False
    assert not [c for c in proxy.calls if c[0] == 'revoke']


@pytest.mark.parametrize('failures, close_failures', [(1, 0), (3, 0), (0, 1)])
async def test_a_read_of_chromes_tabs_that_fails_once_is_tried_again(tmp_path, monkeypatch,
                                                                     failures, close_failures):
    """Reviewers' r2: a read of Chrome's tabs at the end that fails (once, or a few times
    within the bound), or a close that did not happen, is tried again; the lost tabs are still
    closed."""
    server, session, ctx, proxy = guest_driver(tmp_path, monkeypatch)
    ctx.debugger_refused = True
    sid = await server.open_session('guest', 'run', 'site', session, proxy, ())
    try:
        assert await server.guest_state(sid) == {'state': 'unknown'}
        assert len(ctx.targets) > 1
        ctx.cdp_fail_next, ctx.close_fail_next = failures, close_failures
    finally:
        await server.close_session(sid)
    assert set(ctx.targets) == {'site'} and ctx.pages[0].calls == ['bring_to_front']


async def test_a_tab_sweep_chrome_does_not_answer_ends_the_session_as_before(tmp_path,
                                                                              monkeypatch):
    """Best effort: Chrome's target list unreadable at the start or the end ends the guest
    session as before (its own tab closed, nothing revoked), the site's page still brought to
    the front."""
    server, session, ctx, proxy = guest_driver(tmp_path, monkeypatch)
    monkeypatch.setattr(login_module(), '_SWEEP_S', 0.3)
    ctx.popup = 'locked'
    sid = await server.open_session('guest', 'run', 'site', session, proxy, ())
    try:
        assert await server.guest_state(sid) == {'state': 'locked'}
        ctx.cdp_fails = True
    finally:
        await server.close_session(sid)
    assert ctx.pages[1].closed and server.open_sessions() == ()
    assert ctx.pages[0].calls == ['bring_to_front']
    assert not [c for c in proxy.calls if c[0] == 'revoke']
    ctx.pages[0].calls.clear()
    # Unreadable at the start (reviewers' r1): no tab could be told apart at the end, so the
    # guest opens none — its operation is unavailable (the broker logs it), nothing is left.
    ctx.cdp_fails = True
    pages = len(ctx.pages)
    sid = await server.open_session('guest', 'run', 'site', session, proxy, ())
    try:
        assert await driver_rpc(server, {'op': 'guest_state', 'driver_session': sid}) == \
            {'error': 'unavailable'}
        ctx.cdp_fails = False                  # readable again by the end: still nothing to close
    finally:
        await server.close_session(sid)
    assert len(ctx.pages) == pages and set(ctx.targets) == {'site'}
    assert server.open_sessions() == () and ctx.pages[0].calls == []


class PromptFrame:
    def __init__(self, url, parent, origin, button=True):
        self.url, self.parent_frame, self.origin = url, parent, origin
        self.clicks = []
        self.button = button
        self.asked = []

    async def evaluate(self, script, arg=None):
        from casa_browser.login import _PROMPT_SHOWN
        if script == '() => location.origin':
            return self.origin
        assert script == _PROMPT_SHOWN
        return True

    async def evaluate_handle(self, script, arg):
        from casa_browser.login import _PASSKEY_BUTTON
        assert script == _PASSKEY_BUTTON
        self.asked.append(arg)
        frame = self

        class Button:
            async def click(self, timeout=None):
                frame.clicks.append(timeout)
        return SimpleNamespace(as_element=lambda: Button() if self.button else None)

    async def query_selector_all(self, selector):
        return []


def prompt_page(page, main_origin, frames):
    main = PromptFrame(main_origin + '/', None, main_origin)
    page.main_frame = main
    page.frames = [main, *(f(main) for f in frames)]
    return main


@pytest.mark.parametrize('case', ['offered', 'main_elsewhere', 'nested', 'two', 'no_match',
                                  'not_extension'])
async def test_a_passkey_prompt_is_answered_only_on_the_login_page_for_the_linked_item(
        tmp_path, case):
    from casa_browser.login import EXTENSION_NOTIFICATION, EXTENSION_ORIGIN
    server, _, session, page, proxy = driver(tmp_path)
    login = 'https://login.example'
    url = EXTENSION_NOTIFICATION + 'notification.html?language=en'
    prompt = lambda main: PromptFrame(url, main, EXTENSION_ORIGIN, button=case != 'no_match')
    frames = {'offered': [prompt], 'main_elsewhere': [prompt], 'no_match': [prompt],
              'nested': [lambda main: PromptFrame(url, SimpleNamespace(), EXTENSION_ORIGIN)],
              'two': [prompt, prompt],
              'not_extension': [lambda main: PromptFrame(url, main, 'https://evil.example')]}[case]
    prompt_page(page, 'https://elsewhere.example' if case == 'main_elsewhere' else login, frames)
    page.on = lambda *a: None
    sid = await server.open_session('login', 'run', 'site', session, proxy, (login,))
    try:
        reply = await driver_rpc(server, {'op': 'choose_passkey', 'driver_session': sid,
                                          'page_generation': session.view_generation,
                                          'title': 'Item', 'username': 'user'})
        clicks = [c for f in page.frames[1:] for c in f.clicks]
        if case == 'offered':
            assert reply == {'ok': True} and clicks == [5000]
            assert page.frames[1].asked == [{'title': 'Item', 'username': 'user'}]
        else:
            assert reply == {'ok': False} and clicks == []
    finally:
        await server.close_session(sid)


async def test_only_1passwords_prompt_in_this_page_is_a_passkey_frame(tmp_path):
    """find_login_fields reports the prompt only for 1Password's notification frame that is a
    child of the page's main frame (was: any extension frame)."""
    from unittest.mock import AsyncMock
    from casa_browser.login import EXTENSION_NOTIFICATION, EXTENSION_ORIGIN
    server, _, session, page, proxy = driver(tmp_path)
    login = 'https://login.example'
    for frames, expected in [
            ([lambda m: PromptFrame(EXTENSION_NOTIFICATION + 'notification.html', m,
                                    EXTENSION_ORIGIN)], True),
            ([lambda m: PromptFrame(EXTENSION_ORIGIN + '/inline/menu/menu.html', m,
                                    EXTENSION_ORIGIN)], False),
            ([lambda m: PromptFrame(EXTENSION_NOTIFICATION + 'notification.html',
                                    SimpleNamespace(), EXTENSION_ORIGIN)], False)]:
        prompt_page(page, login, frames)
        page.on = lambda *a: None
        proxy.status = AsyncMock(return_value={'run_id': 'run', 'site_id': 'site',
                                               'site_origins': [login]})
        sid = await server.open_session('login', 'run', 'site', session, proxy, (login,))
        try:
            fields = await server.find_login_fields(sid, session.view_generation)
            assert fields['passkey_frame'] is expected
        finally:
            await server.close_session(sid)


class HintInput:
    """An input as field discovery reads it: visible, enabled, its autocomplete; no role
    (a role-less field keeps forms out of the read)."""
    def __init__(self, autocomplete, visible=True, enabled=True):
        self.autocomplete, self.visible, self.enabled = autocomplete, visible, enabled

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def evaluate(self, script, arg=None):
        from casa_browser.login import _DISCOVER, _ROLE
        if script == _DISCOVER:
            return {'type': 'text', 'autocomplete': self.autocomplete, 'connected': True,
                    'invalid': False}
        assert script == _ROLE
        return None


class HintFrame(PromptFrame):
    def __init__(self, origin, inputs):
        super().__init__(origin + '/', None, origin)
        self.inputs = inputs

    async def query_selector_all(self, selector):
        assert selector == 'input'
        return list(self.inputs)


@pytest.mark.parametrize('inputs, on_login_origin, expected', [
    ([HintInput('webauthn')], True, True),
    ([HintInput('nickname webauthn')], True, True),
    ([HintInput('Nickname  WEBAUTHN')], True, True),
    ([HintInput('off'), HintInput('webauthn')], True, True),
    ([HintInput('webauthn', visible=False)], True, False),
    ([HintInput('webauthn', enabled=False)], True, False),
    ([HintInput('webauthn')], False, False),
    ([HintInput('webauthn nickname')], True, False),
    ([HintInput('xwebauthn')], True, False),
    ([HintInput('')], True, False),
    ([], True, False),
])
async def test_a_passkey_hint_is_a_shown_webauthn_field_on_a_login_origin(
        tmp_path, inputs, on_login_origin, expected):
    """The lazy guest unlock's T1: a visible, enabled input on a login origin whose autocomplete
    ends with the token ``webauthn`` (the one sign of a passkey request while the guest has never
    signed in); nothing else is one."""
    from unittest.mock import AsyncMock
    server, _, session, page, proxy = driver(tmp_path)
    login = 'https://login.example'
    main = HintFrame(login if on_login_origin else 'https://other.example', inputs)
    page.main_frame, page.frames = main, [main]
    page.on = lambda *a: None
    proxy.status = AsyncMock(return_value={'run_id': 'run', 'site_id': 'site',
                                           'site_origins': [login]})
    sid = await server.open_session('login', 'run', 'site', session, proxy, (login,))
    try:
        fields = await driver_rpc(server, {'op': 'find_login_fields', 'driver_session': sid,
                                           'page_generation': session.view_generation})
        assert fields['passkey_hint'] is expected
        assert fields['passkey_frame'] is False and fields['roles'] == {}
    finally:
        await server.close_session(sid)


async def test_a_passkey_click_that_moves_the_page_still_counts(tmp_path):
    """r1 Astra: a successful assertion navigates; the click is not judged by the page it led
    to (as after submit), so the broker learns it was made."""
    from casa_browser.login import EXTENSION_NOTIFICATION, EXTENSION_ORIGIN
    server, _, session, page, proxy = driver(tmp_path)
    login = 'https://login.example'
    url = EXTENSION_NOTIFICATION + 'notification.html'

    def moving(main):
        frame = PromptFrame(url, main, EXTENSION_ORIGIN)
        original = frame.evaluate_handle

        async def evaluate_handle(script, arg):
            handle = await original(script, arg)
            element = handle.as_element()

            async def click(timeout=None):
                await element.click(timeout)
                session.view_generation += 1          # the site's page moved on
            return SimpleNamespace(as_element=lambda: SimpleNamespace(click=click))
        frame.evaluate_handle = evaluate_handle
        return frame
    prompt_page(page, login, [moving])
    page.on = lambda *a: None
    sid = await server.open_session('login', 'run', 'site', session, proxy, (login,))
    try:
        assert await driver_rpc(server, {'op': 'choose_passkey', 'driver_session': sid,
                                         'page_generation': session.view_generation,
                                         'title': 'Item', 'username': 'user'}) == {'ok': True}
        assert page.frames[1].clicks == [5000]
    finally:
        await server.close_session(sid)


async def test_an_extension_frame_that_goes_away_under_the_read_is_no_error(tmp_path):
    """Live pin, 2026-11-02 (an unlocked extension re-renders its frame): 1Password's frames are
    never read for an origin, and one that detaches under the prompt read is no prompt rather
    than an error that would interrupt the run."""
    from unittest.mock import AsyncMock
    from casa_browser.login import EXTENSION_NOTIFICATION, EXTENSION_ORIGIN
    server, _, session, page, proxy = driver(tmp_path)
    login = 'https://login.example'

    def detached(main):
        frame = PromptFrame(EXTENSION_NOTIFICATION + 'notification.html', main, EXTENSION_ORIGIN)
        frame.evaluate = AsyncMock(side_effect=RuntimeError('Frame was detached'))
        return frame
    prompt_page(page, login, [detached])
    page.on = lambda *a: None
    proxy.status = AsyncMock(return_value={'run_id': 'run', 'site_id': 'site',
                                           'site_origins': [login]})
    sid = await server.open_session('login', 'run', 'site', session, proxy, (login,))
    try:
        fields = await driver_rpc(server, {'op': 'find_login_fields', 'driver_session': sid,
                                           'page_generation': session.view_generation})
        assert fields['passkey_frame'] is False and fields['origin_ok'] is True
    finally:
        await server.close_session(sid)


async def test_a_click_the_prompt_went_away_under_is_a_page_change(tmp_path):
    """The prompt detached or moved under the click: page_changed (the broker reads the page
    again, its one try spent), never an error that would interrupt the run."""
    from casa_browser.login import EXTENSION_NOTIFICATION, EXTENSION_ORIGIN
    server, _, session, page, proxy = driver(tmp_path)
    login = 'https://login.example'

    def failing(main):
        frame = PromptFrame(EXTENSION_NOTIFICATION + 'notification.html', main, EXTENSION_ORIGIN)

        async def evaluate_handle(script, arg):
            async def click(timeout=None):
                raise RuntimeError('Element is not attached to the DOM')
            return SimpleNamespace(as_element=lambda: SimpleNamespace(click=click))
        frame.evaluate_handle = evaluate_handle
        return frame
    prompt_page(page, login, [failing])
    page.on = lambda *a: None
    sid = await server.open_session('login', 'run', 'site', session, proxy, (login,))
    try:
        assert await driver_rpc(server, {'op': 'choose_passkey', 'driver_session': sid,
                                         'page_generation': session.view_generation,
                                         'title': 'Item', 'username': 'user'}) == \
            {'error': 'page_changed'}
    finally:
        await server.close_session(sid)


@pytest.mark.parametrize('where', ['origin', 'lookup'])
async def test_a_prompt_frame_replaced_during_the_selection_is_a_page_change(tmp_path, where):
    """r4 Astra: the frame's origin read or the button lookup failing under a re-render is
    page_changed too, never an error that would interrupt the run; nothing is clicked."""
    from casa_browser.login import EXTENSION_NOTIFICATION, EXTENSION_ORIGIN
    server, _, session, page, proxy = driver(tmp_path)
    login = 'https://login.example'

    def replaced(main):
        frame = PromptFrame(EXTENSION_NOTIFICATION + 'notification.html', main, EXTENSION_ORIGIN)
        if where == 'origin':
            original = frame.evaluate

            async def evaluate(script, arg=None):
                if script == '() => location.origin':
                    raise RuntimeError('Frame was detached')
                return await original(script, arg)
            frame.evaluate = evaluate
        else:
            async def evaluate_handle(script, arg):
                raise RuntimeError('Execution context was destroyed')
            frame.evaluate_handle = evaluate_handle
        return frame
    prompt_page(page, login, [replaced])
    page.on = lambda *a: None
    sid = await server.open_session('login', 'run', 'site', session, proxy, (login,))
    try:
        assert await driver_rpc(server, {'op': 'choose_passkey', 'driver_session': sid,
                                         'page_generation': session.view_generation,
                                         'title': 'Item', 'username': 'user'}) == \
            {'error': 'page_changed'}
        assert page.frames[1].clicks == []
    finally:
        await server.close_session(sid)
