"""Broker login bounds, private transport and service bootstrap checks."""
import asyncio
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from brokerfakes import Rig, Step, canary
from casa_browser.broker import BrokerError, MAX_LINE
from casa_browser.broker.items import Vault
from casa_browser.broker.main import BrokerServer, DriverClient
from casa_browser.broker_client import BrokerClient
from casa_browser.credential_rpc import UnixServer, encode
from casa_browser.login import LoginFields
from casa_browser.store import Store

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('op', ['get', 'create', 'put'])
async def test_builtin_sdk_timeout_is_fixed_code(tmp_path, op):
    r = Rig(tmp_path)
    value = canary()
    failure = TimeoutError(value)
    if op == 'get':
        r.sdk.read_failure = failure
    else:
        r.sdk.fail = failure
    with pytest.raises(BrokerError) as caught:
        if op == 'get':
            await r.broker.vault.get('item')
        elif op == 'create':
            await r.broker.vault.create_login('Synthetic', value, value, ['https://login.example'])
        else:
            await r.broker.vault.update_fields('item', 1, password=value)
    assert caught.value.args == ('timeout',)
    assert value not in repr(caught.value)
    r.journal.close()


@pytest.mark.parametrize('state,expected,operation', [
    ('signed_out', 'unlocked', 'guest_sign_in'), ('locked', 'unlocked', 'guest_unlock'),
    ('unlocked', 'unlocked', None), ('unknown', 'needs_live_view', None),
    ('two_factor', 'needs_live_view', None),
])
async def test_guest_bootstrap_routes_only_driver_calls(tmp_path, state, expected, operation):
    r = await Rig(tmp_path).setup()
    r.driver.guest = state
    reply = await r.call('unlock_guest', run_id='run', site_id='site', driver_session='driver')
    assert reply['state'] == expected
    assert [c[0] for c in r.driver.calls] == ['guest_state'] + ([operation] if operation else [])
    assert r.writes() == []
    assert not any(value in json.dumps(reply) for value in (
        r.options.guest_email, r.options.guest_password, r.options.guest_secret_key))
    r.journal.close()


async def test_login_rejection_does_not_repeat_values(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('password',)), Step(('password',), field_error=True)]
    assert await r.call('login', run_id='run', site_id='site', driver_session='driver') == {
        'status': 'failed', 'reason': 'rejected'}
    assert len(r.driver.fills) == 1
    r.journal.close()


async def test_login_step_and_clock_bounds(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username',))]
    assert (await r.call('login', run_id='run', site_id='site', driver_session='driver'))['reason'] == 'step_limit'
    assert len(r.driver.fills) == 6
    original = r.driver.submit
    async def submit(**args):
        r.clock.advance(121)
        return await original(**args)
    r.driver.submit = submit
    r.driver.fills.clear()
    assert (await r.call('login', run_id='run', site_id='site', driver_session='driver'))['reason'] == 'timeout'
    assert len(r.driver.fills) == 1
    r.journal.close()


async def test_site_lock_serializes_login_but_not_other_sites(tmp_path):
    r = await Rig(tmp_path).setup()
    await r.call('register_item', site_id='second', item_id='item',
                 login_origins=['https://login.example'], operator_ref='operator')
    await r.call('begin_run', run_id='run', account_site_ids=['site', 'second'])
    entered, release = asyncio.Event(), asyncio.Event()
    original = r.driver.settle
    calls = []
    async def settle(driver_session, **args):
        calls.append(driver_session)
        if driver_session == 'first':
            entered.set()
            await release.wait()
        return await original(driver_session, **args)
    r.driver.settle = settle
    r.driver.steps = [Step()]
    first = asyncio.create_task(r.call('login', run_id='run', site_id='site', driver_session='first'))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(r.call('login', run_id='run', site_id='site', driver_session='second'))
    try:
        await asyncio.wait_for(r.call('login', run_id='run', site_id='second', driver_session='other'), 1)
        # (a field-free page is read a few more times for a late form, slice 5u: 'other' may
        # settle again; 'second', the same site's, never before 'first' is released)
        assert calls[0] == 'first' and set(calls[1:]) == {'other'}
        assert not second.done()
    finally:
        release.set()
        await asyncio.gather(first, second)
        r.journal.close()


@dataclass
class Endpoint(UnixServer):
    source: object
    path: Path
    allowed_uid: int = os.getuid()

    async def handle(self, reader, writer):
        await self.serve(reader, writer, self.dispatch)

    async def dispatch(self, request):
        reply = await self.source.call(request['op'], **{k: v for k, v in request.items() if k != 'op'})
        return asdict(reply) if isinstance(reply, LoginFields) else reply


async def test_login_drives_only_rpc_and_reply_has_no_credentials(tmp_path):
    r = await Rig(tmp_path).setup()
    endpoint = Endpoint(r.driver, tmp_path / 'driver.sock')
    await endpoint.start()
    server = BrokerServer(r.broker, tmp_path / 'broker.sock', allowed_uid=os.getuid())
    await server.start()
    try:
        r.broker.driver = DriverClient(endpoint.path)
        client = BrokerClient(server.path)
        assert await client.login('run', 'site', 'driver') == {'status': 'logged_in', 'reason': 'completed'}
        assert len(r.driver.fills) == 3
        assert stat.S_IMODE(server.path.stat().st_mode) == 0o660
    finally:
        await server.close()
        await endpoint.close()
        r.journal.close()


@pytest.mark.parametrize('payload', [
    b'not json\n', b'[]\n', b'{"op":"status","other":1}\n',
    b'{"op":"begin_run","run_id":true,"account_site_ids":[]}\n',
    b'{"op":"register_capture","site_id":"site","login_origins":1,"operator_ref":"operator"}\n',
    b'{"op":"register_capture","site_id":"site","login_origins":["https://login.example"],"operator_ref":"operator","title":"a\\nb"}\n',
    b'{"op":"register_capture","site_id":"site","login_origins":["https://login.example"],"operator_ref":"operator","title":""}\n',
    b'x' * MAX_LINE + b'\n',
])
async def test_rpc_rejects_malformed_or_oversized_requests(tmp_path, payload):
    r = Rig(tmp_path)
    server = BrokerServer(r.broker, tmp_path / 'broker.sock', allowed_uid=os.getuid())
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.path))
        writer.write(payload)
        await writer.drain()
        assert json.loads(await asyncio.wait_for(reader.readline(), 1)) == {'error': 'unsupported'}
        writer.close()
        await writer.wait_closed()
        assert r.sdk.calls == []
    finally:
        await server.close()
        r.journal.close()


async def test_rpc_bounds_replies_and_client_requests(tmp_path):
    r = Rig(tmp_path)
    async def large(request):
        return {'large': 'x' * MAX_LINE}
    r.broker.dispatch = large
    server = BrokerServer(r.broker, tmp_path / 'broker.sock', allowed_uid=os.getuid())
    await server.start()
    try:
        client = BrokerClient(server.path)
        assert await client.status() == {'error': 'unsupported'}
        assert await client.call('unknown', value='x' * MAX_LINE) == {'error': 'unsupported'}
    finally:
        await server.close()
        r.journal.close()


async def test_notice_import_replays_with_broker_dedupe_key(tmp_path):
    r = await Rig(tmp_path).setup()
    # Seed durable metadata directly; capture and save are independent of importing.
    r.journal.begin('save', 'live', 'site', 'run')
    r.journal.transition('save', 'attempt')
    r.journal.recover()          # an unresolved save: one durable warning
    server = BrokerServer(r.broker, tmp_path / 'broker.sock', allowed_uid=os.getuid())
    await server.start()
    store = Store(tmp_path / 'controller.db')
    try:
        client = BrokerClient(server.path, store=store)
        await client.start()
        await client.import_notices()
        notices = store.notices_after(0)
        assert len(notices) == 1
        assert notices[0].dedupe_key == 'broker:1'
        assert notices[0].kind == 'credential_unknown'
        assert '{display_name}' not in notices[0].text_masked
    finally:
        await server.close()
        store.close()
        r.journal.close()


async def test_notice_pages_fit_wire_bound(tmp_path):
    r = Rig(tmp_path)
    for index in range(500):
        op_id = str(index)
        r.journal.begin(op_id, 'live', 'site', 'run')
        r.journal.transition(op_id, 'attempt')
    r.journal.recover()          # 500 unresolved saves: 500 durable warnings
    first = await r.broker.pending_notices(0)
    assert len(encode(first)) <= MAX_LINE
    assert 0 < len(first['notices']) < 500
    second = await r.broker.pending_notices(first['notices'][-1]['notice_key'])
    assert len(first['notices']) + len(second['notices']) == 500
    r.journal.close()


async def test_broker_s6_environment_and_dependencies():
    root = Path(__file__).parents[1] / 'rootfs/etc/s6-overlay'
    unit = root / 's6-rc.d/svc-browser-broker'
    raw = (unit / 'run').read_bytes()
    assert b'\r' not in raw
    assert raw.startswith(b'#!/command/with-contenv bash\n')
    script = raw.decode()
    env = script.split('exec env -i ', 1)[1].split('s6-setuidgid', 1)[0]
    assert set(re.findall(r'\b([A-Za-z_]+)=', env)) == {
        'PATH', 'LANG', 'TZ', 'HOME', 'PYTHONPATH', 'HTTPS_PROXY', 'https_proxy', 'SSL_CERT_FILE'}
    assert 'HTTPS_PROXY=http://127.0.0.1:3129' in env
    assert 'https_proxy=http://127.0.0.1:3129' in env
    assert 'SSL_CERT_FILE=/run/casa-browser/ca/ca.pem' in env
    assert 'HOME=/run/casa-browser/broker' in env
    assert 'while [ ! -s /run/casa-browser/ca/ca.pem ]' in script
    assert 's6-setuidgid casa-broker /opt/venv/bin/python -m casa_browser.broker.main' in script
    assert (unit / 'run').stat().st_mode & stat.S_IXUSR
    assert (unit / 'type').read_text() == 'longrun\n'
    assert {p.name for p in (unit / 'dependencies.d').iterdir()} == {
        'init-browser-netfloor', 'svc-browser-proxy'}
    assert (root / 'user-bundles.d/user/contents.d/svc-browser-broker').exists()


async def test_sdk_authentication_is_lazy_and_token_stays_private(tmp_path, monkeypatch):
    import casa_browser.broker.items as items
    r = Rig(tmp_path)
    token = canary()
    calls = []
    class Client:
        @staticmethod
        async def authenticate(**args):
            calls.append(args)
            return r.sdk
    monkeypatch.setattr(items, 'Client', Client)
    vault = Vault(None, 'Synthetic vault', token)
    assert token not in repr(vault)
    assert calls == []
    await vault.get('item')
    await vault.get('item')
    assert calls == [dict(auth=token, integration_name='Casa Browser', integration_version='1.0')]
    r.journal.close()


async def test_expired_login_cannot_fill_after_settle(tmp_path):
    r = await Rig(tmp_path).setup()
    original = r.driver.settle
    async def settle(**args):
        result = await original(**args)
        r.clock.advance(121)
        return result
    r.driver.settle = settle
    assert await r.call('login', run_id='run', site_id='site', driver_session='driver') == {
        'status': 'failed', 'reason': 'timeout'}
    assert r.driver.fills == []
    assert [c[0] for c in r.driver.calls] == ['settle']
    r.journal.close()


async def test_rpc_timeout_and_exception_text_stay_fixed(tmp_path):
    r = Rig(tmp_path)
    entered = asyncio.Event()
    async def stall(request):
        entered.set()
        await asyncio.Event().wait()
    r.broker.dispatch = stall
    server = BrokerServer(r.broker, tmp_path / 'broker.sock', allowed_uid=os.getuid())
    await server.start()
    try:
        client = BrokerClient(server.path, timeout=0.05)
        assert await client.status() == {'error': 'timeout'}
        assert entered.is_set()
        secret = canary()
        async def failure(request):
            raise RuntimeError(secret)
        r.broker.dispatch = failure
        client.timeout = 1
        assert await client.status() == {'error': 'internal'}
    finally:
        await server.close()
        r.journal.close()


async def test_the_bounds_on_opening_a_capture_nest(monkeypatch):
    """Driver, then the broker's transport to it, then the controller's call: the driver answers
    first, so the op has let go of the session when its caller gives up, and a close needs no
    forced close (the 10 s client bound under a 30 s driver bound killed Chrome on casa-test)."""
    from casa_browser import broker_client, login
    from casa_browser.broker.main import DriverClient
    seen = {}

    async def call(path, bound, op, **_args):
        seen[op] = bound
        return {}
    monkeypatch.setattr(broker_client, 'rpc_call', call)
    await BrokerClient().begin_capture('r', 's', 'd', 'login')
    assert login._CAPTURE_OPEN_S < DriverClient().timeout < seen['begin_capture']
    assert login.CAPTURE_STOP_S + login._CAPTURE_DISPOSE_S < login._CLEANUP_TIMEOUT
