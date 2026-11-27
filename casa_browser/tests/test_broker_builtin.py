"""The broker when site logins are kept in the app's own store, and a site's link per store."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from brokerfakes import (LOGIN, BuiltinRig, FakeSDK, Rig, Step, attempt_of, canary, item_value,
                         sdk_error)
from casa_browser import vault_store
from casa_browser.broker import items, logins_page
from casa_browser.broker.builtin import BuiltinVault, one_time_code
from casa_browser.broker.items import Vault
from casa_browser.broker.main import (LINK_KEYS, VAULT_DIR, BrokerServer, build_vault,
                                      site_record, site_view)
from casa_browser.credential_rpc import call as rpc_call
from casa_browser.options import BrokerOptions
# The saved-logins page's forms, read and posted as a browser does.
from test_logins_page import form_of, post

SEED = 'JBSWY3DPEHPK3PXP'
LOGGED_IN = {'status': 'logged_in', 'reason': 'completed'}
NOT_LINKED = {'status': 'needs_live_view', 'reason': 'not_found'}
REGISTERED = '{"site": {"item_id": "item", "login_origins": ["https://login.example"], ' \
             '"operator_ref": "operator"}}'


async def _login(r, **extra):
    return await r.call('login', run_id='run', site_id='site', driver_session='driver', **extra)


def _page(r, *steps):
    """The pages the driver shows from here on, read from the first, with nothing filled yet."""
    r.driver.steps, r.driver.index = list(steps), 0
    r.driver.fills.clear()
    r.driver.calls.clear()


def _form():
    return Step(('username', 'password'))


def _filled(r):
    return [(role, value) for _, role, value in r.driver.fills]


def _typed():
    return [('username', canary()), ('password', canary())]


async def _type_and_save(r, values):
    """A person signs in through the live view with ``values``; the sign-in is judged to have
    worked, so the save runs."""
    await r.capture(values, attempt=attempt_of(values))
    return await r.save()


async def _held(r, item_id):
    item = await r.vault.get(item_id)
    return [(role, item_value(item, role)) for role in ('username', 'password')]


def _record(root, site_id='site'):
    return json.loads((root / 'registry.json').read_text())[site_id]


def _asked(vault):
    """Every id the vault is asked for from here on."""
    asked, real = [], vault.get

    async def get(item_id):
        asked.append(item_id)
        return await real(item_id)
    vault.get = get
    return asked


# -- which vault is built ------------------------------------------------------------------

def test_the_store_is_under_the_brokers_data():
    assert str(VAULT_DIR) == '/data/broker/vault'


@pytest.mark.parametrize('login_store, token, built', [
    ('onepassword', 'a token', Vault),
    ('onepassword', '', Vault),
    ('none', '', Vault),
    ('builtin', '', BuiltinVault),
    ('builtin', 'a token', BuiltinVault),
])
def test_build_vault_follows_the_mode_only(tmp_path, login_store, token, built):
    """The word bootstrap wrote decides, never the token: invalid options (``none``) get the
    1Password class they always got, and only the built-in mode touches the directory."""
    directory = tmp_path / 'vault'
    vault = build_vault(BrokerOptions(op_token=token, login_store=login_store), directory)
    assert type(vault) is built
    assert directory.exists() is (built is BuiltinVault)
    if built is BuiltinVault:
        assert vault.available is True


async def test_build_vault_opens_the_store_with_the_supplied_key(tmp_path):
    """The key bootstrap passed on is the one that encrypts the logins: the store's file opens
    with it, not with another, and no key file is written beside it."""
    directory = tmp_path / 'vault'
    options = BrokerOptions(op_token='', login_store='builtin', store_key='a key from outside')
    vault = build_vault(options, directory)
    assert type(vault) is BuiltinVault and vault.available is True
    login = await vault.create_login('Synthetic site', canary(), canary(), list(LOGIN))
    assert [p.name for p in directory.iterdir()] == [vault_store.DATABASE_NAME]

    opened = vault_store.Store.open(directory, 'a key from outside')
    assert [item.id for item in opened.list()] == [login.id]
    for other in (options.vault, 'another key'):
        with pytest.raises(vault_store.KeyProblem):
            vault_store.Store.open(directory, other)
    assert [p.name for p in directory.iterdir()] == [vault_store.DATABASE_NAME]


def _populated_store(directory):
    """A built-in store with one login in it, as an earlier start without a token left it.
    Returns its files' bytes, by name."""
    store = vault_store.Store.open(directory, None)
    store.create(title='Synthetic login', category='LOGIN', tags=(), writer='live_view',
                 fields=[vault_store.Field('', 'username', 'STRING', canary()),
                         vault_store.Field('', 'password', 'CONCEALED', canary())])
    return {p.name: p.read_bytes() for p in directory.iterdir()}


def _count_opens(monkeypatch):
    """Every time the app's own store is opened from here on."""
    opened, real = [], vault_store.Store.open

    def counting(cls, directory, env_key):
        opened.append(directory)
        return real(directory, env_key)
    monkeypatch.setattr(vault_store.Store, 'open', classmethod(counting))
    return opened


@pytest.mark.parametrize('populated', [False, True], ids=['no store', 'a store with a login'])
async def test_a_failing_token_never_opens_the_built_in_store(tmp_path, monkeypatch, populated):
    """A token that does not work stays a visible 1Password failure: no login is looked for,
    or put, anywhere else, also when a store an earlier start without a token made is there."""
    tried = []

    async def authenticate(**given):
        tried.append(given['auth'])
        raise sdk_error('Internal')
    monkeypatch.setattr(items.Client, 'authenticate', authenticate)
    directory = tmp_path / 'vault'
    before = _populated_store(directory) if populated else None
    opened = _count_opens(monkeypatch)
    r = Rig(tmp_path)
    r.broker.vault = build_vault(r.options, directory)
    await r.broker.start()
    assert await r.call('status') == dict(configured=True, sdk='unavailable', guest_role='unknown',
                                          readonly_users=0, editable_users=0)
    assert await r.call('register_item', site_id='site', item_id='item',
                        login_origins=list(LOGIN), operator_ref='operator') == {
                            'error': 'unavailable'}
    assert tried == [r.options.op_token] * 2
    assert opened == []
    if populated:
        assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    else:
        assert not directory.exists()


@pytest.mark.parametrize('token', ['', 'a token'])
async def test_invalid_options_never_open_a_store_that_is_there(tmp_path, monkeypatch, token):
    """Options that are not valid (``none``) get the 1Password class, also when a store an
    earlier start without a token made is there: it is not opened, and stays as it was."""
    directory = tmp_path / 'vault'
    before = _populated_store(directory)
    opened = _count_opens(monkeypatch)
    vault = build_vault(BrokerOptions(op_token=token, login_store='none'), directory)
    assert type(vault) is Vault
    assert opened == []
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


# -- status ----------------------------------------------------------------------------------

async def test_builtin_status_says_so_and_assesses_no_accessors(tmp_path):
    r = await BuiltinRig(tmp_path).setup()
    seen = []

    async def accessors():
        seen.append('vault_accessors')
    r.vault.vault_accessors = accessors
    assert await r.call('status') == {'configured': True, 'store': 'builtin', 'available': True,
                                      'links': {'site': r.item_id}}
    assert seen == []


async def test_builtin_status_when_the_store_cannot_be_opened(tmp_path):
    # A store that only a key from outside opens, and a start without that key.
    vault_store.Store.open(tmp_path / 'vault', 'a key from outside')
    r = BuiltinRig(tmp_path)
    await r.broker.start()
    assert await r.call('status') == {'configured': True, 'store': 'builtin', 'available': False,
                                      'links': {}}


# -- linking and saving ----------------------------------------------------------------------

async def test_builtin_register_item_links_a_saved_login_without_a_warning(tmp_path):
    r = BuiltinRig(tmp_path)
    await r.broker.start()
    login = await r.vault.create_login('Synthetic login', canary(), canary(), LOGIN)
    reply = await r.call('register_item', site_id='site', item_id=login.id,
                         login_origins=list(LOGIN), operator_ref='operator')
    assert reply == {'site_id': 'site', 'roles_available': ['username', 'password'],
                     'extra_field_names': [], 'warning': ''}
    assert _record(tmp_path) == {'item_id': None, 'login_origins': list(LOGIN),
                                 'operator_ref': 'operator', 'builtin': {'item_id': login.id}}


async def test_a_login_typed_in_the_live_view_is_saved_and_signs_in_next_time(tmp_path):
    r = await BuiltinRig(tmp_path).setup(item=False)
    typed = _typed()
    saved = await _type_and_save(r, typed)
    assert saved['saved'] is True
    logins = await r.vault.list_logins()
    assert [(o.id, o.title, [w.url for w in o.websites]) for o in logins] == [
        (saved['item_id'], 'Synthetic site', list(LOGIN))]
    assert await _held(r, saved['item_id']) == typed
    _page(r, _form(), Step())
    assert await _login(r) == LOGGED_IN
    assert _filled(r) == typed


async def test_a_second_live_view_password_overwrites_only_after_a_rejection(tmp_path):
    r = await BuiltinRig(tmp_path).setup()
    before = await _held(r, r.item_id)
    assert await _type_and_save(r, _typed()) == {
        'saved': False, 'reason': 'item_kept', 'item_id': r.item_id}
    assert (await r.vault.get(r.item_id)).version == 1
    assert await _held(r, r.item_id) == before

    _page(r, _form(), Step(('username', 'password'), field_error=True))
    assert await _login(r) == {'status': 'failed', 'reason': 'rejected'}
    record = _record(tmp_path)
    assert record['builtin'] == {'item_id': r.item_id,
                                 'rejected': {'run_id': 'run', 'item_id': r.item_id}}
    assert 'rejected' not in record and record['item_id'] is None

    typed = _typed()
    assert await _type_and_save(r, typed) == {'saved': True, 'item_id': r.item_id}
    assert (await r.vault.get(r.item_id)).version == 2
    assert await _held(r, r.item_id) == typed


# -- signing in ------------------------------------------------------------------------------

async def test_builtin_sign_in_with_a_passkey_marker_fills_the_stored_login(tmp_path):
    """A passkey marker sits on ordinary password forms. Here there is no extension to answer
    it and no guest to unlock, so the form is filled like any other."""
    r = await BuiltinRig(tmp_path).setup()
    _page(r, Step(('username', 'password'), passkey_hint=True), Step())
    assert await _login(r, guest_ask=True) == LOGGED_IN
    assert _filled(r) == await _held(r, r.item_id)


async def test_builtin_never_asks_for_a_passkey_choice(tmp_path):
    # Everything the choice needs is there: a prompt, one login of that title, its username.
    r = await BuiltinRig(tmp_path).setup()
    _page(r, Step(('username', 'password'), passkey_frame=True), Step())
    assert await _login(r) == LOGGED_IN
    assert r.driver.passkeys == []
    assert r.broker._passkey_tries == set()
    assert _filled(r) == await _held(r, r.item_id)


async def test_builtin_look_reports_no_passkey(tmp_path):
    r = await BuiltinRig(tmp_path).setup()
    look = dict(run_id='run', site_id='site', driver_session='driver')
    _page(r, Step(passkey_hint=True, passkey_frame=True))
    assert await r.call('read_sign_in', **look) == {'found': False}
    assert r.broker._looks == {}
    _page(r, Step(('username', 'password'), passkey_hint=True, passkey_frame=True))
    reply = await r.call('read_sign_in', **look)
    assert reply['found'] is True and reply['passkey'] is False
    assert [f['role'] for f in reply['fields']] == ['password', 'username']


async def test_builtin_unlock_guest_is_refused_and_calls_no_driver(tmp_path):
    r = await BuiltinRig(tmp_path).setup()
    r.driver.guest = 'locked'
    assert await r.call('unlock_guest', run_id='run', site_id='site',
                        driver_session='driver') == {'error': 'refused'}
    assert not [c for c in r.driver.calls if c[0].startswith('guest_')]


async def test_an_unavailable_store_fails_a_sign_in_as_unavailable(tmp_path):
    r = await BuiltinRig(tmp_path).setup()
    (tmp_path / 'vault' / vault_store.KEY_NAME).unlink()
    await r.restart()
    assert r.vault.available is False
    await r.call('begin_run', run_id='run', account_site_ids=['site'])
    _page(r, _form(), Step())
    assert await _login(r) == {'error': 'unavailable'}
    assert r.driver.fills == []


@pytest.mark.parametrize('linked', [True, False], ids=['linked', 'unlinked'])
async def test_an_unavailable_store_sends_nobody_to_type_a_login_it_cannot_keep(tmp_path, linked):
    """A site with no login yet would otherwise be sent to the live view to sign in there, for
    a save that cannot happen. With the store unavailable its sign-in ends ``unavailable`` too,
    before the driver is used; a live-view sign-in made anyway is answered as not saved, and
    the store's file is left as it was."""
    r = await BuiltinRig(tmp_path).setup(item=linked)
    (tmp_path / 'vault' / vault_store.KEY_NAME).unlink()
    database = tmp_path / 'vault' / vault_store.DATABASE_NAME
    before = database.read_bytes()
    await r.restart()
    assert r.vault.available is False
    await r.call('begin_run', run_id='run', account_site_ids=['site'])
    _page(r, _form(), Step())
    assert await _login(r) == {'error': 'unavailable'}
    assert await _login(r, auto=True) == {'error': 'unavailable'}
    assert r.driver.calls == [] and r.driver.fills == []
    assert await _type_and_save(r, _typed()) == {
        'saved': False, 'item_id': r.item_id, 'reason': 'unknown'}
    assert database.read_bytes() == before
    assert sorted(p.name for p in (tmp_path / 'vault').iterdir()) == [vault_store.DATABASE_NAME]


async def test_a_code_step_uses_the_stored_seed(tmp_path):
    r = await BuiltinRig(tmp_path, seed=SEED).setup()
    _page(r, _form(), Step(('one_time_code',)), Step())
    assert await _login(r) == LOGGED_IN
    assert _filled(r) == [*await _held(r, r.item_id),
                          ('one_time_code', one_time_code(SEED, r.clock()))]


# -- a site's link per store -----------------------------------------------------------------

def test_site_view_and_site_record():
    assert LINK_KEYS == ('item_id', 'unverified', 'rejected')
    site = {'login_origins': list(LOGIN), 'operator_ref': 'operator', 'title': 'Synthetic site'}
    existing = {**site, 'item_id': 'item', 'unverified': True,
                'rejected': {'run_id': 'run', 'item_id': 'item'}}

    # Under 1Password a record with no built-in part is read and written as it is.
    assert site_view(existing, False) == existing
    assert site_record(site_view(existing, False), existing, False) == existing
    assert site_record(existing, None, False) == existing

    # Under the built-in store the same record is a site with no link and no mark, and
    # writing it back leaves the 1Password link as it was.
    unlinked = site_view(existing, True)
    assert unlinked == {**site, 'item_id': None}
    assert site_record(unlinked, existing, True) == {**existing, 'builtin': {'item_id': None}}

    # A link made there, with its mark, goes under ``builtin`` and comes back from it.
    linked = {**unlinked, 'item_id': 'saved', 'unverified': True}
    both = site_record(linked, existing, True)
    assert both == {**existing, 'builtin': {'item_id': 'saved', 'unverified': True}}
    assert site_view(both, True) == linked

    # 1Password does not see it, and keeps it through its own writes: a mark cleared, the
    # site registered again.
    assert site_view(both, False) == existing
    cleared = {key: value for key, value in existing.items() if key != 'unverified'}
    assert site_record(cleared, both, False) == {**cleared, 'builtin': both['builtin']}
    again = {'item_id': 'other', 'login_origins': list(LOGIN), 'operator_ref': 'operator'}
    assert site_record(again, both, False) == {**again, 'builtin': both['builtin']}

    # A site first registered under the built-in store has no 1Password link.
    assert site_record({**site, 'item_id': 'saved'}, None, True) == {
        **site, 'item_id': None, 'builtin': {'item_id': 'saved'}}


async def test_a_1password_only_registry_is_written_as_today(tmp_path):
    """The file's text for a registration, a rejected sign-in, a sign-in and a forget, as the
    broker wrote it before a site could have a second link."""
    r = await Rig(tmp_path).setup()
    registry = tmp_path / 'registry.json'
    assert registry.read_text() == REGISTERED
    _page(r, _form(), Step(('username', 'password'), field_error=True))
    assert await _login(r) == {'status': 'failed', 'reason': 'rejected'}
    rejected = ('{"site": {"item_id": "item", "login_origins": ["https://login.example"], '
                '"operator_ref": "operator", "rejected": {"item_id": "item", "run_id": "run"}}}')
    assert registry.read_text() == rejected
    _page(r, _form(), Step())
    assert await _login(r) == LOGGED_IN
    assert registry.read_text() == rejected
    assert await r.call('forget_site', site_id='site', operator_ref='operator') == {}
    assert registry.read_text() == ('{"site": {"item_id": null, "login_origins": '
                                    '["https://login.example"], "operator_ref": "operator"}}')


async def test_one_site_through_token_set_then_empty_then_set(tmp_path):
    """One registry and one journal, opened with a token, without one, with it again and
    without it again. Each store keeps its own link to the site: the other one's is neither
    used nor changed."""
    sdk = FakeSDK()
    registry = tmp_path / 'registry.json'
    mark = {'run_id': 'earlier', 'item_id': 'item'}

    # (1) With a token: the site is linked to a 1Password item, which carries a mark.
    r = await Rig(tmp_path, sdk=sdk).setup()
    item = sdk.saved['vault', 'item'].model_dump()
    in_1password = [(role, item_value(sdk.saved['vault', 'item'], role))
                    for role in ('username', 'password')]
    planted = json.loads(registry.read_text())
    planted['site']['rejected'] = mark
    registry.write_text(json.dumps(planted))
    await r.restart()
    await r.call('begin_run', run_id='run', account_site_ids=['site'])
    _page(r, _form(), Step())
    assert await _login(r) == LOGGED_IN
    assert _filled(r) == in_1password
    own = {'item_id': 'item', 'login_origins': list(LOGIN), 'operator_ref': 'operator',
           'rejected': mark}
    assert _record(tmp_path) == own
    r.journal.close()

    # (2) Without one: the site has no login here, and the 1Password id goes nowhere.
    b = await BuiltinRig(tmp_path).resume()
    asked = _asked(b.vault)
    _page(b, _form(), Step())
    assert await _login(b) == NOT_LINKED
    assert asked == [] and b.driver.calls == []
    first = _typed()
    saved = await _type_and_save(b, first)
    assert saved['saved'] is True and saved['item_id'] != 'item'
    _page(b, _form(), Step())
    assert await _login(b) == LOGGED_IN
    assert _filled(b) == first
    assert 'item' not in asked
    assert _record(tmp_path) == {**own, 'builtin': {'item_id': saved['item_id']}}

    # (3) Forgetting the login here, and saving another, changes only the built-in link.
    assert await b.call('forget_site', site_id='site', operator_ref='operator') == {}
    assert _record(tmp_path) == {**own, 'builtin': {'item_id': None}}
    second = _typed()
    saved = await _type_and_save(b, second)
    assert saved['saved'] is True
    kept = {'item_id': saved['item_id'], 'unverified': True}
    assert _record(tmp_path) == {**own, 'builtin': kept}
    b.journal.close()

    # (4) With the token again: the 1Password item signs in, untouched, and a write made in
    # this mode keeps the built-in link.
    r = await Rig(tmp_path, sdk=sdk).resume()
    _page(r, _form(), Step())
    assert await _login(r) == LOGGED_IN
    assert _filled(r) == in_1password
    assert sdk.saved['vault', 'item'].model_dump() == item and r.writes() == []
    await r.call('register_capture', site_id='other', login_origins=list(LOGIN),
                 operator_ref='operator', title='Another site')
    assert await r.call('forget_site', site_id='other', operator_ref='operator') == {}
    assert 'builtin' not in _record(tmp_path, 'other')
    assert _record(tmp_path) == {**own, 'builtin': kept}
    # Forgetting this very site's 1Password login, and linking it again, leaves its built-in
    # link and mark as they were after each write.
    plain = {'login_origins': list(LOGIN), 'operator_ref': 'operator'}
    assert await r.call('forget_site', site_id='site', operator_ref='operator') == {}
    assert _record(tmp_path) == {**plain, 'item_id': None, 'builtin': kept}
    reply = await r.call('register_item', site_id='site', item_id='item',
                         login_origins=list(LOGIN), operator_ref='operator')
    assert 'error' not in reply, reply
    assert _record(tmp_path) == {**plain, 'item_id': 'item', 'builtin': kept}
    r.journal.close()

    # (5) Without it once more: the login saved here still signs in, with both its values.
    b = await BuiltinRig(tmp_path).resume()
    _page(b, _form(), Step())
    assert await _login(b) == LOGGED_IN
    assert _filled(b) == second
    assert _record(tmp_path) == {**plain, 'item_id': 'item',
                                 'builtin': {'item_id': saved['item_id']}}


async def test_a_site_linked_without_a_token_has_no_1password_login(tmp_path):
    """A site first linked to a login of the app's own store, then started with a token: the
    site has no 1Password login, and the built-in login's id is never asked of 1Password."""
    b = await BuiltinRig(tmp_path).setup()
    builtin_id = b.item_id
    assert _record(tmp_path) == {'item_id': None, 'login_origins': list(LOGIN),
                                 'operator_ref': 'operator', 'builtin': {'item_id': builtin_id}}
    b.journal.close()

    sdk = FakeSDK()
    r = await Rig(tmp_path, sdk=sdk).resume()
    _page(r, _form(), Step())
    assert await _login(r) == NOT_LINKED
    assert [c for c in sdk.calls if c[0] == 'items.get'] == []
    assert r.driver.fills == []
    assert _record(tmp_path)['builtin'] == {'item_id': builtin_id}


async def test_forgetting_the_built_in_link_keeps_an_unverified_1password_login(tmp_path):
    """A 1Password login made by a live-view save is still ``unverified``. Without a token the
    site gets a login of its own store, which is then forgotten: the 1Password link and its
    mark are as the save left them."""
    sdk = FakeSDK()
    r = await Rig(tmp_path, sdk=sdk).setup(item=False)
    saved = await _type_and_save(r, _typed())
    assert saved['saved'] is True
    own = _record(tmp_path)
    assert own['item_id'] == saved['item_id'] and own['unverified'] is True
    r.journal.close()

    b = await BuiltinRig(tmp_path).resume()
    here = await _type_and_save(b, _typed())
    assert here['saved'] is True and here['item_id'] != saved['item_id']
    assert _record(tmp_path) == {**own, 'builtin': {'item_id': here['item_id'],
                                                    'unverified': True}}
    assert await b.call('forget_site', site_id='site', operator_ref='operator') == {}
    assert _record(tmp_path) == {**own, 'builtin': {'item_id': None}}


# -- a login changed or deleted on the saved-logins page ---------------------------------------

REJECTED = {'status': 'failed', 'reason': 'rejected'}
KEPT = 'item_kept'
# A site's link to a 1Password item with both marks, as a start with a token left it.
OTHER_STORE = {'item_id': 'item', 'unverified': True,
               'rejected': {'run_id': 'earlier', 'item_id': 'item'}}


def _logins_page(r):
    """The saved-logins page on the rig's vault, telling the rig's broker what a person
    changed and deleted, as the broker's process builds it."""
    return logins_page.page(r.vault, r.broker.login_changed, r.broker.login_deleted)


def _replace_password(page, item_id, password):
    """A person replaces the login's password on its edit form."""
    edit = f'/s/{item_id}/edit'
    response = post(page, edit, {**form_of(page, edit), 'replace1': '1', 'value1': password})
    assert response.status == 303
    return response


def _delete(page, item_id):
    """A person deletes the login on its own screen."""
    delete = f'/s/{item_id}/delete'
    assert post(page, delete, form_of(page, delete)).status == 303


def _registry(root):
    return json.loads((root / 'registry.json').read_text())


def _other_store_part(record):
    """The text of what a record holds for the store that is not in use."""
    return json.dumps({key: value for key, value in record.items() if key != 'builtin'},
                      sort_keys=True)


def _count_registry_writes(broker):
    """Every time the registry file is written from here on."""
    writes, real = [], broker._persist_registry

    def persist():
        writes.append(1)
        real()
    broker._persist_registry = persist
    return writes


def _hold_submit(r):
    """The driver's click on the form's button waits from here on: ``reached`` is set once
    the broker asked for it, and it happens once ``release`` is set."""
    reached, release, submit = asyncio.Event(), asyncio.Event(), r.driver.submit

    async def held(**args):
        reached.set()
        await release.wait()
        return await submit(**args)
    r.driver.submit = held
    return reached, release


async def _three_sites(root):
    """Two sites linked to login A (the first not signed in once yet, the second refused in
    this run) and a third linked to login B, not signed in once yet. Every record also holds
    a 1Password link with both marks. Returns the rig, the two ids and the records."""
    r = BuiltinRig(root)
    await r.broker.start()
    a = (await r.vault.create_login('Login A', canary(), canary(), LOGIN)).id
    b = (await r.vault.create_login('Login B', canary(), canary(), LOGIN)).id

    def site(title, **link):
        return {**OTHER_STORE, 'login_origins': list(LOGIN), 'operator_ref': 'operator',
                'title': title, 'builtin': link}
    records = {'first': site('First site', item_id=a, unverified=True),
               'second': site('Second site', item_id=a,
                              rejected={'run_id': 'run', 'item_id': a}),
               'third': site('Third site', item_id=b, unverified=True)}
    (root / 'registry.json').write_text(json.dumps(records))
    await r.restart()
    await r.call('begin_run', run_id='run', account_site_ids=list(records))
    return r, a, b, records


async def test_a_page_save_clears_the_marks_of_that_login_only(tmp_path):
    r, a, b, before = await _three_sites(tmp_path)
    writes = _count_registry_writes(r.broker)
    r.broker.login_changed(a)
    after = _registry(tmp_path)
    assert after == {'first': {**before['first'], 'builtin': {'item_id': a}},
                     'second': {**before['second'], 'builtin': {'item_id': a}},
                     'third': before['third']}
    assert after['third']['builtin'] == {'item_id': b, 'unverified': True}
    for site_id in before:
        assert _other_store_part(after[site_id]) == _other_store_part(before[site_id])
    assert writes == [1]
    # and the broker works on what the file holds
    assert {site_id: {key: site.get(key) for key in LINK_KEYS}
            for site_id, site in r.broker._registry.items()} == {
        'first': {'item_id': a, 'unverified': None, 'rejected': None},
        'second': {'item_id': a, 'unverified': None, 'rejected': None},
        'third': {'item_id': b, 'unverified': True, 'rejected': None}}


async def test_after_a_page_save_a_live_view_sign_in_is_not_saved_over_it(tmp_path):
    """The stored password is refused, so a live-view sign-in may be saved over the login.
    Then the person corrects the login on the page. What a live view sends after that is
    judged against the corrected login, which no site has refused: it is not saved over it."""
    r = await BuiltinRig(tmp_path).setup()
    page = _logins_page(r)
    _page(r, _form(), Step(('username', 'password'), field_error=True))
    assert await _login(r) == REJECTED
    assert _record(tmp_path)['builtin'] == {
        'item_id': r.item_id, 'rejected': {'run_id': 'run', 'item_id': r.item_id}}

    corrected = canary()
    _replace_password(page, r.item_id, corrected)
    assert _record(tmp_path)['builtin'] == {'item_id': r.item_id}
    on_the_page = await _held(r, r.item_id)
    assert on_the_page[1] == ('password', corrected)

    assert await _type_and_save(r, _typed()) == {
        'saved': False, 'reason': KEPT, 'item_id': r.item_id}
    assert await _held(r, r.item_id) == on_the_page
    assert (await r.vault.get(r.item_id)).version == 2


async def test_a_refusal_after_a_page_save_in_mid_sign_in_sets_no_mark(tmp_path):
    """The sign-in filled the stored password and waits on the driver. The person changes
    the login on the page. The refusal that then arrives is of the password the login held
    before, not of the one it holds now: it marks nothing."""
    r = await BuiltinRig(tmp_path).setup()
    page = _logins_page(r)
    _page(r, _form(), Step(('username', 'password'), field_error=True))
    reached, release = _hold_submit(r)
    signing_in = asyncio.create_task(_login(r))
    await asyncio.wait_for(reached.wait(), 10)
    assert [role for role, _ in _filled(r)] == ['username', 'password']

    corrected = canary()
    _replace_password(page, r.item_id, corrected)
    assert (await r.vault.get(r.item_id)).version == 2
    release.set()
    assert await signing_in == REJECTED
    assert _record(tmp_path)['builtin'] == {'item_id': r.item_id}

    on_the_page = await _held(r, r.item_id)
    assert await _type_and_save(r, _typed()) == {
        'saved': False, 'reason': KEPT, 'item_id': r.item_id}
    assert await _held(r, r.item_id) == on_the_page


async def test_a_refusal_after_the_login_was_deleted_in_mid_sign_in_is_still_a_refusal(tmp_path):
    r = await BuiltinRig(tmp_path).setup()
    page = _logins_page(r)
    _page(r, _form(), Step(('username', 'password'), field_error=True))
    reached, release = _hold_submit(r)
    signing_in = asyncio.create_task(_login(r))
    await asyncio.wait_for(reached.wait(), 10)
    _delete(page, r.item_id)
    release.set()
    assert await signing_in == REJECTED
    assert _record(tmp_path)['builtin'] == {'item_id': None}


async def test_an_ordinary_refusal_still_sets_the_mark_and_the_correction_is_saved(tmp_path):
    r = await BuiltinRig(tmp_path).setup()
    _page(r, _form(), Step(('username', 'password'), field_error=True))
    assert await _login(r) == REJECTED
    assert _record(tmp_path)['builtin'] == {
        'item_id': r.item_id, 'rejected': {'run_id': 'run', 'item_id': r.item_id}}
    assert (await r.vault.get(r.item_id)).version == 1
    typed = _typed()
    assert await _type_and_save(r, typed) == {'saved': True, 'item_id': r.item_id}
    assert await _held(r, r.item_id) == typed
    assert (await r.vault.get(r.item_id)).version == 2


async def test_with_1password_the_marks_are_untouched_by_this_release(tmp_path):
    """With 1Password a refused password marks the item as it always did, whatever happened
    to the item while the form was out: the item is read once, to fill the form, and its
    version is compared with nothing."""
    r = await Rig(tmp_path).setup()
    _page(r, _form(), Step(('username', 'password'), field_error=True))
    reached, release = _hold_submit(r)
    signing_in = asyncio.create_task(_login(r))
    await asyncio.wait_for(reached.wait(), 10)
    r.sdk.saved['vault', 'item'].version += 1     # changed in 1Password meanwhile
    release.set()
    assert await signing_in == REJECTED
    assert [call for call in r.sdk.calls if call[0] == 'items.get'] == [
        ('items.get', 'vault', 'item')]
    assert (tmp_path / 'registry.json').read_text() == (
        '{"site": {"item_id": "item", "login_origins": ["https://login.example"], '
        '"operator_ref": "operator", "rejected": {"item_id": "item", "run_id": "run"}}}')
    typed = _typed()
    assert await _type_and_save(r, typed) == {'saved': True, 'item_id': 'item'}
    assert [(role, item_value(r.sdk.saved['vault', 'item'], role))
            for role in ('username', 'password')] == typed


async def test_deleting_a_login_unlinks_its_sites(tmp_path):
    r, a, b, before = await _three_sites(tmp_path)
    writes = _count_registry_writes(r.broker)
    _delete(_logins_page(r), a)
    assert [overview.id for overview in await r.vault.list_logins()] == [b]
    after = _registry(tmp_path)
    assert after == {'first': {**before['first'], 'builtin': {'item_id': None}},
                     'second': {**before['second'], 'builtin': {'item_id': None}},
                     'third': before['third']}
    for site_id in before:
        assert _other_store_part(after[site_id]) == _other_store_part(before[site_id])
    assert writes == [1]
    kept = {'login_origins': list(LOGIN), 'operator_ref': 'operator', 'item_id': None}
    assert r.broker._registry['first'] == {**kept, 'title': 'First site'}
    assert r.broker._registry['second'] == {**kept, 'title': 'Second site'}

    # A site that lost its login asks for one again; the other login still signs in.
    for site_id in ('first', 'second'):
        _page(r, _form(), Step())
        assert await r.call('login', run_id='run', site_id=site_id,
                            driver_session='driver') == NOT_LINKED
        assert r.driver.calls == []
    _page(r, _form(), Step())
    assert await r.call('login', run_id='run', site_id='third',
                        driver_session='driver') == LOGGED_IN
    assert _filled(r) == await _held(r, b)


async def test_a_live_view_verdict_that_arrives_after_a_delete_saves_nothing(tmp_path):
    """A person signs in through the live view on a site that has a login, and before the
    verdict on that sign-in arrives deletes the login on the page. The verdict must not save
    what was typed as a new login of the site: the person just removed its login."""
    r = await BuiltinRig(tmp_path).setup()
    typed = _typed()
    await r.capture(typed, attempt=attempt_of(typed))
    taken = await r.call('take_attempt', run_id='run', site_id='site', capture_id='capture')
    assert taken == {'attempt': {'failed': False, 'typed_after': False}, 'incomplete': False}

    _delete(_logins_page(r), r.item_id)
    reply = await r.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                         title='Synthetic site')
    assert reply == {'error': 'refused'}
    assert await r.vault.list_logins() == []
    assert _record(tmp_path)['builtin'] == {'item_id': None}
    assert r.broker._captures == {}


async def test_status_lists_the_links(tmp_path):
    r, a, b, _ = await _three_sites(tmp_path / 'own')
    builtin = {'configured': True, 'store': 'builtin', 'available': True}
    assert await r.call('status') == {**builtin, 'links': {'first': a, 'second': a, 'third': b}}
    _delete(_logins_page(r), a)
    assert await r.call('status') == {**builtin, 'links': {'third': b}}

    # With 1Password the reply is the one it was.
    op = await Rig(tmp_path / 'op').setup()
    reply = await op.call('status')
    assert sorted(reply) == ['configured', 'editable_users', 'guest_role', 'readonly_users',
                             'sdk']
    assert reply['sdk'] == 'ok'


@pytest.mark.parametrize('sites, listed', [(600, True), (700, False)])
async def test_status_answers_without_the_links_when_they_do_not_fit_one_line(
        tmp_path, sites, listed):
    """One reply is one line of at most 64 KiB. A registry whose links do not fit is answered
    without them, over the real socket: the caller still learns that the store is there."""
    links = {f'site-{n:04d}'.ljust(64, 'x'): f'login-{n:04d}'.ljust(26, 'y')
             for n in range(sites)}
    (tmp_path / 'registry.json').write_text(json.dumps({
        site_id: {'item_id': None, 'login_origins': list(LOGIN), 'operator_ref': 'operator',
                  'builtin': {'item_id': item_id}} for site_id, item_id in links.items()}))
    r = BuiltinRig(tmp_path)
    await r.broker.start()
    server = BrokerServer(r.broker, tmp_path / 'rpc.sock', allowed_uid=os.getuid())
    await server.start()
    try:
        reply = await rpc_call(server.path, 10, 'status')
    finally:
        await server.close()
    builtin = {'configured': True, 'store': 'builtin', 'available': True}
    assert reply == ({**builtin, 'links': links} if listed else builtin)
