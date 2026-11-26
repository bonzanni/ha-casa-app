"""The vault kept in the app's own store: codes, what survives on disk, and the key."""
from __future__ import annotations

import asyncio
import base64
import os
import re
import stat

import pytest

from brokerfakes import canary
from casa_browser import vault_store
from casa_browser.broker import BrokerError
from casa_browser.broker.builtin import VAULT_ID, BuiltinVault, one_time_code
from casa_browser.vault_store import DATABASE_NAME, KEY_NAME

ORIGINS = ('https://login.example',)
TITLE = 'Synthetic site'
SEED = 'JBSWY3DPEHPK3PXP'
FAILURE_LINE = 'casa-browser broker: the saved logins could not be opened'

# RFC 6238, appendix B: the shared secret is this ASCII text, as long as the hash.
RFC_SECRETS = {
    'SHA1': b'12345678901234567890',
    'SHA256': b'12345678901234567890123456789012',
    'SHA512': b'1234567890123456789012345678901234567890123456789012345678901234',
}
RFC_ROWS = [
    ('SHA1', 59, '94287082'), ('SHA256', 59, '46119246'), ('SHA512', 59, '90693936'),
    ('SHA1', 1111111109, '07081804'), ('SHA256', 1111111109, '68084774'),
    ('SHA512', 1111111109, '25091201'),
    ('SHA1', 2000000000, '69279037'), ('SHA256', 2000000000, '90698825'),
    ('SHA512', 2000000000, '38618901'),
]


def _rfc_address(algorithm):
    secret = base64.b32encode(RFC_SECRETS[algorithm]).decode()
    return f'otpauth://totp/Example:someone?secret={secret}&algorithm={algorithm}&digits=8&period=30'


def _plant_seed(vault, item_id, seed):
    """Seeds are not written through the vault: it takes only login values and websites."""
    vault._store.set_field(item_id, label='one-time password', type='OTP', value=seed,
                           writer='live_view')


def _values(item):
    return {f.id: f.value for f in item.fields if f.id in ('username', 'password')}


def _snapshot(directory):
    """The directory's names and every file's bytes, or None when it does not exist."""
    if not directory.exists():
        return None
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


async def _code_of(call):
    with pytest.raises(BrokerError) as raised:
        await call
    return raised.value.code


async def _every_operation_is_unavailable(vault, item_id='anything'):
    assert vault.available is False
    for call in (vault.resolve_vault_id(),
                 vault.get(item_id),
                 vault.list_logins(),
                 vault.create_login(TITLE, canary(), canary(), ORIGINS),
                 vault.update_fields(item_id, 1, password=canary()),
                 vault.totp_code(item_id)):
        assert await _code_of(call) == 'unavailable'


async def _a_store_with_a_login(directory, env_key=None):
    """A store on disk holding one login; returns the login's id."""
    vault = BuiltinVault(directory, env_key)
    assert vault.available is True
    return (await vault.create_login(TITLE, canary(), canary(), ORIGINS)).id


# -- one-time codes -------------------------------------------------------------

@pytest.mark.parametrize('algorithm, at, code', RFC_ROWS)
def test_codes_match_rfc_6238(algorithm, at, code):
    assert one_time_code(_rfc_address(algorithm), at) == code


def test_a_16_character_seed_gives_a_code():
    code = one_time_code(SEED, 59)
    assert re.fullmatch(r'\d{6}', code)
    assert code == one_time_code(f'otpauth://totp/Example?secret={SEED}', 59)
    assert code == one_time_code(SEED, 31)          # the same 30 seconds
    assert code != one_time_code(SEED, 60)          # the next 30 seconds


def test_a_seed_with_spaces_and_lower_case_reads_the_same():
    assert one_time_code('jbsw y3dp ehpk 3pxp', 59) == one_time_code(SEED, 59)
    assert one_time_code(' ' + SEED + '\n', 59) == one_time_code(SEED, 59)
    # Padding may be written or left out: a one-byte secret is 'ME' and six '='.
    assert one_time_code('ME======', 59) == one_time_code('ME', 59)


@pytest.mark.parametrize('seed', [
    '', '   ', 'not base 32!', 'JBSWY3DPEHPK3PX1', 'A',
    'otpauth://hotp/Example?secret=JBSWY3DPEHPK3PXP&counter=1',
    'otpauth://totp/Example',
    'otpauth://totp/Example?secret=',
    'otpauth://totp/Example?secret=JBSWY3DPEHPK3PXP&digits=5',
    'otpauth://totp/Example?secret=JBSWY3DPEHPK3PXP&digits=9',
    'otpauth://totp/Example?secret=JBSWY3DPEHPK3PXP&digits=six',
    'otpauth://totp/Example?secret=JBSWY3DPEHPK3PXP&period=0',
    'otpauth://totp/Example?secret=JBSWY3DPEHPK3PXP&algorithm=MD5',
])
def test_an_unreadable_seed_is_not_found(seed):
    with pytest.raises(BrokerError) as raised:
        one_time_code(seed, 59)
    assert raised.value.code == 'not_found'


async def test_a_stored_seed_gives_the_code_for_the_clocks_time(tmp_path):
    vault = BuiltinVault(tmp_path / 'vault', None, clock=lambda: 59)
    login = await vault.create_login(TITLE, canary(), canary(), ORIGINS)
    _plant_seed(vault, login.id, _rfc_address('SHA1'))
    assert await vault.totp_code(login.id) == '94287082'


async def test_the_seed_is_in_no_field_value_of_get(tmp_path):
    vault = BuiltinVault(tmp_path / 'vault', None)
    login = await vault.create_login(TITLE, canary(), canary(), ORIGINS)
    _plant_seed(vault, login.id, SEED)
    item = await vault.get(login.id)
    code_fields = [f for f in item.fields if f.field_type == 'Totp']
    assert [(f.id, f.title, f.value, f.section_id) for f in code_fields] == [
        ('one-time password', 'one-time password', '', None)]
    assert not [f for f in item.fields if SEED in f.value]
    assert SEED not in repr(item)
    assert item.version == login.version          # planting a seed is not a login change


# -- what a login is ------------------------------------------------------------

async def test_a_login_is_in_the_one_vault_and_shows_no_value_when_printed(tmp_path):
    vault = BuiltinVault(tmp_path / 'vault', None)
    username, password = canary(), canary()
    login = await vault.create_login(TITLE, username, password, ORIGINS)
    assert await vault.resolve_vault_id() == VAULT_ID == 'builtin'
    assert (login.vault_id, login.category, login.version) == (VAULT_ID, 'Login', 1)
    assert [f.id for f in login.fields] == ['username', 'password']
    assert username not in repr(login) and password not in repr(login)
    assert await vault.vault_accessors() is None


async def test_an_id_that_is_another_logins_title_is_not_found(tmp_path):
    vault = BuiltinVault(tmp_path / 'vault', None)
    login = await vault.create_login('example.com', canary(), canary(), ORIGINS)
    _plant_seed(vault, login.id, SEED)
    assert await _code_of(vault.get('example.com')) == 'not_found'
    assert await _code_of(vault.update_fields('example.com', 1, password=canary())) == 'not_found'
    assert await _code_of(vault.totp_code('example.com')) == 'not_found'
    assert (await vault.get(login.id)).version == 1


@pytest.mark.parametrize('title, username, password', [
    ('Bank "Example" login', 'someone@example.com', 'plain'),
    (TITLE, 'someone@example.com', 'first line\nsecond line'),
    ('Señal · 例え', 'élève@exämple.jp', 'pässwörd-密码'),
    ('-title', '-rf', '--password=x'),
], ids=['a quote in the title', 'a newline in the password', 'text outside ASCII',
        'a leading hyphen'])
async def test_values_survive_a_reopen(tmp_path, title, username, password):
    directory = tmp_path / 'vault'
    login = await BuiltinVault(directory, None).create_login(title, username, password, ORIGINS)

    again = BuiltinVault(directory, None)
    item = await again.get(login.id)
    assert item.title == title
    assert _values(item) == {'username': username, 'password': password}
    assert [o.title for o in await again.list_logins()] == [title]


async def test_websites_round_trip_in_order(tmp_path):
    directory = tmp_path / 'vault'
    origins = ['https://b.example', 'https://a.example', 'https://login.c.example:8443']
    vault = BuiltinVault(directory, None)
    login = await vault.create_login(TITLE, canary(), canary(), origins)
    assert [w.url for w in login.websites] == origins
    await vault.update_fields(login.id, login.version, password=canary())

    again = BuiltinVault(directory, None)
    assert [w.url for w in (await again.get(login.id)).websites] == origins
    assert [[w.url for w in o.websites] for o in await again.list_logins()] == [origins]

    bare = await again.create_login('No website', canary(), canary(), [])
    assert (await again.get(bare.id)).websites == ()


# -- writes ---------------------------------------------------------------------

async def test_two_updates_from_one_version_give_one_conflict(tmp_path):
    vault = BuiltinVault(tmp_path / 'vault', None)
    login = await vault.create_login(TITLE, canary(), canary(), ORIGINS)
    first, second = canary(), canary()
    results = await asyncio.gather(
        vault.update_fields(login.id, login.version, password=first),
        vault.update_fields(login.id, login.version, password=second),
        return_exceptions=True)
    written = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, BrokerError)]
    assert len(written) == 1 and [r.code for r in refused] == ['conflict']
    item = await vault.get(login.id)
    assert item.version == login.version + 1 == written[0].version
    assert _values(item)['password'] in (first, second)


async def test_a_write_that_fails_leaves_the_old_login(tmp_path, monkeypatch):
    directory = tmp_path / 'vault'
    vault = BuiltinVault(directory, None)
    username, password, new = canary(), canary(), canary()
    login = await vault.create_login(TITLE, username, password, ORIGINS)

    real_replace, failures = os.replace, []

    def fail_once(source, target):
        if not failures:
            failures.append(target)
            raise OSError('the disk refused')
        return real_replace(source, target)

    monkeypatch.setattr(os, 'replace', fail_once)
    assert await _code_of(vault.update_fields(login.id, login.version, password=new)) == 'unavailable'
    assert len(failures) == 1

    item = await vault.get(login.id)
    assert item.version == login.version
    assert _values(item) == {'username': username, 'password': password}

    updated = await vault.update_fields(login.id, login.version, password=new)
    assert updated.version == login.version + 1
    assert _values(await BuiltinVault(directory, None).get(login.id)) == {
        'username': username, 'password': new}


# -- the files and the key ------------------------------------------------------

async def test_first_open_makes_the_directory_0700_and_both_files_0600(tmp_path):
    directory = tmp_path / 'vault'
    vault = BuiltinVault(directory, None)
    assert vault.available is True
    assert _mode(directory) == 0o700
    assert sorted(path.name for path in directory.iterdir()) == sorted([DATABASE_NAME, KEY_NAME])
    assert _mode(directory / DATABASE_NAME) == 0o600
    assert _mode(directory / KEY_NAME) == 0o600
    await vault.create_login(TITLE, canary(), canary(), ORIGINS)
    assert _mode(directory / DATABASE_NAME) == 0o600


async def test_a_key_from_outside_is_used_and_no_key_file_is_written(tmp_path):
    directory = tmp_path / 'vault'
    username = canary()
    vault = BuiltinVault(directory, 'a key from outside')
    assert vault.available is True
    login = await vault.create_login(TITLE, username, canary(), ORIGINS)
    assert [path.name for path in directory.iterdir()] == [DATABASE_NAME]

    again = BuiltinVault(directory, 'a key from outside')
    assert _values(await again.get(login.id))['username'] == username
    assert [path.name for path in directory.iterdir()] == [DATABASE_NAME]


@pytest.mark.parametrize('existing', [True, False], ids=['a store with its key', 'no directory yet'])
async def test_a_whitespace_only_outside_key_is_unavailable_and_changes_nothing(
        tmp_path, monkeypatch, existing):
    directory = tmp_path / 'vault'
    item_id = await _a_store_with_a_login(directory) if existing else 'anything'
    before = _snapshot(directory)
    assert (before is not None) is existing

    opened = []
    real_open = vault_store.Store.open
    monkeypatch.setattr(vault_store.Store, 'open',
                        lambda *args: opened.append(args) or real_open(*args))
    vault = BuiltinVault(directory, '   ')
    await _every_operation_is_unavailable(vault, item_id)
    assert opened == []
    assert _snapshot(directory) == before


@pytest.mark.parametrize('key_file', ['missing', 'another key'],
                         ids=['no key file', 'a key file with another key'])
async def test_a_file_without_its_key_is_unavailable_and_untouched(tmp_path, key_file):
    directory = tmp_path / 'vault'
    item_id = await _a_store_with_a_login(directory)
    key = directory / KEY_NAME
    if key_file == 'missing':
        key.unlink()
    else:
        key.write_text('a different key\n')
    before = _snapshot(directory)
    assert list(before) == sorted([DATABASE_NAME] + ([KEY_NAME] if key_file != 'missing' else []))

    await _every_operation_is_unavailable(BuiltinVault(directory, None), item_id)
    assert _snapshot(directory) == before


@pytest.mark.parametrize('made_with', [None, 'the right key'],
                         ids=['its own key file', 'a key from outside'])
async def test_a_file_with_a_wrong_outside_key_is_unavailable_and_untouched(tmp_path, made_with):
    # A store that made its own key keeps that file; a wrong outside key must not fall back to it.
    directory = tmp_path / 'vault'
    item_id = await _a_store_with_a_login(directory, made_with)
    before = _snapshot(directory)
    assert sorted(before) == sorted([DATABASE_NAME] + ([KEY_NAME] if made_with is None else []))

    await _every_operation_is_unavailable(BuiltinVault(directory, 'a wrong key'), item_id)
    assert _snapshot(directory) == before


@pytest.mark.parametrize('case', ['no key', 'a wrong key', 'a blank key'])
async def test_the_failure_line_carries_no_exception_text(tmp_path, capsys, case):
    directory = tmp_path / 'vault'
    await _a_store_with_a_login(directory, 'the right key' if case == 'a wrong key' else None)
    if case == 'no key':
        (directory / KEY_NAME).unlink()
    capsys.readouterr()

    env_key = {'no key': None, 'a wrong key': 'a wrong key', 'a blank key': ' \t'}[case]
    assert BuiltinVault(directory, env_key).available is False
    printed = capsys.readouterr()
    assert printed.err == FAILURE_LINE + '\n'
    assert printed.out == ''
