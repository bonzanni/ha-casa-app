"""What the broker reads from a vault, asserted on both places logins can be kept.

The same tests run against the 1Password vault class (on the SDK double the broker
tests use) and against the vault kept in the app's own store, so the broker can be
handed either one.
"""
from __future__ import annotations

import re

import pytest
from onepassword.types import ItemField

from brokerfakes import FakeSDK, canary
from casa_browser.broker import BrokerError, main
from casa_browser.broker.builtin import BuiltinVault
from casa_browser.broker.items import Vault
from casa_browser.protocol import ID_PATTERN

ORIGINS = ('https://login.example', 'https://www.example.org:8443')
HOSTS = ['login.example', 'www.example.org']
TITLE = 'Synthetic site'
WORDS_THAT_REMOVE = ('delete', 'archive', 'remove', 'purge', 'trash')


def _plant_sdk_seed(vault, item_id):
    vault._client.saved['vault', item_id].fields.append(
        ItemField(id='otp', title='one-time code', field_type='Totp', value=canary()))


def _plant_builtin_seed(vault, item_id):
    vault._store.set_field(item_id, label='one-time password', type='OTP',
                           value='JBSWY3DPEHPK3PXP', writer='live_view')


PLANT_SEED = {Vault: _plant_sdk_seed, BuiltinVault: _plant_builtin_seed}


def plant_seed(vault, item_id):
    """Give the login an authenticator seed, the way each backend holds one."""
    PLANT_SEED[type(vault)](vault, item_id)


@pytest.fixture(params=['sdk', 'builtin'])
def vault(request, tmp_path):
    if request.param == 'sdk':
        return Vault(FakeSDK(), 'Synthetic vault')
    return BuiltinVault(tmp_path / 'vault', None)


def _values(item):
    """The login values as the broker reads them: by field id, sections left out."""
    return {f.id: f.value for f in item.fields
            if f.id in ('username', 'password') and not f.section_id}


async def _code_of(call):
    with pytest.raises(BrokerError) as raised:
        await call
    return raised.value.code


async def test_a_created_login_has_what_the_broker_reads(vault):
    username, password = canary(), canary()
    created = await vault.create_login(TITLE, username, password, ORIGINS)
    assert ID_PATTERN.fullmatch(created.id)
    assert isinstance(created.version, int)

    item = await vault.get(created.id)
    assert item.id == created.id
    assert item.vault_id == await vault.resolve_vault_id()
    assert isinstance(item.version, int) and item.version == created.version
    assert item.category in ('Login', 'Password')
    assert item.title == TITLE
    fields = {f.id: f for f in item.fields}
    assert (fields['username'].value, fields['password'].value) == (username, password)
    assert not fields['username'].section_id and not fields['password'].section_id
    assert (fields['username'].field_type, fields['password'].field_type) == ('Text', 'Concealed')
    assert (fields['username'].title, fields['password'].title) == ('username', 'password')
    assert not any(f.field_type == 'Totp' for f in item.fields)


async def test_a_login_created_without_a_password_has_no_password_field(vault):
    username = canary()
    created = await vault.create_login(TITLE, username, '', ORIGINS)
    item = await vault.get(created.id)
    assert [f.id for f in item.fields if f.id in ('username', 'password')] == ['username']
    assert _values(item) == {'username': username}


async def test_the_listing_has_id_title_and_hosts_and_no_value(vault):
    username, password = canary(), canary()
    created = await vault.create_login(TITLE, username, password, ORIGINS)
    overview = next(o for o in await vault.list_logins() if o.id == created.id)
    assert ID_PATTERN.fullmatch(overview.id)
    assert overview.title == TITLE
    assert main._hosts(overview.websites) == HOSTS
    assert not hasattr(overview, 'fields')
    assert password not in repr(overview) and username not in repr(overview)


async def test_an_update_raises_the_version_by_one_and_keeps_the_other_field(vault):
    username, password, new = canary(), canary(), canary()
    created = await vault.create_login(TITLE, username, password, ORIGINS)
    updated = await vault.update_fields(created.id, created.version, password=new)
    assert updated.id == created.id
    assert updated.version == created.version + 1

    item = await vault.get(created.id)
    assert item.version == created.version + 1
    assert _values(item) == {'username': username, 'password': new}
    assert item.title == TITLE
    assert main._hosts(item.websites) == HOSTS


async def test_an_update_with_a_stale_version_is_a_conflict_and_changes_nothing(vault):
    created = await vault.create_login(TITLE, canary(), canary(), ORIGINS)
    await vault.update_fields(created.id, created.version, password=canary())
    before = await vault.get(created.id)
    assert before.version == created.version + 1

    stale = vault.update_fields(created.id, created.version, username=canary(), password=canary())
    assert await _code_of(stale) == 'conflict'
    assert await vault.get(created.id) == before


async def test_an_update_adds_a_password_the_login_did_not_have(vault):
    username, password = canary(), canary()
    created = await vault.create_login(TITLE, username, '', ORIGINS)
    updated = await vault.update_fields(created.id, created.version, password=password)
    assert updated.version == created.version + 1
    item = await vault.get(created.id)
    assert _values(item) == {'username': username, 'password': password}
    assert next(f for f in item.fields if f.id == 'password').field_type == 'Concealed'


async def test_an_unknown_id_is_not_found(vault):
    await vault.create_login(TITLE, canary(), canary(), ORIGINS)
    assert await _code_of(vault.get('nosuchlogin')) == 'not_found'
    assert await _code_of(vault.update_fields('nosuchlogin', 1, password=canary())) == 'not_found'
    assert await _code_of(vault.totp_code('nosuchlogin')) == 'not_found'


async def test_a_code_for_a_login_without_a_seed_is_not_found(vault):
    created = await vault.create_login(TITLE, canary(), canary(), ORIGINS)
    assert await _code_of(vault.totp_code(created.id)) == 'not_found'


async def test_a_code_for_a_login_with_a_seed_is_digits(vault):
    created = await vault.create_login(TITLE, canary(), canary(), ORIGINS)
    plant_seed(vault, created.id)
    item = await vault.get(created.id)
    assert [f.field_type for f in item.fields].count('Totp') == 1
    code = await vault.totp_code(created.id)
    assert isinstance(code, str) and re.fullmatch(r'\d{6,8}', code)


async def test_no_public_name_removes_anything(vault):
    public = [name for name in dir(type(vault)) if not name.startswith('_')]
    assert public
    assert not [name for name in public if any(word in name for word in WORDS_THAT_REMOVE)]
