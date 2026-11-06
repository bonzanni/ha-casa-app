"""Credential broker boundary, SDK facade and recovery contracts."""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import pkgutil
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from onepassword.types import FileAttributes, ItemField, ItemFile, ItemSection, VaultAccess, Website

from brokerfakes import ALL_SDK_CALLS, FORBIDDEN_SDK_METHODS, FakeSDK, LOGIN, Rig, Step, canary, item_value, sdk_error
from casa_browser.broker import BrokerError
from casa_browser.broker.journal import Journal
from casa_browser.broker.guest import assess_guest
from casa_browser.broker.items import EXTRA_FIELDS_WARNING, Vault
from casa_browser.broker.main import BROKER_OPS, BrokerServer
from casa_browser.login import DRIVER_OPS, DriverServer

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('case', ['wrong_run', 'public_site', 'unknown_site', 'wrong_vault_argument'])
async def test_wrong_vault_or_run_site_refused(tmp_path, case):
    """Refusals are made before even a vault read."""
    r = await Rig(tmp_path).setup()
    args = dict(run_id='run', site_id='site', driver_session='driver')
    if case == 'wrong_run':
        args['run_id'] = 'other'
    elif case == 'public_site':
        await r.call('begin_run', run_id='public', account_site_ids=[])
        args['run_id'] = 'public'
    elif case == 'unknown_site':
        args['site_id'] = 'other'
    else:
        args['vault_id'] = 'other'
    reply = await r.call('login', **args)
    assert reply['error'] in ('refused', 'unsupported')
    assert r.sdk.calls == []
    assert r.driver.fills == []


async def test_guest_readonly_and_extra_fields_warning(tmp_path):
    """Only extra names cross the boundary; permission bit 1024 is not write access."""
    r = Rig(tmp_path)
    extra = canary()
    r.sdk.saved['vault', 'item'].fields.append(
        ItemField(id='extra', title='Recovery note', field_type='Concealed', value=extra))
    await r.broker.start()
    reply = await r.call('register_item', site_id='site', item_id='item', login_origins=list(LOGIN), operator_ref='operator')
    assert reply['extra_field_names'] == ['Recovery note']
    assert set(reply['roles_available']) == {'username', 'password', 'one_time_code'}
    assert extra not in json.dumps(reply)
    assert 'whole browsing vault' in EXTRA_FIELDS_WARNING
    # The accessor list alone decides (design §5.2: no confirmation): a read-only user is taken
    # as the guest; no read-only user means the guest can edit; no list is unknown.
    for permissions, expected in [(1072, 'readonly'), (1072 | 64, 'not_readonly')]:
        access = [VaultAccess(vault_uuid='vault', accessor_type='user', accessor_uuid='guest', permissions=permissions)]
        result = assess_guest(access)
        assert result.guest_role == expected
        assert (result.readonly_users, result.editable_users) == ((0, 1) if permissions & 64 else (1, 0))
    unknown = assess_guest(None)
    assert (unknown.guest_role, unknown.readonly_users, unknown.editable_users) == ('unknown', 0, 0)
    assert assess_guest([]).guest_role == 'not_readonly'


async def test_a_guest_that_can_edit_never_stops_a_login(tmp_path):
    """Design §5.2: the guest's role is a Setup warning only — a guest able to edit items
    neither stops the extension's unlock nor the item's sign-in."""
    r = Rig(tmp_path)
    r.sdk.accessors = [VaultAccess(vault_uuid='vault', accessor_type='user', accessor_uuid='guest',
                                   permissions=1072 | 64)]
    await r.setup()
    assert (await r.call('status'))['guest_role'] == 'not_readonly'
    r.driver.guest = 'locked'
    unlocked = await r.call('unlock_guest', run_id='run', site_id='site', driver_session='driver')
    assert unlocked.get('reason') != 'guest_unconfirmed'
    assert [c for c in r.driver.calls if c[0] in ('guest_unlock', 'guest_sign_in')]
    reply = await r.call('login', run_id='run', site_id='site', driver_session='driver')
    assert reply['status'] == 'logged_in'
    assert r.driver.fills


async def test_secret_never_filled_off_login_origin(tmp_path):
    """A driver reporting an off-origin step gets neither fill nor submit."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('password',), origin_ok=False)]
    reply = await r.call('login', run_id='run', site_id='site', driver_session='driver')
    assert reply['status'] == 'needs_live_view'
    assert r.driver.fills == []
    assert not [c for c in r.driver.calls if c[0] == 'submit']


@pytest.mark.parametrize('roles,submit', [(('password',), None), (('ambiguous',), 'submit'), ((), None)])
async def test_login_fields_ambiguous(tmp_path, roles, submit):
    """Ambiguity or a missing submit never falls through to another click."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(roles, submit=submit, on_site_origin=False)]
    reply = await r.call('login', run_id='run', site_id='site', driver_session='driver')
    assert reply['status'] == 'needs_live_view'
    assert r.driver.fills == []
    assert not [c for c in r.driver.calls if c[0] == 'submit']


async def test_totp_resolved_at_use(tmp_path):
    """Each OTP step reads a new code from details.content.code immediately at use."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('one_time_code',)), Step(('one_time_code',)), Step()]
    reply = await r.call('login', run_id='run', site_id='site', driver_session='driver')
    assert reply['status'] == 'logged_in'
    codes = [v for _, role, v in r.driver.fills if role == 'one_time_code']
    assert len(codes) == 2 and codes[0] != codes[1]
    assert codes == r.sdk.otp_reads[-2:]
    assert [c[0] for c in r.sdk.calls].count('items.get') >= 2
    assert r.writes() == []


@pytest.mark.parametrize('roles', [('new_password', 'confirm_password'),
                                  ('new_password',), ('password', 'new_password')])
async def test_password_change_page_goes_to_live_view(tmp_path, roles):
    """Forced, skippable and settings password changes never auto-fill or submit."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(roles)]
    reply = await r.call('login', run_id='run', site_id='site', driver_session='driver')
    assert (reply['status'], reply['reason']) == ('needs_live_view', 'password_change_page')
    assert r.driver.fills == []
    assert not [c for c in r.driver.calls if c[0] == 'submit']
    assert r.writes() == []


def sdk_client_attribute_names():
    """Discover the installed SDK surface, including instance-only transport attributes."""
    import onepassword
    names = set()
    for entry in pkgutil.iter_modules(onepassword.__path__, onepassword.__name__ + '.'):
        # Data models, errors and SDK self-tests are not client-side API classes.
        if entry.ispkg or entry.name.rsplit('.', 1)[-1] in {'types', 'errors', 'test_client'}:
            continue
        module = importlib.import_module(entry.name)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__:
                continue
            names.update(dir(cls))
            names.update(inspect.get_annotations(cls))
            # dir(class) omits attributes assigned only on authenticated instances.
            # Inspect their definitions without authenticating or starting a native core.
            for node in ast.walk(ast.parse(inspect.getsource(cls))):
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                    names.add(node.attr)
    assert names  # An empty discovery must never disable the guard.
    return names


async def test_no_delete_interface(tmp_path):
    """The facade, RPC table and AST admit no destructive or credential-generation route."""
    sdk_names = sdk_client_attribute_names()
    allowed = {'authenticate', 'items', 'vaults', 'get', 'create', 'put', 'list'}
    # TOTP is read from ItemField.details.content.code; secrets access is unnecessary.
    banned = {name.split('.')[-1] for name in FORBIDDEN_SDK_METHODS} - {'update'}
    banned.add('generate_password')
    dynamic = {'getattr', 'setattr', 'delattr', 'attrgetter', 'methodcaller',
               '__getattribute__', '__dict__', 'eval', 'exec', 'compile'}
    for names in (BROKER_OPS, DRIVER_OPS, [n for n in dir(Vault) if not n.startswith('_')]):
        assert not any(any(word in n for word in ('delete', 'archive', 'remove', 'purge', 'trash')) for n in names)
    root = Path(__file__).parents[1] / 'rootfs/opt/casa_browser'
    for path in [*(root / 'broker').rglob('*.py'), root / 'broker_client.py']:
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Attribute):
                names.append(node.attr)
                assert node.attr not in sdk_names or node.attr in allowed, (path, node.lineno, node.attr)
                assert node.attr not in dynamic, (path, node.lineno, node.attr)
                assert not (isinstance(node.value, ast.Attribute) and node.value.attr == 'vaults'
                            and node.attr in ('update', 'delete'))
            if isinstance(node, ast.Name):
                assert node.id not in dynamic, (path, node.lineno, node.id)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names.extend(n.name for n in node.names)
                modules = [n.name for n in node.names]
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module)
                assert path == root / 'broker/items.py' or not any(
                    n.split('.')[0] == 'onepassword' for n in modules), (path, node.lineno)
                assert not any(n.split('.')[0] == 'importlib' for n in modules), (path, node.lineno)
                assert not any(n.split('.')[-1] in dynamic for n in modules), (path, node.lineno)
            assert not any(n in banned or n.startswith(('revoke_', 'grant_')) for n in names)
    r = await Rig(tmp_path).setup()
    await r.call('login', run_id='run', site_id='site', driver_session='driver')
    assert not any(c[0] in FORBIDDEN_SDK_METHODS for c in ALL_SDK_CALLS)


async def test_forget_keeps_vault_item(tmp_path):
    """Forgetting removes the site's item link without making any SDK call."""
    r = await Rig(tmp_path).setup()
    before = r.sdk.saved['vault', 'item'].model_dump()
    await r.call('forget_site', site_id='site', operator_ref='operator')
    assert r.sdk.calls == []
    assert r.sdk.saved['vault', 'item'].model_dump() == before
    assert json.loads(r.broker.registry_path.read_text())['site']['item_id'] is None


async def test_forget_login_keeps_the_site_and_asks_again(tmp_path):
    """Slice 5: Forget login removes only the item link (and its unverified / rejected marks);
    the site stays registered with its login origins, so its next login is ``not_found`` —
    the 1Password question again — and never a refusal."""
    r = await Rig(tmp_path).setup()
    registry = json.loads((tmp_path / 'registry.json').read_text())
    registry['site'].update(unverified=True, rejected={'run_id': 'run', 'item_id': 'item'})
    (tmp_path / 'registry.json').write_text(json.dumps(registry))
    await r.restart()
    assert await r.call('forget_site', site_id='site', operator_ref='operator') == {}
    entry = json.loads((tmp_path / 'registry.json').read_text())['site']
    assert entry == {'item_id': None, 'login_origins': list(LOGIN), 'operator_ref': 'operator'}
    await r.call('begin_run', run_id='run2', account_site_ids=['site'])
    assert await r.call('login', run_id='run2', site_id='site', driver_session='driver') == \
        {'status': 'needs_live_view', 'reason': 'not_found'}
    assert (await r.call('list_login_items', run_id='run2', site_id='site'))['items']


@pytest.mark.parametrize('socket_kind', ['broker', 'driver'])
async def test_rpc_peer_uid_checked(tmp_path, socket_kind):
    """Both real Unix sockets admit the configured uid and refuse a different uid."""
    r = Rig(tmp_path)
    path = tmp_path / 'rpc.sock'
    server = (BrokerServer(r.broker, path, allowed_uid=os.getuid() + 1) if socket_kind == 'broker'
              else DriverServer(SimpleNamespace(session=None), path, allowed_uid=os.getuid() + 1))
    async def request():
        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            writer.write(json.dumps({'op': 'status'} if socket_kind == 'broker' else
                                    {'op': 'guest_state', 'driver_session': 'wrong'}).encode() + b'\n')
            await writer.drain()
            return await asyncio.wait_for(reader.readline(), 1)
        finally:
            writer.close()
            await writer.wait_closed()
    await server.start()
    try:
        assert await request() in (b'', b'{"error": "refused"}\n')
        assert r.sdk.calls == [] and r.driver.calls == []
        server.allowed_uid = os.getuid()
        reply = json.loads(await request())
        if socket_kind == 'broker':
            assert 'configured' in reply
        else:
            assert reply == {'error': 'refused'}
    finally:
        await server.close()


async def test_vault_update_preserves_unrelated_fields(tmp_path):
    """A field update preserves every unrelated SDK item component and uses a version."""
    sdk = FakeSDK()
    before = sdk.saved['vault', 'item'].model_copy(deep=True)
    before.fields = [f for f in before.fields if f.field_type != 'Totp']
    before.sections = [ItemSection(id='section', title='Extra section')]
    before.websites = [Website(url='https://site.example', label='Account', autofill_behavior='ExactDomain')]
    before.files = [ItemFile(attributes=FileAttributes(name='attachment.txt', id='file', size=7), section_id='section', field_id='attachment')]
    before.notes, before.tags = canary(), ['synthetic']
    before.fields.append(ItemField(id='extra', title='Extra', field_type='Concealed', value=canary()))
    sdk.saved['vault', 'item'] = before.model_copy(deep=True)
    replacement = canary()
    await Vault(sdk, 'Synthetic vault').update_fields('item', 1, password=replacement)
    after = sdk.saved['vault', 'item']
    assert item_value(after, 'password') == replacement
    expected = before.model_copy(deep=True)
    next(f for f in expected.fields if f.id == 'password').value = replacement
    expected.version = after.version
    assert after.model_dump() == expected.model_dump()
    assert [c[0] for c in sdk.calls][-2:] == ['items.get', 'items.put']


@pytest.mark.parametrize('permissions', [128, 64, 256, 512, 2097152, 2])
async def test_guest_each_write_permission_is_editable(permissions):
    access = VaultAccess(vault_uuid='vault', accessor_type='user',
                         accessor_uuid='guest', permissions=permissions)
    result = assess_guest([access])
    assert (result.guest_role, result.readonly_users, result.editable_users) == ('not_readonly', 0, 1)


async def test_vault_stale_version_rereads_without_put():
    sdk = FakeSDK()
    vault = Vault(sdk, 'Synthetic vault')
    observed = await vault.get('item')
    sdk.saved['vault', 'item'].version += 1
    sdk.calls.clear()
    with pytest.raises(BrokerError) as error:
        await vault.update_fields('item', observed.version, password=canary())
    assert error.value.code == 'conflict'
    assert sdk.calls == [('items.get', 'vault', 'item'), ('items.get', 'vault', 'item')]


@pytest.mark.parametrize('count', [0, 2])
async def test_vault_resolution_requires_exactly_one_match(count):
    sdk = FakeSDK()
    sdk.vault_count = count
    with pytest.raises(BrokerError) as error:
        await Vault(sdk, 'Synthetic vault').get('item')
    assert error.value.code == 'refused'
    assert sdk.calls == [('vaults.list',)]


@pytest.mark.parametrize('identity', [('other', 'item'), ('vault', 'other')])
async def test_vault_rejects_returned_item_identity(identity):
    sdk = FakeSDK()
    sdk.returned_identity = identity
    with pytest.raises(BrokerError) as error:
        await Vault(sdk, 'Synthetic vault').get('item')
    assert error.value.code == 'refused'
    assert not any(c[0] == 'items.put' for c in sdk.calls)


@pytest.mark.parametrize('configured', [False, True])
async def test_status_unconfigured_or_sdk_unavailable(tmp_path, configured):
    from dataclasses import replace
    r = Rig(tmp_path)
    r.broker.options = replace(r.options, op_token=r.options.op_token if configured else '')
    r.sdk.vault_failure = sdk_error('Internal')
    await r.broker.start()
    reply = await r.call('status')
    assert reply == dict(configured=configured, sdk='unavailable' if configured else 'unconfigured',
                         guest_role='unknown', readonly_users=0, editable_users=0)
    assert r.sdk.calls == ([('vaults.list',)] if configured else [])


@pytest.mark.parametrize('op', ['get', 'create', 'put'])
@pytest.mark.parametrize('name,message,code', [
    ('ItemNotFound', None, 'not_found'),
    ('PermissionDenied', None, 'refused'),
    ('ItemUpdateFailure', 'itemStatusPermissionError: ', 'refused'),
    ('ItemUpdateFailure', 'itemStatusIncorrectItemVersion: ', 'conflict'),
    ('Internal', 'permission denied; incorrect item version; not found: ', 'unavailable'),
    ('Internal', 'prefixitemStatusPermissionErrorsuffix: ', 'unavailable'),
])
async def test_sdk_error_bridge_maps_fixed_codes(op, name, message, code):
    sdk = FakeSDK()
    secret = canary()
    failure = sdk_error(name, (message or '') + secret)
    assert type(failure) is Exception
    assert failure.args == ((message or '') + secret,)
    assert json.loads(failure.__context__.msg)['name'] == name
    vault = Vault(sdk, 'Synthetic vault')
    if op == 'get':
        sdk.read_failure = failure
    else:
        sdk.fail = failure
    with pytest.raises(BrokerError) as caught:
        if op == 'get':
            await vault.get('item')
        elif op == 'create':
            await vault.create_login('Synthetic', canary(), canary(), LOGIN)
        else:
            await vault.update_fields('item', 1, password=canary())
    assert caught.value.code == code
    assert caught.value.args == (code,)
    assert secret not in str(caught.value) + repr(caught.value)


async def test_fake_sdk_version_conflict_uses_real_bridge():
    sdk = FakeSDK()
    sdk.before_write = lambda: setattr(sdk.saved['vault', 'item'], 'version', 2)
    with pytest.raises(BrokerError) as error:
        await Vault(sdk, 'Synthetic vault').update_fields('item', 1, password=canary())
    assert error.value.code == 'conflict'
    assert sdk.history == []


async def test_journal_synchronous_full(tmp_path):
    journal = Journal(tmp_path / 'journal.db')
    try:
        assert journal._db.execute('PRAGMA synchronous').fetchone()[0] == 2
    finally:
        journal.close()


@pytest.mark.parametrize('mode', ['DELETE', 'WAL'])
async def test_broker_state_files_private_under_permissive_umask(tmp_path, mode):
    import stat
    old = os.umask(0)
    r = None
    try:
        r = await Rig(tmp_path).setup()
        assert stat.S_IMODE(r.broker.registry_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(r.journal.path.stat().st_mode) == 0o600
        assert r.journal._db.execute('PRAGMA journal_mode=' + mode).fetchone()[0].upper() == mode
        r.journal._db.execute("INSERT INTO controls VALUES ('mode-test', 'operator')")
        suffixes = ['-journal'] if mode == 'DELETE' else ['-wal', '-shm']
        for suffix in suffixes:
            path = Path(str(r.journal.path) + suffix)
            assert path.exists()
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        r.journal._db.commit()
    finally:
        if r is not None:
            r.journal.close()
        os.umask(old)


@pytest.mark.parametrize('failure,expected', [
    ('permission', 'vault_failed'), ('version', 'vault_failed'),
    ('transport', 'vault_unknown'),
])
async def test_sdk_write_failure_journal_classification(tmp_path, failure, expected):
    sdk = FakeSDK()
    journal = Journal(tmp_path / 'journal.db')
    journal.begin('save', 'manual', 'site', 'run', item_id='item', item_version_before=1)
    journal.transition('save', 'attempt')
    before = sdk.saved['vault', 'item'].model_dump()
    if failure == 'permission':
        sdk.fail = 'refuse'
    elif failure == 'version':
        sdk.before_write = lambda: setattr(sdk.saved['vault', 'item'], 'version', 2)
    else:
        sdk.fail = 'raise_before'
    try:
        with pytest.raises(BrokerError) as caught:
            await Vault(sdk, 'Synthetic vault').update_fields('item', 1, password=canary())
        journal.write_failed('save', caught.value)
        assert journal.rows()[0]['state'] == expected
        independent = Journal(journal.path)
        try:
            assert independent.rows()[0]['state'] == expected
        finally:
            independent.close()
        assert sdk.history == []
        after = sdk.saved['vault', 'item'].model_dump()
        after['version'] = before['version']
        assert after == before
    finally:
        journal.close()


@pytest.mark.parametrize('message,code', [
    ('ItemNotFound: ', 'not_found'),
    ('itemStatusPermissionError: ', 'refused'),
    ('itemStatusIncorrectItemVersion: ', 'conflict'),
    ('permission denied: ', 'unavailable'),
    ('ItemNotFound itemStatusPermissionError: ', 'unavailable'),
])
async def test_sdk_message_fallback_uses_only_stable_type_tokens(message, code):
    sdk = FakeSDK()
    sdk.read_failure = Exception(message + canary())
    with pytest.raises(BrokerError) as caught:
        await Vault(sdk, 'Synthetic vault').get('item')
    assert caught.value.args == (code,)


async def test_fake_sdk_missing_item_uses_real_bridge():
    sdk = FakeSDK()
    with pytest.raises(Exception) as caught:
        await sdk.items.get('vault', 'absent')
    assert type(caught.value) is Exception
    assert json.loads(caught.value.__context__.msg)['name'] == 'ItemNotFound'
    with pytest.raises(BrokerError) as mapped:
        await Vault(sdk, 'Synthetic vault').get('absent')
    assert mapped.value.code == 'not_found'


async def test_existing_registry_mode_is_private(tmp_path):
    import stat
    registry = tmp_path / 'registry.json'
    registry.write_text('{}')
    registry.chmod(0o666)
    r = Rig(tmp_path)
    try:
        await r.broker.start()
        assert stat.S_IMODE(registry.stat().st_mode) == 0o600
    finally:
        r.journal.close()


# -- slice 5: vault listing and the automatic save (plan S5-1, S5-6) -------------------------

def _overview_item(r, item_id, title, category='Login', websites=()):
    from brokerfakes import make_item
    item = make_item(item_id)
    item.title, item.category = title, category
    item.websites = [Website(url=u, label='', autofill_behavior='AnywhereOnWebsite') for u in websites]
    r.sdk.saved['vault', item_id] = item
    return item


async def test_list_login_items_only_login_and_password_active_no_values(tmp_path):
    """Only the browser vault's active Login and Password items, as metadata: id, title and
    website hosts — never a field value, and only for an account site of the run."""
    r = await Rig(tmp_path).setup()
    _overview_item(r, 'pw', 'RunPod', 'Password', ['https://www.runpod.io/console'])
    _overview_item(r, 'note', 'RunPod recovery', 'SecureNote')
    _overview_item(r, 'old', 'RunPod (old)')
    _overview_item(r, 'bare', 'Ziggo', websites=['mijn.ziggo.nl'])
    r.sdk.archived.add('old')
    secrets_ = [f.value for i in r.sdk.saved.values() for f in i.fields]
    reply = await r.call('list_login_items', run_id='run', site_id='site')
    assert reply['truncated'] is False
    got = {i['item_id']: i for i in reply['items']}
    assert set(got) == {'item', 'pw', 'bare'}
    assert got['pw'] == {'item_id': 'pw', 'title': 'RunPod', 'website_hosts': ['www.runpod.io']}
    assert got['bare']['website_hosts'] == ['mijn.ziggo.nl']    # no scheme: read as https
    assert all(v not in json.dumps(reply) for v in secrets_)
    assert [c for c in r.sdk.calls if c[0] == 'items.list'] == [('items.list', 'vault', 1)]
    for run, site in (('other', 'site'), ('run', 'other')):
        assert (await r.call('list_login_items', run_id=run, site_id=site)) == {'error': 'refused'}


async def test_list_login_items_fits_one_broker_line(tmp_path):
    r = await Rig(tmp_path).setup()
    for n in range(800):
        _overview_item(r, f'i{n}', 'x' * 200, websites=[f'https://h{k}.example' for k in range(12)])
    reply = await r.call('list_login_items', run_id='run', site_id='site')
    assert reply['truncated'] is True and 0 < len(reply['items']) < 801
    assert all(i['title'] == 'x' * 200 and len(i['website_hosts']) == 10
               for i in reply['items'] if i['item_id'] != 'item')
    assert len(json.dumps(reply)) <= 60_000


async def test_a_title_is_listed_whole_or_not_at_all(tmp_path):
    """d1 Astra A: a title cut short could hide what tells two accounts apart; a title too
    long to show whole leaves its item out."""
    r = await Rig(tmp_path).setup()
    _overview_item(r, 'work', 'R' * 200 + ' WORK')
    _overview_item(r, 'huge', 'R' * 301)
    got = {i['item_id']: i['title'] for i in
           (await r.call('list_login_items', run_id='run', site_id='site'))['items']}
    assert got['work'] == 'R' * 200 + ' WORK' and 'huge' not in got


ATTEMPT = None


def _attempt(username, password, failed=False, typed_after=False):
    return {'username': username, 'password': password, 'failed': failed,
            'typed_after': typed_after}


async def _judging(r, attempt, values=()):
    """A live view's capture taken for judgement at Done."""
    r.driver.values = list(values)
    r.driver.attempt = attempt
    await r.call('begin_capture', run_id='run', site_id='site', driver_session='driver',
                 purpose='login')
    return await r.call('take_attempt', run_id='run', site_id='site', capture_id='capture')


async def test_take_attempt_returns_flags_never_values(tmp_path):
    r = await Rig(tmp_path).setup(item=False)
    user, password = canary(), canary()
    taken = await _judging(r, _attempt(user, password, typed_after=True))
    assert taken == {'attempt': {'failed': False, 'typed_after': True}, 'incomplete': False}
    assert (await _judging_again(r)) == {'attempt': {'failed': False, 'typed_after': True},
                                         'incomplete': False}
    assert user not in json.dumps(r.replies) and password not in json.dumps(r.replies)


async def _judging_again(r):
    return await r.call('take_attempt', run_id='run', site_id='site', capture_id='capture')


async def test_save_auto_creates_unverified(tmp_path):
    """No linked item: a new Login item titled with the site's name, the login origins as its
    websites, linked and marked unverified; consent ``auto:<capture>``; no broker notice (Fido
    says it)."""
    r = await Rig(tmp_path).setup(item=False)
    user, password = canary(), canary()
    await _judging(r, _attempt(user, password))
    reply = await r.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                         title='RunPod')
    assert reply['saved'] is True and reply['item_id'].startswith('created_')
    item = r.sdk.saved['vault', reply['item_id']]
    assert (item.title, item_value(item, 'username'), item_value(item, 'password')) == \
        ('RunPod', user, password)
    assert [w.url for w in item.websites] == list(LOGIN)
    registry = json.loads((tmp_path / 'registry.json').read_text())['site']
    assert registry['item_id'] == reply['item_id'] and registry['unverified'] is True
    ops = [row for row in r.journal.rows() if 'op_id' in row]
    assert [o['consent_id'] for o in ops] == ['auto:capture']
    assert not [n for n in r.journal.pending_notices(0) if n['kind'] == 'credential_saved']
    assert (await r.call('take_attempt', run_id='run', site_id='site', capture_id='capture')) \
        == {'error': 'refused'}                                  # the capture is gone


@pytest.mark.parametrize('flag', ['failed', 'typed_after', 'none', 'not_judging'])
async def test_save_attempt_refuses_what_the_rules_exclude(tmp_path, flag):
    r = await Rig(tmp_path).setup(item=False)
    attempt = _attempt(canary(), canary(), failed=flag == 'failed',
                       typed_after=flag == 'typed_after')
    if flag == 'not_judging':
        r.driver.attempt = attempt
        await r.call('begin_capture', run_id='run', site_id='site', driver_session='driver',
                     purpose='login')
        # Taken into broker memory without Done's take (``end_capture``/``end_run`` review
        # this way): never held for judgement.
        await r.broker._review_capture('run', 'site', 'capture')
    else:
        await _judging(r, None if flag == 'none' else attempt)
    reply = await r.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                         title='RunPod')
    assert reply == {'error': 'refused'} and r.writes() == []


@pytest.mark.parametrize('case', ['plain', 'rejected_other_run', 'rejected', 'unverified'])
async def test_overwrite_only_after_rejection_or_unverified(tmp_path, case):
    """A linked item is written only when its own password was rejected in this run, or it
    is still unverified; otherwise nothing is written and the reply says the item was kept."""
    r = await Rig(tmp_path).setup()
    before = item_value(r.sdk.saved['vault', 'item'], 'password')
    if case in ('rejected', 'rejected_other_run'):
        run = 'run' if case == 'rejected' else 'earlier'
        if run != 'run':
            await r.call('begin_run', run_id=run, account_site_ids=['site'])
        r.driver.steps = [Step(('username', 'password')), Step(('username', 'password'), field_error=True)]
        reply = await r.call('login', run_id=run, site_id='site', driver_session='driver')
        assert reply == {'status': 'failed', 'reason': 'rejected'}
        r.driver.index = 0
    elif case == 'unverified':
        registry = json.loads((tmp_path / 'registry.json').read_text())
        registry['site']['unverified'] = True
        (tmp_path / 'registry.json').write_text(json.dumps(registry))
        await r.restart()
        await r.call('begin_run', run_id='run', account_site_ids=['site'])
    r.sdk.calls.clear()
    user, password = canary(), canary()
    await _judging(r, _attempt(user, password))
    reply = await r.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                         title='Synthetic site')
    item = r.sdk.saved['vault', 'item']
    if case in ('rejected', 'unverified'):
        assert reply['saved'] is True and reply['item_id'] == 'item'
        assert (item_value(item, 'username'), item_value(item, 'password')) == (user, password)
    else:
        assert reply == {'saved': False, 'reason': 'item_kept', 'item_id': 'item'}
        assert r.writes() == [] and item_value(item, 'password') == before


async def test_unverified_cleared_on_next_login(tmp_path):
    r = await Rig(tmp_path).setup(item=False)
    await _judging(r, _attempt(canary(), canary()))
    saved = await r.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                         title='RunPod')
    assert json.loads((tmp_path / 'registry.json').read_text())['site']['unverified'] is True
    r.driver.steps = [Step(('username', 'password')), Step()]
    r.driver.index = 0
    assert (await r.call('login', run_id='run', site_id='site', driver_session='driver')) == \
        {'status': 'logged_in', 'reason': 'completed'}
    registry = json.loads((tmp_path / 'registry.json').read_text())['site']
    assert registry['item_id'] == saved['item_id'] and not registry.get('unverified')


async def test_equal_values_write_nothing(tmp_path):
    r = await Rig(tmp_path).setup()
    item = r.sdk.saved['vault', 'item']
    registry = json.loads((tmp_path / 'registry.json').read_text())
    registry['site']['unverified'] = True                       # writable, yet equal: no write
    (tmp_path / 'registry.json').write_text(json.dumps(registry))
    await r.restart()
    await r.call('begin_run', run_id='run', account_site_ids=['site'])
    r.sdk.calls.clear()
    await _judging(r, _attempt(item_value(item, 'username'), item_value(item, 'password')))
    reply = await r.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                         title='Synthetic site')
    assert reply == {'saved': False, 'reason': 'equal', 'item_id': 'item'}
    assert r.writes() == []


async def test_an_unreadable_linked_item_at_the_save_warns_never_declines(tmp_path):
    """The linked item cannot be read when a ``signed_in`` verdict saves (vault unreachable):
    the login typed in the live view stays unsaved, and the operator is told so by a failed
    save's warning (``credential_unknown``), never by a silent decline."""
    r = await Rig(tmp_path).setup()
    user, password = canary(), canary()
    await _judging(r, _attempt(user, password), values=[('username', user), ('password', password)])
    r.sdk.calls.clear()
    r.sdk.read_failure = TimeoutError()
    reply = await r.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                         title='Synthetic site')
    assert reply == {'saved': False, 'reason': 'unknown', 'item_id': 'item'}
    assert r.writes() == []
    assert [n['kind'] for n in r.journal.pending_notices(0)] == ['credential_unknown']
    assert (await r.call('take_attempt', run_id='run', site_id='site', capture_id='capture')) \
        == {'error': 'refused'}                                  # the capture is gone


@pytest.mark.parametrize('end', ['drop_attempt', 'end_run'])
async def test_a_judging_capture_ends_unsaved_and_silent(tmp_path, end):
    """`drop_attempt` (not signed in) and the run's end before a verdict save nothing and
    warn about nothing; `end_capture` (the view's release) keeps a judging capture."""
    r = await Rig(tmp_path).setup(item=False)
    await _judging(r, _attempt(canary(), canary()), values=[('password', canary())])
    assert await r.call('end_capture', run_id='run', site_id='site', capture_id='capture') == {}
    if end == 'drop_attempt':
        assert await r.call('drop_attempt', run_id='run', site_id='site', capture_id='capture') == {}
    else:
        assert await r.call('end_run', run_id='run') == {}
        await r.call('begin_run', run_id='run', account_site_ids=['site'])
    assert (await r.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                         title='RunPod')) == {'error': 'refused'}
    assert r.writes() == [] and r.journal.pending_notices(0) == []


@pytest.mark.parametrize('rejected_step, outcome', [
    (('one_time_code',), {'status': 'failed', 'reason': 'rejected'}),
    # A username step after the password is not pressed at all (one send of the password).
    (('username',), {'status': 'needs_live_view', 'reason': 'password_again'})])
async def test_a_rejected_code_or_username_step_never_allows_an_overwrite(tmp_path, rejected_step, outcome):
    """d1 Astra B: only a step that sent the item's password (and no code) records the item as
    rejected; a rejected code or username leaves the verified item protected."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step(rejected_step),
                      Step(rejected_step, field_error=True)]
    reply = await r.call('login', run_id='run', site_id='site', driver_session='driver')
    assert reply == outcome
    assert 'rejected' not in json.loads((tmp_path / 'registry.json').read_text())['site']
    r.sdk.calls.clear()
    await _judging(r, _attempt(canary(), canary()))
    assert (await r.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                         title='x')) == {'saved': False, 'reason': 'item_kept', 'item_id': 'item'}
    assert r.writes() == []


# -- slice 6 (S6-2, S6-3): a sign-in code the site sent ------------------------------------------

def _no_totp(r):
    item = r.sdk.saved['vault', 'item']
    item.fields[:] = [f for f in item.fields if f.field_type != 'Totp']


async def _report(r, steps, at=0):
    """A login that stops on the code page at step ``at`` (the report marks that document),
    then the page is where the next calls find it (``r.driver.index = at``)."""
    _no_totp(r)
    r.driver.steps = steps
    r.driver.index = at
    reply = await r.call('login', run_id='run', site_id='site', driver_session='driver')
    assert reply['status'] == 'needs_code', reply
    r.driver.fills.clear()
    r.driver.calls.clear()
    return reply['page_generation']


async def test_otp_without_totp_is_needs_code(tmp_path):
    """A code step when the item holds no authenticator stops before anything is filled on it:
    the page stays as it is for the code the site sent."""
    r = await Rig(tmp_path).setup()
    _no_totp(r)
    r.driver.steps = [Step(('username', 'password')), Step(('one_time_code',)), Step()]
    reply = await r.call('login', run_id='run', site_id='site', driver_session='driver')
    assert reply == {'status': 'needs_code', 'code_field': True, 'totp_held': False,
                     'page_generation': 2}
    assert [(i, role) for i, role, _ in r.driver.fills] == [(0, 'username'), (0, 'password')]
    assert [c[0] for c in r.driver.calls].count('submit') == 1 and r.driver.index == 1


@pytest.mark.parametrize('roles', [('password', 'one_time_code'), ('username', 'one_time_code')])
async def test_a_code_beside_other_fields_is_still_missing_role(tmp_path, roles):
    """Only a step that asks for the code alone waits for one (S6-2); a code asked together with
    other fields goes to the live view as before."""
    r = await Rig(tmp_path).setup()
    _no_totp(r)
    r.driver.steps = [Step(roles), Step()]
    reply = await r.call('login', run_id='run', site_id='site', driver_session='driver')
    assert reply == {'status': 'needs_live_view', 'reason': 'missing_role'}
    assert r.driver.fills == []


async def test_fill_code_fills_found_field_and_submits(tmp_path):
    """The code goes into the one code field of the page that reported, the step is submitted,
    and the sign-in goes on with the item's values for a later step."""
    r = await Rig(tmp_path).setup()
    code = '48-15A'
    reads = list(r.sdk.otp_reads)
    gen = await _report(r, [Step(('one_time_code',)), Step(('password',)), Step()])
    reply = await r.call('fill_code', run_id='run', site_id='site', driver_session='driver2',
                         page_generation=gen, code=code)
    assert reply == {'status': 'logged_in', 'reason': 'completed'}
    assert r.driver.fills[0] == (0, 'one_time_code', code)
    assert r.driver.fills[1][:2] == (1, 'password')
    assert [c[0] for c in r.driver.calls].count('submit') == 2
    assert [c[0] for c in r.driver.calls][:2] == ['settle', 'document_marked']
    assert {c[1] for c in r.driver.calls} == {'driver2'}
    assert r.sdk.otp_reads == reads and r.writes() == []


@pytest.mark.parametrize('step', [Step(()), Step(('password',)), Step(('one_time_code',), origin_ok=False),
                                  Step(('one_time_code',), submit=None),
                                  Step(('one_time_code', 'password'))],
                         ids=['no-field', 'other-field', 'off-origin', 'no-submit', 'code-and-more'])
async def test_fill_code_refuses_when_the_code_field_is_gone(tmp_path, step):
    """The code is filled only into a step that asks for it alone, on the login origin, with
    one submit; anything else fills and submits nothing."""
    r = await Rig(tmp_path).setup()
    gen = await _report(r, [Step(('one_time_code',)), Step()])
    r.driver.steps = [step, Step()]                 # the same document, changed
    reply = await r.call('fill_code', run_id='run', site_id='site', driver_session='driver',
                         page_generation=gen, code='123456')
    assert reply == {'status': 'needs_live_view', 'reason': 'code_page_gone'}
    assert r.driver.fills == []
    assert not [c for c in r.driver.calls if c[0] == 'submit']


async def test_fill_code_second_code_step_is_needs_code_again(tmp_path):
    r = await Rig(tmp_path).setup()
    gen = await _report(r, [Step(('one_time_code',)), Step(('one_time_code',)), Step()])
    reply = await r.call('fill_code', run_id='run', site_id='site', driver_session='driver',
                         page_generation=gen, code='111111')
    assert reply == {'status': 'needs_code', 'code_field': True, 'totp_held': False,
                     'page_generation': 2}
    assert [v for _, _, v in r.driver.fills] == ['111111']
    assert r.driver.marks.get(1)                    # the second code page is marked in turn
    assert (await r.call('fill_code', run_id='run', site_id='site', driver_session='driver',
                         page_generation=2, code='222222')) == {'status': 'logged_in',
                                                               'reason': 'completed'}


async def test_fill_code_a_rejected_code_is_rejected_not_the_password(tmp_path):
    r = await Rig(tmp_path).setup()
    gen = await _report(r, [Step(('one_time_code',)), Step(('one_time_code',), field_error=True)])
    reply = await r.call('fill_code', run_id='run', site_id='site', driver_session='driver',
                         page_generation=gen, code='111111')
    assert reply == {'status': 'failed', 'reason': 'rejected'}
    assert 'rejected' not in json.loads((tmp_path / 'registry.json').read_text())['site']


@pytest.mark.parametrize('case', ['wrong_run', 'unknown_site', 'no_item', 'no_report'])
async def test_fill_code_refused_like_login(tmp_path, case):
    r = await Rig(tmp_path).setup(item=case != 'no_item')
    args = dict(run_id='run', site_id='site', driver_session='driver', page_generation=1,
                code='123456')
    if case == 'wrong_run':
        args['run_id'] = 'other'
    elif case == 'unknown_site':
        args['site_id'] = 'other'
    reply = await r.call('fill_code', **args)
    assert reply in ({'error': 'refused'},
                     {'status': 'needs_live_view', 'reason': 'not_found'},
                     {'status': 'needs_live_view', 'reason': 'code_page_gone'}), reply
    assert r.driver.fills == [] and r.driver.calls == []


@pytest.mark.parametrize('code,generation', [('123', 1), ('12345678901', 1), ('12 34', 1),
                                             ('1234\n', 1), (1234, 1), (None, 1),
                                             ('123456', -1), ('123456', '1'), ('123456', True)])
async def test_fill_code_bounds(tmp_path, code, generation):
    r = await Rig(tmp_path).setup()
    reply = await r.call('fill_code', run_id='run', site_id='site', driver_session='driver',
                         page_generation=generation, code=code)
    assert reply == {'error': 'unsupported'}
    assert r.driver.calls == []


@pytest.mark.parametrize('change', ['generation', 'other_document', 'used', 'new_login', 'run_ended'])
async def test_fill_code_only_into_the_page_that_asked(tmp_path, change):
    """Terra d2, Astra r1/r2: the code goes only into the very document that reported
    needs_code — the same generation and the mark the report set in that document; another
    tab, a reload, a used or replaced report, or the run's end gets nothing."""
    r = await Rig(tmp_path).setup()
    gen = await _report(r, [Step(('one_time_code',)), Step()])
    if change == 'generation':
        gen += 6
    elif change == 'other_document':
        r.driver.marks.clear()                      # another tab or a reload: no mark there
    elif change == 'used':
        await r.call('fill_code', run_id='run', site_id='site', driver_session='driver',
                     page_generation=gen, code='999999')
        r.driver.index = 0
        r.driver.fills.clear()
        r.driver.calls.clear()
    elif change == 'new_login':
        r.driver.steps = [Step()]
        # A new sign-in that finds no form claims nothing (slice 5u), and still replaces the
        # report.
        assert (await r.call('login', run_id='run', site_id='site', driver_session='driver')
                )['status'] == 'no_form'
        r.driver.steps = [Step(('one_time_code',)), Step()]
        r.driver.calls.clear()
    else:
        await r.call('end_run', run_id='run')
        await r.call('begin_run', run_id='run', account_site_ids=['site'])
    reply = await r.call('fill_code', run_id='run', site_id='site', driver_session='driver',
                         page_generation=gen, code='123456')
    assert reply == {'status': 'needs_live_view', 'reason': 'code_page_gone'}
    assert r.driver.fills == [] and not [c for c in r.driver.calls
                                         if c[0] in ('find_login_fields', 'fill', 'submit')]


async def test_a_code_page_that_cannot_be_marked_goes_to_the_live_view(tmp_path):
    r = await Rig(tmp_path).setup()
    _no_totp(r)
    r.driver.steps = [Step(('one_time_code',)), Step()]
    r.driver.mark_fails = True
    reply = await r.call('login', run_id='run', site_id='site', driver_session='driver')
    assert reply == {'status': 'needs_live_view', 'reason': 'missing_role'}
    assert r.driver.fills == []


async def test_code_never_logged_or_journaled(tmp_path, caplog):
    """The code reaches the page's field and nothing else the broker keeps or says: no log
    line, no journal row, no registry entry, no reply."""
    import logging
    caplog.set_level(logging.DEBUG)
    r = await Rig(tmp_path).setup()
    code = 'Q7-' + canary()[8:14]
    gen = await _report(r, [Step(('one_time_code',)), Step(('one_time_code',), field_error=True)])
    replies = [await r.call('fill_code', run_id='run', site_id='site', driver_session='driver',
                            page_generation=gen, code=code)]
    gen = await _report(r, [Step(('one_time_code',)), Step()])
    replies.append(await r.call('fill_code', run_id='run', site_id='site', driver_session='driver',
                                page_generation=gen, code=code))
    assert [f[2] for f in r.driver.fills] == [code]          # the page got it
    assert code not in json.dumps(replies) and code not in caplog.text
    assert code not in repr(r.broker)
    r.journal.close()
    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert code.encode() not in path.read_bytes(), path


# -- slice 5n (O2): an identifier-only sign-in; the vault listing for a sign-in card ----------

async def test_an_identifier_only_sign_in_saves_just_the_identifier(tmp_path):
    """O2: a live-view sign-in that sent only an email (an emailed code followed) saves a new
    item holding that email and no password field at all; the code is never in it."""
    r = await Rig(tmp_path).setup(item=False)
    email = 'it-' + canary()[8:16] + '@example.com'
    await _judging(r, _attempt(email, ''))
    reply = await r.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                         title='Enable Banking')
    assert reply['saved'] is True
    item = r.sdk.saved['vault', reply['item_id']]
    assert item.title == 'Enable Banking' and item_value(item, 'username') == email
    assert [f.id for f in item.fields] == ['username']
    assert [w.url for w in item.websites] == list(LOGIN)


async def test_an_identifier_only_sign_in_never_removes_a_linked_password(tmp_path):
    """A linked item that may be rewritten (unverified) gets only its username; its password
    stays; a linked item that may not is left as it is (item_kept), as for any attempt."""
    r = await Rig(tmp_path).setup()
    before = {f.id: f.value for f in r.sdk.saved['vault', 'item'].fields}
    await _judging(r, _attempt('new-' + canary()[8:14] + '@example.com', ''))
    kept = await r.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                        title='Site')
    assert kept == {'saved': False, 'reason': 'item_kept', 'item_id': 'item'}
    assert {f.id: f.value for f in r.sdk.saved['vault', 'item'].fields} == before
    r.broker._registry['site']['unverified'] = True
    user = 'new-' + canary()[8:14] + '@example.com'
    await _judging(r, _attempt(user, ''))
    saved = await r.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                         title='Site')
    assert saved['saved'] is True
    after = {f.id: f.value for f in r.sdk.saved['vault', 'item'].fields}
    assert after['username'] == user and after['password'] == before['password']


@pytest.mark.parametrize('raw', [_attempt('', ''), {'username': None, 'password': ''},
                                 {'username': '', 'password': None},
                                 _attempt('432198', ''), _attempt('alice', '')])   # r2: email only
async def test_an_attempt_with_neither_identifier_nor_password_is_none(tmp_path, raw):
    r = await Rig(tmp_path).setup(item=False)
    taken = await _judging(r, raw)
    assert taken['attempt'] is None
    reply = await r.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                         title='X')
    assert reply == {'error': 'refused'} and r.writes() == []


async def test_list_vault_logins_through_dispatch(tmp_path):
    """d1 Astra S2: the sign-in card's listing goes through the broker's own validation and
    dispatch with no site (before any site exists), metadata only."""
    r = await Rig(tmp_path).setup()
    _overview_item(r, 'eb', 'Enable Banking', websites=['https://enablebanking.com'])
    secrets_ = [f.value for i in r.sdk.saved.values() for f in i.fields]
    reply = await r.call('list_vault_logins', run_id='run_open_web')
    got = {i['item_id']: i for i in reply['items']}
    assert got['eb'] == {'item_id': 'eb', 'title': 'Enable Banking',
                         'website_hosts': ['enablebanking.com']}
    assert all(v not in json.dumps(reply) for v in secrets_)
    assert (await r.call('list_vault_logins', run_id='run', site_id='site')) == \
        {'error': 'unsupported'}


# --------------------------------------------------------------------------- slice 5u

class _Moving:
    """Wraps the rig's FakeDriver: ``plan`` maps (op, n-th call of that op) to what happens —
    'changed' raises page_changed, a callable runs first (it may change the steps)."""
    def __init__(self, inner, plan):
        self.inner, self.plan, self.count = inner, plan, {}

    async def call(self, op, **args):
        n = self.count[op] = self.count.get(op, 0) + 1
        what = self.plan.get((op, n))
        if what == 'changed':
            raise BrokerError('page_changed')
        if callable(what):
            what()
        return await self.inner.call(op, **args)


def _moving(r, plan):
    r.broker.driver = _Moving(r.driver, plan)
    return r.broker.driver


async def _login(r):
    return await r.call('login', run_id='run', site_id='site', driver_session='driver')


@pytest.mark.parametrize('where', [('settle', 1), ('find_login_fields', 1), ('fill', 1),
                                   ('submit', 2), ('fill', 3)])
async def test_a_page_change_reads_the_page_again_and_goes_on(tmp_path, where):
    """Slice 5u (casa-test run_25cb): a page that changed under a step is settled and read
    again, and the sign-in goes on; the restart takes none of the six steps, and every value
    goes only into fields of the read after it."""
    r = await Rig(tmp_path).setup()
    _moving(r, {where: 'changed'})
    assert await _login(r) == {'status': 'logged_in', 'reason': 'completed'}
    seq = [(i, h) for i, h, _ in r.driver.fills]
    assert [x for k, x in enumerate(seq) if k == 0 or seq[k - 1] != x] == [
        (0, 'username'), (1, 'password'), (2, 'one_time_code')]       # a restart types again
    reads = [c for c in r.driver.calls if c[0] == 'find_login_fields']
    fills = [c for c in r.driver.calls if c[0] == 'fill']
    assert all(any(f[2] == rd[2] for rd in reads) for f in fills)   # each fill names a read's generation


async def test_a_page_that_keeps_changing_asks_for_the_live_view(tmp_path):
    """Bounded: past PAGE_CHANGES restarts the sign-in asks for the login live view (it does
    not end the run), and nothing was filled."""
    from casa_browser.broker import main
    r = await Rig(tmp_path).setup()
    _moving(r, {('settle', n): 'changed' for n in range(1, main.PAGE_CHANGES + 2)})
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'page_unsettled'}
    assert r.driver.fills == []


async def test_fields_that_went_away_without_our_submit_are_never_a_completion(tmp_path):
    """Operator ruling (option B): the step found fields, the page changed, and the read after
    it finds none: the login live view, never logged_in — whatever the page shows."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username',)), Step()]
    def gone():
        r.driver.steps = [Step()]
    _moving(r, {('fill', 1): 'changed', ('settle', 2): gone})
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'page_unsettled'}
    assert not [c for c in r.driver.calls if c[0] == 'submit']


async def test_a_form_that_renders_late_is_waited_for_and_signed_into(tmp_path):
    """Operator ruling 2: a field-free read before anything was filled reads the page again; a
    form that renders meanwhile is signed into."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step()]
    def rendered():
        r.driver.steps = [Step(('username', 'password')), Step()]
    _moving(r, {('settle', 4): rendered})
    assert await _login(r) == {'status': 'logged_in', 'reason': 'completed'}
    assert [h for _, h, _ in r.driver.fills] == ['username', 'password']


async def test_a_sign_in_that_found_no_form_claims_nothing(tmp_path):
    """Operator ruling 2: no form after the wait is no_form, not logged_in (an operator who is
    already signed in sees no form too: the model reading the page decides); the item's
    unverified mark stays, since nothing proved it."""
    from casa_browser.broker import main
    r = await Rig(tmp_path).setup()
    r.broker._registry['site']['unverified'] = True
    r.driver.steps = [Step()]
    assert await _login(r) == {'status': 'no_form', 'reason': 'no_form'}
    assert len([c for c in r.driver.calls if c[0] == 'find_login_fields']) == 1 + main.FORM_WAITS
    assert r.broker._registry['site'].get('unverified') is True and r.driver.fills == []


async def test_after_our_submit_a_field_free_site_page_is_logged_in_at_once(tmp_path):
    """The wait is only for a sign-in that filled nothing: after a submit of ours, a field-free
    page on the site is the completion, read once."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step()]
    assert await _login(r) == {'status': 'logged_in', 'reason': 'completed'}
    assert len([c for c in r.driver.calls if c[0] == 'find_login_fields']) == 2


async def test_a_page_change_on_the_code_page_fills_no_code(tmp_path):
    """The emailed code goes only into the document that asked: a page change while it is
    filled reads the page again, whose generation is another one — code_page_gone."""
    r = await Rig(tmp_path).setup()
    gen = await _report(r, [Step(('one_time_code',)), Step()])
    driver = _moving(r, {('fill', 1): 'changed'})
    real_settle = r.driver.settle

    async def moved_settle(driver_session, timeout_s=30):
        reply = await real_settle(driver_session, timeout_s)
        return dict(reply, page_generation=reply['page_generation'] + 100 * (driver.count['settle'] > 1))
    r.driver.settle = moved_settle
    reply = await r.call('fill_code', run_id='run', site_id='site', driver_session='driver',
                         page_generation=gen, code='123456')
    assert reply == {'status': 'needs_live_view', 'reason': 'code_page_gone'}
    assert not [c for c in r.driver.calls if c[0] == 'submit']


# -- slice 5v: 1Password's passkey prompt and the guest -----------------------------------------

def _linked(r):
    item = r.sdk.saved['vault', 'item']
    return item.title, item_value(item, 'username')


async def test_a_passkey_prompt_for_the_linked_item_is_signed_in_and_claims_nothing(tmp_path):
    """The prompt offering the linked item (its title and username) is answered by its Sign in,
    which counts as the step's submit; the field-free page after it claims nothing (the site
    checks the assertion on its own time): no_form, the unverified mark stays, nothing filled."""
    r = await Rig(tmp_path).setup()
    r.broker._registry['site']['unverified'] = True
    r.driver.steps = [Step(('username',), submit=None, passkey_frame=True), Step()]
    assert await _login(r) == {'status': 'no_form', 'reason': 'passkey'}
    assert r.driver.passkeys == [_linked(r)]
    assert r.driver.fills == [] and not [c for c in r.driver.calls if c[0] == 'submit']
    assert r.broker._registry['site'].get('unverified') is True


async def test_a_passkey_whose_site_shows_its_form_again_is_the_live_view(tmp_path):
    """A failed assertion (webauthn.io shows its form again) is never a sign-in: the form's own
    reason, here two submit buttons."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username',), submit=None, passkey_frame=True),
                      Step(('username',), submit=None)]
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'ambiguous_fields'}
    assert len(r.driver.passkeys) == 1


async def test_a_second_passkey_prompt_in_one_sign_in_is_the_live_view(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(passkey_frame=True), Step(passkey_frame=True)]
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'challenge'}
    assert len(r.driver.passkeys) == 1


async def test_a_linked_title_another_login_shares_never_answers_a_prompt(tmp_path):
    """The prompt shows a title and a username, never an item id: when another login of the
    vault has the linked item's title, the prompt is left to the operator."""
    from brokerfakes import make_item
    r = await Rig(tmp_path).setup()
    r.sdk.saved['vault', 'twin'] = make_item('twin')
    r.driver.steps = [Step(passkey_frame=True)]
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'challenge'}
    assert r.driver.passkeys == []


async def test_a_prompt_the_driver_does_not_match_goes_on_as_before(tmp_path):
    """The driver found no prompt offering the linked item: nothing chosen; a field-free page is
    the challenge, a form is signed into as before."""
    r = await Rig(tmp_path).setup()
    r.driver.passkey_ok = False
    r.driver.steps = [Step(passkey_frame=True)]
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'challenge'}
    (tmp_path / 'b').mkdir()
    r2 = await Rig(tmp_path / 'b').setup()
    r2.driver.passkey_ok = False
    r2.driver.steps = [Step(('username', 'password'), passkey_frame=True), Step()]
    assert await _login(r2) == {'status': 'logged_in', 'reason': 'completed'}
    assert len(r2.driver.passkeys) == 1 and len(r2.driver.fills) == 2


async def test_an_item_without_a_username_never_answers_a_prompt(tmp_path):
    r = await Rig(tmp_path).setup()
    item = r.sdk.saved['vault', 'item']
    item.fields = [f for f in item.fields if f.id != 'username']
    r.driver.steps = [Step(passkey_frame=True)]
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'challenge'}
    assert r.driver.passkeys == []


@pytest.mark.parametrize('state, called, reply', [
    ('signed_out', 'guest_sign_in', {'state': 'unlocked', 'reason': 'completed'}),
    ('locked', 'guest_unlock', {'state': 'unlocked', 'reason': 'completed'}),
    ('unlocked', None, {'state': 'unlocked', 'reason': 'completed'}),
    ('unknown', None, {'state': 'needs_live_view', 'reason': 'guest_prompt'}),
])
async def test_the_guest_flow_follows_the_measured_states(tmp_path, state, called, reply):
    r = await Rig(tmp_path).setup()
    r.driver.guest = state
    assert await r.call('unlock_guest', run_id='run', site_id='site', driver_session='d') == reply
    ops = [c[0] for c in r.driver.calls]
    assert ops[0] == 'guest_state'
    assert ops[1:] == ([called] if called else [])
    if called == 'guest_sign_in':
        assert r.driver.calls[1][2] == 'acct.1password.eu'


async def test_the_guest_flow_is_bounded(tmp_path, monkeypatch):
    """A guest op that never answers ends the flow within its bound (``timeout``), which the
    run controller logs and goes on from."""
    from casa_browser.broker import main
    monkeypatch.setattr(main, 'GUEST_FLOW_S', 0.2)
    r = await Rig(tmp_path).setup()

    async def hang(driver_session):
        await asyncio.Event().wait()
    r.driver.guest_state = hang
    assert await r.call('unlock_guest', run_id='run', site_id='site', driver_session='d') == \
        {'state': 'needs_live_view', 'reason': 'timeout'}


async def test_guest_ops_get_their_longer_transport_bound():
    from casa_browser.broker import main
    from casa_browser import login
    assert main.GUEST_OPS == {'guest_state', 'guest_sign_in', 'guest_unlock'}
    # The driver answers before the broker's transport gives up on an op, the flow outlasts its
    # two ops, and the run controller's long call outlasts the flow.
    assert login._GUEST_TIMEOUT < main.GUEST_OP_S and 2 * main.GUEST_OP_S < main.GUEST_FLOW_S < 125


async def test_a_sign_in_that_answered_a_passkey_never_reports_logged_in(tmp_path):
    """r1 Astra: a passkey, then a form the broker fills and submits, then a field-free page:
    still no_form — a sign-in that answered a passkey claims nothing."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(passkey_frame=True), Step(('username', 'password')), Step()]
    assert await _login(r) == {'status': 'no_form', 'reason': 'passkey'}
    assert len(r.driver.passkeys) == 1 and len(r.driver.fills) == 2


async def test_a_passkey_prompt_is_tried_once_whatever_answers_the_try(tmp_path):
    """r1 Astra: the try counts before the call, so a page change reported after the click
    never leads to a second click in the same sign-in."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(passkey_frame=True), Step(passkey_frame=True)]
    real = r.driver.choose_passkey

    async def changed(**args):
        await real(**args)
        return {'error': 'page_changed'}
    r.driver.choose_passkey = changed
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'challenge'}
    assert len(r.driver.passkeys) == 1


@pytest.mark.parametrize('code', ['timeout', 'internal', 'refused'])
async def test_a_guest_op_that_fails_stops_nothing(tmp_path, code):
    """r1 Terra: a guest driver op's error is the flow's reason (logged by the run controller,
    which goes on), never a broker error that would interrupt the run."""
    r = await Rig(tmp_path).setup()

    async def failing(driver_session):
        return {'error': code}
    r.driver.guest_state = failing
    assert await r.call('unlock_guest', run_id='run', site_id='site', driver_session='d') == \
        {'state': 'needs_live_view', 'reason': code}


async def test_a_title_that_differs_only_by_spaces_is_not_unique(tmp_path):
    """r2 Astra: the prompt's text is trimmed before it is matched, so titles are compared
    trimmed too: a twin whose title only adds spaces declines the prompt."""
    from brokerfakes import make_item
    r = await Rig(tmp_path).setup()
    twin = make_item('twin')
    twin.title = ' ' + twin.title + ' \t'
    r.sdk.saved['vault', 'twin'] = twin
    r.driver.steps = [Step(passkey_frame=True)]
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'challenge'}
    assert r.driver.passkeys == []


@pytest.mark.parametrize('after, reply', [
    (Step(passkey_frame=True), {'status': 'needs_live_view', 'reason': 'challenge'}),
    (Step(), {'status': 'no_form', 'reason': 'passkey'}),
])
async def test_a_sign_in_continued_with_a_code_keeps_its_passkey_try(tmp_path, after, reply):
    """r2 Astra: a passkey, then the site's code (needs_code, then fill_code), is one sign-in:
    no second passkey try, and it still claims nothing."""
    r = await Rig(tmp_path).setup()
    _no_totp(r)
    r.driver.steps = [Step(passkey_frame=True), Step(('one_time_code',)), after]
    first = await _login(r)
    assert first['status'] == 'needs_code'
    assert await r.call('fill_code', run_id='run', site_id='site', driver_session='driver',
                        page_generation=first['page_generation'], code='123456') == reply
    assert len(r.driver.passkeys) == 1
    assert [f[1] for f in r.driver.fills] == ['one_time_code']


async def test_a_run_tries_a_sites_passkey_prompt_once(tmp_path):
    """One passkey try per run and site, whichever sign-in of the run makes it (the start page
    opened again signs in again, but never answers a second prompt); the next run tries again."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(passkey_frame=True), Step()]
    assert await _login(r) == {'status': 'no_form', 'reason': 'passkey'}
    r.driver.index = 0
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'challenge'}
    assert len(r.driver.passkeys) == 1
    await r.call('end_run', run_id='run')
    await r.call('begin_run', run_id='run2', account_site_ids=['site'])
    r.driver.index = 0
    assert await r.call('login', run_id='run2', site_id='site', driver_session='driver') == \
        {'status': 'no_form', 'reason': 'passkey'}
    assert len(r.driver.passkeys) == 2


# -- the lazy guest unlock: a passkey hint while the guest is not unlocked -------------------

NEEDS_GUEST = {'status': 'needs_guest', 'reason': 'passkey'}


async def _ask(r, guest_ask=True):
    return await r.call('login', run_id='run', site_id='site', driver_session='driver',
                        guest_ask=guest_ask)


def _acted(r):
    return [c[0] for c in r.driver.calls if c[0] in ('fill', 'submit', 'choose_passkey')]


@pytest.mark.parametrize('hint', [
    Step(('username',), submit=None, passkey_hint=True),          # T1: the webauthn field
    Step(passkey_frame=True),                                       # T2: 1Password's prompt
    Step(('username',), passkey_frame=True),                        # T2 beside a form
])
async def test_a_passkey_hint_at_the_first_read_asks_for_the_guest(tmp_path, hint):
    """With ``guest_ask``, the first read's hint ends the sign-in before anything is filled,
    submitted or chosen, and the passkey try is not spent."""
    r = await Rig(tmp_path).setup()
    if hint.passkey_frame:
        r.driver.passkey_ok = False      # a locked prompt offers no item (M8): unmatched
    r.driver.steps = [hint, Step()]
    assert await _ask(r) == NEEDS_GUEST
    assert _acted(r) == [] and r.driver.fills == []
    assert r.broker._passkey_tries == set()
    assert [c[0] for c in r.driver.calls].count('find_login_fields') == 1


@pytest.mark.parametrize('hint', [Step(('username',), passkey_hint=True),
                                  Step(passkey_frame=True)])
async def test_a_passkey_hint_after_a_password_submit_asks_for_the_guest(tmp_path, hint):
    """A hint on the page after a submit of ours ends the sign-in there: the first step's fill
    and submit happened, nothing more."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), hint, Step()]
    assert await _ask(r) == {**NEEDS_GUEST, 'password_sent': True}
    assert _acted(r) == ['fill', 'fill', 'submit']
    assert r.broker._passkey_tries == set()


@pytest.mark.parametrize('steps, reply', [
    ([Step(('username',), submit=None, passkey_hint=True), Step()],
     {'status': 'needs_live_view', 'reason': 'ambiguous_fields'}),
    ([Step(passkey_frame=True), Step()], {'status': 'no_form', 'reason': 'passkey'}),
])
async def test_no_guest_is_asked_for_without_guest_ask(tmp_path, steps, reply):
    """``guest_ask`` False (or absent, as before) never ends in ``needs_guest``: the sign-in
    goes on as it did (the prompt answered, a hint field read as the form it is)."""
    for n, absent in enumerate((False, True)):
        (tmp_path / str(n)).mkdir()
        r = await Rig(tmp_path / str(n)).setup()
        r.driver.steps = list(steps)
        if absent:
            assert await r.call('login', run_id='run', site_id='site',
                                driver_session='driver') == reply
        else:
            assert await _ask(r, False) == reply


async def test_fill_code_never_asks_for_the_guest(tmp_path):
    """A code continuing a sign-in is that sign-in's: the page after it shows a hint, and the
    sign-in goes on (the prompt is the one it would have answered)."""
    r = await Rig(tmp_path).setup()
    _no_totp(r)
    r.driver.steps = [Step(('one_time_code',)), Step(passkey_frame=True, passkey_hint=True),
                      Step()]
    first = await _ask(r)
    assert first['status'] == 'needs_code'
    reply = await r.call('fill_code', run_id='run', site_id='site', driver_session='driver',
                         page_generation=first['page_generation'], code='123456')
    assert reply != NEEDS_GUEST and reply['status'] != 'needs_guest'


async def test_an_unlinked_item_or_a_page_with_no_hint_never_asks_for_the_guest(tmp_path):
    r = await Rig(tmp_path).setup(item=False)
    r.driver.steps = [Step(passkey_frame=True, passkey_hint=True)]
    assert await _ask(r) == {'status': 'needs_live_view', 'reason': 'not_found'}
    assert r.driver.calls == []
    (tmp_path / 'b').mkdir()
    r2 = await Rig(tmp_path / 'b').setup()
    r2.driver.steps = [Step(('username', 'password')), Step()]
    assert await _ask(r2) == {'status': 'logged_in', 'reason': 'completed'}


async def test_guest_ask_must_be_a_boolean(tmp_path):
    r = await Rig(tmp_path).setup()
    for value in ('true', 1, None):
        assert await _ask(r, value) == {'error': 'unsupported'}
    assert r.driver.calls == []


# -- a form whose button is held, or with a box beside the password ----------------------------

async def test_a_box_beside_the_password_goes_to_the_check_with_nothing_done(tmp_path):
    """Start page, a shown empty box nothing would fill beside the password: nothing filled or
    clicked, nothing claimed (the look takes it)."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('password',), empty_box=True)]
    assert await _login(r) == {'status': 'no_form', 'reason': 'needs_check'}
    assert _acted(r) == []


async def test_a_box_beside_the_password_on_a_later_step_is_the_operators(tmp_path):
    """After a username step of ours the check is not asked: the live view, the password
    unsent."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username',)), Step(('password',), empty_box=True)]
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'empty_field'}
    assert _acted(r) == ['fill', 'submit']
    assert [f[1] for f in r.driver.fills] == ['username']


async def test_a_held_button_is_pressed_once_after_the_fill_when_it_comes_on(tmp_path):
    """Both shapes: fill first (a form's own checks need the values), then wait, then one
    press. Exactly 2 fills, 3 looks at the button, 1 submit."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password'), submit_ready=False), Step()]
    replies = iter([{'ok': False}, {'ok': False}, {'ok': True}])

    async def usable():
        return next(replies)
    r.driver.usable = usable
    assert await _login(r) == {'status': 'logged_in', 'reason': 'completed'}
    order = [c[0] for c in r.driver.calls if c[0] in ('fill', 'submit', 'submit_usable')]
    assert order == ['fill', 'fill', 'submit_usable', 'submit_usable', 'submit_usable', 'submit']


async def test_a_ready_button_is_not_waited_for(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step()]
    assert await _login(r) == {'status': 'logged_in', 'reason': 'completed'}
    assert [c for c in r.driver.calls if c[0] == 'submit_usable'] == []


async def test_a_button_that_never_comes_on_sends_nothing(tmp_path):
    from casa_browser.broker.main import SUBMIT_WAIT_S
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password'), submit_ready=False)]

    async def usable():
        r.clock.advance(SUBMIT_WAIT_S)
        return {'ok': False}
    r.driver.usable = usable
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'submit_disabled'}
    assert _acted(r) == ['fill', 'fill']


async def test_a_page_change_during_the_wait_reads_again_and_presses_once(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password'), submit_ready=False), Step()]
    replies = iter([BrokerError('page_changed'), {'ok': True}])

    async def usable():
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply
    r.driver.usable = usable
    assert await _login(r) == {'status': 'logged_in', 'reason': 'completed'}
    assert _acted(r).count('submit') == 1 and _acted(r).count('fill') == 4


async def test_a_click_stopped_by_an_empty_box_is_the_operators(tmp_path):
    """The driver's check in the click's own evaluation: no send, the live view, no error that
    would interrupt the run."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password'))]
    r.driver.submit_reply = {'ok': False, 'empty_box': True}
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'empty_field'}


async def test_a_start_page_sign_in_sends_the_password_once(tmp_path):
    """A form that comes back with no error mark after a password submit (the old loop sent it
    up to six times): 1 password fill, 1 submit, then the operator."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step(('username', 'password'))]
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'password_again'}
    assert [f[1] for f in r.driver.fills] == ['username', 'password']
    assert _acted(r).count('submit') == 1


async def test_a_password_sent_before_the_guest_restart_is_not_sent_again(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password'))]
    assert await r.call('login', run_id='run', site_id='site', driver_session='driver',
                        guest_ask=False, password_sent=True) == {
        'status': 'needs_live_view', 'reason': 'password_again'}
    assert _acted(r) == []


async def test_after_the_password_went_out_a_username_step_is_not_pressed(tmp_path):
    """A form keeps the password however the page shows it: a read with only the username after
    the send stays unpressed."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step(('username',))]
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'password_again'}
    assert _acted(r) == ['fill', 'fill', 'submit']


async def test_after_the_password_one_code_step_is_pressed_and_no_more(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step(('one_time_code',)), Step(('one_time_code',))]
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'password_again'}
    assert _acted(r).count('submit') == 2


async def test_a_filled_password_the_next_read_no_longer_shows_is_not_pressed(tmp_path):
    """A page that re-rendered between the fill and the click and whose read no longer shows the
    password: the form still holds it, so nothing is pressed (the old flow pressed up to six
    times with the username alone showing)."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step(('username',))]

    _moving(r, {('submit', 1): 'changed'})
    real = r.driver.settle
    moved = {'n': 0}

    async def settle(driver_session, timeout_s=30):      # the second read is the re-rendered page
        moved['n'] += 1
        r.driver.index = 1 if moved['n'] > 1 else 0
        return await real(driver_session, timeout_s)
    r.driver.settle = settle
    assert await _login(r) == {'status': 'needs_live_view', 'reason': 'password_again'}
    assert _acted(r).count('submit') == 0
    assert [f[1] for f in r.driver.fills] == ['username', 'password']


async def test_a_filled_password_still_showing_after_a_page_change_is_filled_again_once_sent(tmp_path):
    """A re-render that keeps the field: filled again (it may be wiped), and pressed once."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step()]
    _moving(r, {('submit', 1): 'changed'})
    assert await _login(r) == {'status': 'logged_in', 'reason': 'completed'}
    assert [f[1] for f in r.driver.fills].count('password') == 2
    assert [c[0] for c in r.driver.calls].count('submit') == 1
