"""The automatic save's write durability, restart recovery and secret containment."""
from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timezone

import pytest

from brokerfakes import ControllerRig, Rig, attempt_of, canary, item_value
from casa_browser.broker_client import arm_capture_then
from casa_browser.broker.journal import Journal
from casa_browser.protocol import SafeNotice
from casa_browser.store import Store

pytestmark = pytest.mark.asyncio


def pending(r):
    return r.journal.pending_notices(0)


def states(r):
    return [row['state'] for row in r.journal.rows()]


def disk_bytes(root):
    return b''.join(p.read_bytes() for p in root.rglob('*') if p.is_file())


def assert_absent(values, *outputs):
    for value in values:
        for output in outputs:
            blob = output if isinstance(output, bytes) else repr(output).encode()
            assert value.encode() not in blob


async def test_automatic_save_is_journaled_before_the_write(tmp_path):
    """Typing and the take do not write; the save's attempt is durable in the journal before
    the SDK write (S5-6's automatic save, through ``Broker._save``)."""
    r = await Rig(tmp_path).setup(item=False)
    value = canary()
    capture = await r.capture([('password', value)], attempt=attempt_of([('password', value)]))
    assert r.writes() == []
    assert len(capture.candidates) == 1
    # The SDK callback uses a separate SQLite connection: a queued transaction is insufficient.
    def durable_attempt():
        independent = Journal(r.journal.path)
        try:
            rows = independent.rows()
        finally:
            independent.close()
        assert any(row['state'] == 'attempt' for row in rows)
        assert all(value not in repr(row) for row in rows)
    r.sdk.before_write = durable_attempt
    saved = await r.save()
    assert saved['saved'] is True
    assert len(r.writes()) == 1
    assert item_value(r.sdk.saved['vault', saved['item_id']], 'password') == value
    assert 'vault_written' in states(r) and 'capture_saved' in states(r)
    assert pending(r) == []      # an automatic save is told by Fido's own reply (slice 5)


async def containment(r, values, caplog):
    """Inspect every controller sink plus a recursive data backup, including SQLite WAL."""
    controller = r.controller
    for n in (await r.call('pending_notices', after=0))['notices']:
        controller.store.add_notice(SafeNotice(0, n['kind'], n['run_id'], n['site_id'], n['text_masked'],
                                              datetime.now(timezone.utc), 'broker:' + str(n['notice_key'])))
    api = [await controller.hello(), [s.to_wire() for s in await controller.list_sites()],
           [n.to_wire() for n in await controller.notices(0)]]
    rows = {table: [tuple(row) for row in controller.store._db.execute('SELECT * FROM "' + table + '"')]
            for (table,) in controller.store._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert_absent(values, vars(controller), vars(r.client), r.exposure_order, rows, api, disk_bytes(r.root),
                  [record.__dict__ for record in caplog.records])
    backup = r.root / 'backup'
    shutil.copytree(r.root, backup, ignore=shutil.ignore_patterns('*.sock', 'profiles', 'backup'))
    assert_absent(values, disk_bytes(backup))


@pytest.mark.parametrize('new_item', [False, True])
async def test_capture_at_submit_reaches_broker_only(tmp_path, caplog, new_item):
    """Real Unix RPC capture/take/save keeps the typed values out of controller wires and all
    sinks: the take answers flags only, the save its receipt."""
    caplog.set_level(logging.DEBUG, logger=None)
    async with ControllerRig(tmp_path) as r:
        await r.setup(item=not new_item)
        if not new_item:
            r.overwritable()
        user, password = canary(), canary()
        typed = [('username', user), ('password', password)]
        taken = await r.capture(typed, attempt=attempt_of(typed))
        assert taken == {'attempt': {'failed': False, 'typed_after': False}, 'incomplete': False}
        receipt = await r.save()
        assert receipt['saved'] and len(r.writes()) == 1
        saved = r.sdk.saved['vault', receipt['item_id']]
        assert item_value(saved, 'username') == user and item_value(saved, 'password') == password
        if new_item:   # casa-test F8: a new item carries the site's name, not its id
            assert saved.title == 'Synthetic site'
        else:
            assert saved.title == 'Synthetic login'     # an existing item keeps its own
        assert_absent([user, password], b''.join(r.wire_replies), r.replies)
        # take_capture is broker-bound and necessarily carries values; its acknowledgement
        # before take (the driver's other reply) must contain no credential.
        assert [op for op, _ in r.driver.wire_replies] == ['begin_live_capture', 'take_capture']
        assert_absent([user, password], r.driver.wire_replies[0][1])
        assert all(v.encode() in r.driver.wire_replies[1][1] for v in (user, password))
        await containment(r, [user, password], caplog)
        assert_absent([user, password], b''.join(r.wire_replies))


@pytest.mark.parametrize('state', ['intent', 'attempt', 'vault_failed', 'vault_unknown', 'vault_written',
                                  'capture_pending', 'capture_saved', 'capture_declined', 'capture_warned'])
async def test_save_crash_recovery_warns(tmp_path, state):
    """Restart turns every unresolved operation/capture into one durable warning."""
    journal = Journal(tmp_path / 'journal.db')
    if state.startswith('capture_'):
        journal.capture_pending('capture', 'run', 'site')
        if state == 'capture_saved':
            journal.begin('operation', 'live', 'site', 'run', 'capture', 'item', 1)
            journal.transition('operation', 'attempt')
            journal.transition('operation', 'vault_written', item_version=2)
        elif state != 'capture_pending':
            journal.capture_finish('capture', state)
    else:
        journal.begin('operation', 'live', 'site', 'run', 'capture', 'item', 1)
        if state != 'intent':
            journal.transition('operation', 'attempt')
        if state not in ('intent', 'attempt'):
            journal.transition('operation', state, item_version=2 if state == 'vault_written' else None)
    journal.close()
    journal = Journal(tmp_path / 'journal.db')
    journal.recover()
    first = journal.pending_notices(0)
    expected = [] if state == 'capture_declined' else [
        'credential_saved' if state in ('vault_written', 'capture_saved') else 'credential_unknown']
    assert [n['kind'] for n in first] == expected
    journal.recover()
    assert journal.pending_notices(0) == first
    journal.close()


@pytest.mark.parametrize('failure', ['refuse', 'raise_before', 'write_then_raise'])
@pytest.mark.parametrize('end', ['none', 'restart'])
async def test_a_failed_automatic_save_warns(tmp_path, failure, end):
    """A failed write warns durably (before and after a restart) and never stores the value;
    an unknown outcome is certified only by a same-value reread in this process."""
    r = await Rig(tmp_path).setup()
    r.overwritable()
    value = canary()
    await r.capture([('password', value)], attempt=attempt_of([('password', value)]))
    r.sdk.fail = failure
    receipt = await r.save()
    if receipt['saved']:
        assert failure == 'write_then_raise'
        assert item_value(r.sdk.saved['vault', 'item'], 'password') == value
        assert len([c for c in r.sdk.calls if c[0] == 'items.get']) >= 2
        return
    assert receipt['reason'] == ('failed' if failure == 'refuse' else 'unknown')
    assert len(r.writes()) == 1
    if end == 'restart':
        await r.restart()
    assert [n['kind'] for n in pending(r)] == ['credential_unknown']
    assert_absent([value], disk_bytes(tmp_path), pending(r))


async def test_live_save_never_certified_by_version(tmp_path):
    """An unrelated version bump after an unknown attempt cannot certify a lost value."""
    r = await Rig(tmp_path).setup()
    r.overwritable()
    value = canary()
    await r.capture([('password', value)], attempt=attempt_of([('password', value)]))
    r.sdk.fail = 'raise_before'
    assert not (await r.save())['saved']
    r.sdk.saved['vault', 'item'].version += 1
    r.sdk.saved['vault', 'item'].title = 'Unrelated edit'
    await r.restart()
    assert [n['kind'] for n in pending(r)] == ['credential_unknown']
    assert item_value(r.sdk.saved['vault', 'item'], 'password') != value


@pytest.mark.parametrize('case', ['item_unreadable', 'take_timeout', 'lost_reply', 'exit_after_take', 'saved', 'declined'])
async def test_take_never_drops_taken_values(tmp_path, case):
    """Done's take journals before the driver's take, keeps an unreadable item's values and
    recovers every lost take; only a save or a judged decline ends it silently."""
    r = await Rig(tmp_path).setup()
    r.overwritable()
    value = canary()
    r.driver.values = [('password', value)]
    r.driver.attempt = attempt_of([('password', value)])
    await r.call('begin_capture', run_id='run', site_id='site', driver_session='driver', purpose='login')
    def durable_capture():
        independent = Journal(r.journal.path)
        try:
            assert any(row['state'] == 'capture_pending' for row in independent.rows())
        finally:
            independent.close()
    r.driver.before_take = durable_capture
    take = dict(run_id='run', site_id='site', capture_id='capture')
    if case == 'item_unreadable':
        r.sdk.read_failure = TimeoutError()
    if case == 'take_timeout':
        r.driver.failure = TimeoutError()
    if case == 'lost_reply':
        taken = asyncio.Event()
        async def hold():
            taken.set()
            await asyncio.Event().wait()
        r.driver.after_take = hold
        task = asyncio.create_task(r.call('take_attempt', **take))
        try:
            await asyncio.wait_for(taken.wait(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    elif case == 'exit_after_take':
        def crash():
            raise SystemExit()
        r.driver.after_take = crash
        with pytest.raises(SystemExit):
            await r.call('take_attempt', **take)
    else:
        reply = await r.call('take_attempt', **take)
        if case != 'take_timeout':
            assert reply['attempt'] == {'failed': False, 'typed_after': False}
            assert len(r.broker._captures['capture'].candidates) == 1
            if case == 'saved':
                assert (await r.call('save_attempt', **take, title='Synthetic site'))['saved']
            elif case == 'declined':
                await r.call('drop_attempt', **take)
    await r.restart()
    # A save ends silently (an automatic save is told by Fido's reply), a judged decline too.
    expected = [] if case in ('saved', 'declined') else ['credential_unknown']
    assert [n['kind'] for n in pending(r)] == expected


async def test_only_login_and_assist_capture(tmp_path):
    """A capture of any other purpose is refused before the driver or SDK is touched."""
    r = await Rig(tmp_path).setup()
    reply = await r.call('begin_capture', run_id='run', site_id='site', driver_session='driver', purpose='other')
    assert reply['error'] in ('refused', 'unsupported')
    assert r.sdk.calls == r.driver.calls == []


@pytest.mark.parametrize('failure', ['none', 'refused', 'timeout'])
async def test_capture_armed_before_live_view(tmp_path, failure):
    """The production controller helper cannot expose input before a delayed capture ack."""
    async with ControllerRig(tmp_path) as r:
        await r.setup()
        entered, release = asyncio.Event(), asyncio.Event()
        order = r.exposure_order
        original = r.source.begin_live_capture
        async def delayed(driver_session):
            order.append('begin_live_capture')
            entered.set()
            await release.wait()
            if failure == 'refused':
                return {'error': 'refused'}
            if failure == 'timeout':
                raise TimeoutError()
            result = await original(driver_session)
            order.append('acknowledged')
            return result
        r.source.begin_live_capture = delayed
        task = asyncio.create_task(arm_capture_then(r.client, run_id='run', site_id='site',
                                  driver_session=r.driver_session, purpose='assist', expose=r.expose_input))
        try:
            # A stub/early return must fail promptly, not hang waiting for the driver.
            waiting = asyncio.create_task(entered.wait())
            try:
                done, _ = await asyncio.wait((task, waiting), timeout=1, return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    await task
                assert entered.is_set()
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            assert order == ['begin_live_capture']
            assert not task.done()
            release.set()
            reply = await asyncio.wait_for(task, 2)
            assert order == (['begin_live_capture', 'acknowledged', 'expose_input']
                             if failure == 'none' else ['begin_live_capture'])
            assert ('capture_id' in reply) is (failure == 'none')
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_notices_import_idempotent(tmp_path):
    """Repeated broker imports keep one delivery notice with no captured value or hint."""
    r = await Rig(tmp_path).setup()
    value = canary()
    await r.capture([('password', value)])
    await r.call('end_run', run_id='run')            # left unsaved: the warning
    store = Store(tmp_path / 'controller.db')
    try:
        for _ in range(2):
            for n in (await r.call('pending_notices', after=0))['notices']:
                store.add_notice(SafeNotice(0, n['kind'], n['run_id'], n['site_id'], n['text_masked'],
                                           datetime.now(timezone.utc), 'broker:' + str(n['notice_key'])))
        notices = store.notices_after(0)
        assert len(notices) == 1 and notices[0].kind == 'credential_unknown'
        assert notices[0].text_masked.startswith('a password you changed on ')
        assert_absent([value], disk_bytes(tmp_path), notices)
    finally:
        store.close()


@pytest.mark.parametrize('end', ['end_run', 'expiry', 'end_capture'])
@pytest.mark.parametrize('changed', [False, True])
async def test_capture_without_attempt_warns_only_for_unsaved_changes(tmp_path, end, changed):
    """A capture with no sign-in attempt is never saved: a changed value warns at its end
    (the run's end, its expiry, the view's release); an unchanged one is silent."""
    r = await Rig(tmp_path).setup()
    value = canary() if changed else item_value(r.sdk.saved['vault', 'item'], 'password')
    capture = await r.capture([('password', value)])
    assert len(capture.candidates) == int(changed)
    if end == 'end_run':
        await r.call('end_run', run_id='run')
    elif end == 'expiry':
        r.clock.advance(901)
        await r.call('pending_notices', after=0)
    else:
        await r.call('end_capture', run_id='run', site_id='site', capture_id='capture')
    expected = ['credential_unknown'] if changed else []
    assert [n['kind'] for n in pending(r)] == expected
    assert r.broker._captures == {}
    assert r.writes() == []


async def test_unreadable_item_at_save_keeps_the_attempt_and_warns(tmp_path):
    """The write's own preliminary read failing cannot lose the value silently or leave an
    unrecorded save attempt (``Broker._save``'s read, after the equality check read)."""
    r = await Rig(tmp_path).setup()
    r.overwritable()
    await r.capture([('password', canary())], attempt=attempt_of([('password', canary())]))
    reads = r.sdk.items.get
    seen = []

    async def get(vault_id, item_id):
        seen.append(item_id)
        if len(seen) > 1:       # the equality check reads it; the write's own read fails
            raise TimeoutError()
        return await reads(vault_id, item_id)
    r.sdk.items.get = get
    receipt = await r.save()
    assert receipt['saved'] is False and receipt['reason'] == 'unknown'
    assert r.writes() == []
    assert 'vault_unknown' in states(r)
    assert [n['kind'] for n in pending(r)] == ['credential_unknown']


async def test_capture_expiry_runs_without_an_rpc(tmp_path):
    """The scheduled expiry path wipes candidates and records the unresolved warning."""
    r = await Rig(tmp_path).setup()
    await r.capture([('password', canary())])
    capture = r.broker._captures['capture']
    assert capture.expiry is not None
    r.clock.advance(901)
    await r.broker._expire_capture('capture')
    assert r.broker._captures == {} and capture.candidates == {}
    assert [n['kind'] for n in pending(r)] == ['credential_unknown']


@pytest.mark.parametrize('outcome', ['armed', 'refused', 'unavailable', 'timeout'])
async def test_capture_journaled_before_arming(tmp_path, outcome):
    """A lost armed capture warns; only a confirmed arming failure closes silently."""
    from casa_browser.broker import BrokerError
    r = await Rig(tmp_path).setup()
    original = r.driver.begin_live_capture

    async def arm(driver_session):
        independent = Journal(r.journal.path)
        try:
            rows = independent.rows()
            assert len(rows) == 1 and rows[0]['state'] == 'capture_pending'
        finally:
            independent.close()
        if outcome in ('refused', 'unavailable'):
            raise BrokerError(outcome)
        if outcome == 'timeout':
            raise TimeoutError()
        return await original(driver_session)

    r.driver.begin_live_capture = arm
    reply = await r.call('begin_capture', run_id='run', site_id='site',
                         driver_session='driver', purpose='login')
    assert reply == ({'capture_id': 'capture'} if outcome == 'armed' else {'error': outcome})
    assert len(r.journal.rows()) == 1
    await r.restart()
    expected = [] if outcome == 'refused' else ['credential_unknown']
    assert [n['kind'] for n in pending(r)] == expected


async def test_take_reuses_arming_journal_row(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.values = [('password', canary())]
    begun = await r.call('begin_capture', run_id='run', site_id='site',
                         driver_session='driver', purpose='login')
    rows = r.journal.rows()
    assert len(rows) == 1 and rows[0]['state'] == 'capture_pending'
    taken = await r.call('take_attempt', run_id='run', site_id='site',
                         capture_id=begun['capture_id'])
    assert taken == {'attempt': None, 'incomplete': False}
    assert len(r.broker._captures[begun['capture_id']].candidates) == 1
    assert r.journal.rows() == rows


@pytest.mark.parametrize('end', ['end_run', 'expiry', 'restart'])
async def test_incomplete_capture_without_candidates_warns(tmp_path, end):
    """Loss of an oversized value cannot turn an empty review into a silent end."""
    from casa_browser.login import CaptureBuffer

    r = await Rig(tmp_path).setup()
    value = canary() * 50
    buffer = CaptureBuffer()
    buffer.record('password', value)
    captured = buffer.take()
    assert captured.values == () and captured.incomplete
    r.driver.incomplete = captured.incomplete
    capture = await r.capture([])
    assert capture.candidates == {} and capture.incomplete is True
    if end == 'end_run':
        await r.call('end_run', run_id='run')
    elif end == 'expiry':
        r.clock.advance(901)
        await r.call('pending_notices', after=0)
    else:
        await r.restart()
    assert [n['kind'] for n in pending(r)] == ['credential_unknown']
    await r.restart()
    notices = pending(r)
    assert [n['kind'] for n in notices] == ['credential_unknown']
    r.journal.recover()
    assert pending(r) == notices
    assert r.writes() == []
    assert_absent([value], disk_bytes(tmp_path), notices)


@pytest.mark.parametrize('purpose', ['login', 'assist'])
async def test_live_capture_expiry_starts_at_review(tmp_path, purpose):
    """A long live view retains its capture; once taken, it has fifteen minutes to be saved."""
    r = await Rig(tmp_path).setup(item=False)
    value = canary()
    begun = await r.call('begin_capture', run_id='run', site_id='site',
                         driver_session='driver', purpose=purpose)
    r.driver.values = [('password', value)]
    r.driver.attempt = attempt_of([('password', value)])
    r.clock.advance(16 * 60)
    assert (await r.call('pending_notices', after=0)) == {'notices': []}
    # Also exercise the timer's entry point: an unreviewed capture has no deadline.
    await r.broker._expire_capture(begun['capture_id'])
    await r.broker._review_capture('run', 'site', begun['capture_id'])
    assert len(r.broker._captures[begun['capture_id']].candidates) == 1
    r.clock.advance(899)
    receipt = await r.save()
    assert receipt['saved'] is True
    assert item_value(r.sdk.saved['vault', receipt['item_id']], 'password') == value
    await r.restart()
    assert pending(r) == []      # saved: nothing to warn about, and Fido says the save


@pytest.mark.parametrize('typed, wording', [
    ([('username', 'newname')], 'a username you typed on {display_name} was not saved'),
    ([('password', 'P')], 'a password you changed on {display_name} may not be in the vault'),
    ([('username', 'newname'), ('password', 'P')], 'a password you changed on {display_name}'),
])
async def test_an_unanswered_capture_warns_by_what_stayed_unsaved(tmp_path, typed, wording):
    """casa-test F11: a capture left unsaved at the end of its view warns by what stayed
    unsaved — a username alone is worded as a username, anything with a password (or an
    incomplete capture) as the password warning."""
    r = await Rig(tmp_path).setup()
    typed = [(role, canary() if value == 'P' else value) for role, value in typed]
    capture = await r.capture(typed)
    assert len(capture.candidates) == len(typed)
    await r.call('end_run', run_id='run')
    notices = pending(r)
    assert [n['kind'] for n in notices] == ['credential_unknown']
    assert notices[0]['text_masked'].startswith(wording)


async def test_an_incomplete_capture_with_only_a_username_warns_about_the_password(tmp_path):
    """A capture that lost values (incomplete) and offered only a username may have lost a
    password: its warning is the password warning, not the username one (casa-test F11)."""
    r = await Rig(tmp_path).setup()
    r.driver.incomplete = True
    capture = await r.capture([('username', 'newname')])
    assert capture.incomplete is True and len(capture.candidates) == 1
    await r.call('end_run', run_id='run')
    notices = pending(r)
    assert [n['kind'] for n in notices] == ['credential_unknown']
    assert notices[0]['text_masked'].startswith('a password you changed on {display_name}')


# -- slice 5 (S5-5, S5-6): the live-view login saved from positive evidence ------------------

import json as _json
from contextlib import asynccontextmanager


SIGNED_IN = {"verdict": "signed_in", "evidence_blocks": [2], "reason": "Log out shown"}
ACCOUNT_PAGE = {"title": "Account", "host": "a.example", "path": "/account",
                "blocks": ["Your invoices", "Log out"], "controls": ["link: Log out"],
                "password_field": False, "_origin": "https://a.example"}


@asynccontextmanager
async def signin_rig(tmp_path, *, item=False):
    """The real controller over the real broker (its SDK and driver doubled, as everywhere in
    this file); the site has no linked item unless ``item``."""
    from dataclasses import replace
    import controllerfakes as cf
    from casa_browser.broker_client import BrokerClient
    (tmp_path / "b").mkdir()
    br = Rig(tmp_path / "b")
    await br.broker.start()
    if item:
        await br.call("register_item", site_id="sa", item_id="item",
                      login_origins=["https://login.a.example"], operator_ref="op")
    else:
        await br.call("register_capture", site_id="sa", login_origins=["https://login.a.example"],
                      operator_ref="op", title="Site A")
    r = cf.Rig(tmp_path, sites=(replace(cf.ACCOUNT, vault_item_id="item" if item else None),))
    client = BrokerClient(store=r.store)

    async def call(op, **args):
        return await br.call(op, **args)
    client.call = call
    r.ctl.broker = client
    await r.start()
    try:
        yield r, br
    finally:
        await r.ctl.shutdown()
        r.close()
        br.journal.close()


async def live_signin(r, br, attempt, *, after=(), at_done=ACCOUNT_PAGE):
    """A run whose login live view ends with ``attempt`` taken at Done."""
    run = await r.run()

    async def expose():
        pass
    await r.ctl.request_live(run.run_id, "login")
    await r.ctl.begin_live(run.run_id, expose)
    br.driver.attempt = attempt
    r.driver.readings["capture"] = {"after": list(after), "at_done": dict(at_done)}
    await r.ctl.live_done(run.run_id)
    return run


async def relay_signin(r, run_id, verdict):
    pending = [j for j in (await r.ctl.pending_judgements(run_id))["judgements"]
               if j["kind"] == "signin"]
    if not pending:
        return None
    j = pending[0]
    import hashlib
    body = ({"no_verdict": "check_unavailable"} if verdict is None else
            {"packet_sha256": hashlib.sha256(j["packet"].encode()).hexdigest(), "verdict": verdict})
    return await r.ctl.apply_judgement(run_id, j["id"], body)


def _attempt(user, password, **flags):
    return {"username": user, "password": password, "failed": False, "typed_after": False,
            **flags}


@pytest.mark.parametrize("verdict", [None, "not_signed_in", "unsure", "evidence_outside"])
async def test_save_requires_signed_in_verdict(tmp_path, verdict):
    async with signin_rig(tmp_path) as (r, br):
        run = await live_signin(r, br, _attempt(canary(), canary()))
        v = None if verdict is None else (
            {**SIGNED_IN, "evidence_blocks": [9]} if verdict == "evidence_outside"
            else {"verdict": verdict, "evidence_blocks": [], "reason": "x"})
        reply = await relay_signin(r, run.run_id, v)
        assert reply["outcome"] == "not_saved"
        assert br.writes() == [] and br.broker._captures == {}
        assert r.store.get_site("sa").vault_item_id is None


@pytest.mark.parametrize("flag", ["failed", "typed_after"])
async def test_failed_attempt_never_saved(tmp_path, flag):
    """A failed attempt (a password field seen again) and one followed by password typing are
    never judged and never saved."""
    async with signin_rig(tmp_path) as (r, br):
        run = await live_signin(r, br, _attempt(canary(), canary(), **{flag: True}))
        assert await relay_signin(r, run.run_id, SIGNED_IN) is None      # nothing to judge
        assert br.writes() == [] and br.broker._captures == {}


async def test_password_typed_after_attempt_blocks_save(tmp_path):
    """Even a signed_in verdict cannot save it: the broker re-checks the attempt itself."""
    async with signin_rig(tmp_path) as (r, br):
        run = await live_signin(r, br, _attempt(canary(), canary()))
        capture = br.broker._captures["capture"]
        capture.attempt["typed_after"] = True                 # learned by the broker alone
        reply = await relay_signin(r, run.run_id, SIGNED_IN)
        assert reply["outcome"] == "not_saved" and br.writes() == []


async def test_done_off_origin_blocks_save(tmp_path):
    async with signin_rig(tmp_path) as (r, br):
        off = {**ACCOUNT_PAGE, "_origin": "https://login.a.example"}   # a login origin, not the site's
        run = await live_signin(r, br, _attempt(canary(), canary()), at_done=off)
        assert await relay_signin(r, run.run_id, SIGNED_IN) is None
        assert br.writes() == [] and br.broker._captures == {}


async def test_unjudged_capture_discarded_at_run_end(tmp_path):
    async with signin_rig(tmp_path) as (r, br):
        run = await live_signin(r, br, _attempt(canary(), canary()))
        assert "capture" in br.broker._captures                # held for the verdict
        await r.ctl.cancel(run.run_id)
        await r.settle()
        assert br.broker._captures == {} and br.writes() == []
        assert [n for n in br.journal.pending_notices(0) if n["kind"] != "credential_saved"] == []


async def test_credential_saved_change_in_record(tmp_path):
    """The gate report's gap: a save is a ``credential_saved`` Change in the run's record, the
    new item is linked to the site, and no value reaches the packet, an event or the record."""
    async with signin_rig(tmp_path) as (r, br):
        user, password = canary(), canary()
        run = await live_signin(r, br, _attempt(user, password))
        packet = next(j["packet"] for j in (await r.ctl.pending_judgements(run.run_id))[
            "judgements"] if j["kind"] == "signin")
        assert _json.loads(packet)["at_done"]["blocks"] == [{"n": 1, "text": "Your invoices"},
                                                            {"n": 2, "text": "Log out"}]
        assert "_origin" not in packet
        reply = await relay_signin(r, run.run_id, SIGNED_IN)
        assert reply == {"outcome": "credential_saved", "generation": reply["generation"],
                         "site": "Site A"}
        site = r.store.get_site("sa")
        item = br.sdk.saved["vault", site.vault_item_id]
        assert (item.title, item_value(item, "username"), item_value(item, "password")) == \
            ("Site A", user, password)
        await r.ctl.finish(run.run_id, __import__("test_controller").manifest())
        since = r.clock() - __import__("datetime").timedelta(days=1)
        rec = (await r.ctl.records("finance", since))[0]
        assert [c.kind for c in rec.changes] == ["credential_saved"]
        events = [e.to_wire() for e in r.events(run.run_id)]
        assert_absent([user, password], packet, events, rec, reply)


async def test_a_working_linked_item_is_kept_and_said_once(tmp_path):
    """A linked item neither rejected in this run nor unverified is not written: the run gets
    one ``credential_not_saved`` event, Fido's cue to say so once."""
    async with signin_rig(tmp_path, item=True) as (r, br):
        before = item_value(br.sdk.saved["vault", "item"], "password")
        run = await live_signin(r, br, _attempt(canary(), canary()))
        reply = await relay_signin(r, run.run_id, SIGNED_IN)
        assert reply["outcome"] == "credential_not_saved"
        assert [e.outcome for e in r.events(run.run_id, "credential_not_saved")] == ["item_kept"]
        assert br.writes() == [] and item_value(br.sdk.saved["vault", "item"], "password") == before


async def test_forget_login_then_the_next_task_asks_again(tmp_path):
    """Slice 5 (operator: "undo" is "ask me again next time"): after Forget login the site
    stays registered; the next task's login runs the vault question again (a ``login_item``
    judgement), and nothing is linked until he answers."""
    async with signin_rig(tmp_path, item=True) as (r, br):
        run = await r.run()
        assert [j["kind"] for j in (await r.ctl.pending_judgements(run.run_id))["judgements"]] == []
        assert await r.ctl.forget("sa", "op") == {"site_id": "sa", "status": "forgotten"}
        await r.settle()
        assert r.store.get_site("sa").vault_item_id is None
        nxt = await r.run(__import__("controllerfakes").spec(request_id="rq2"))
        assert r.state(nxt.run_id) == "running"
        pending = (await r.ctl.pending_judgements(nxt.run_id))["judgements"]
        assert [j["kind"] for j in pending] == ["login_item"]
        assert [e.outcome for e in r.events(nxt.run_id, "login_item_judgement")] == ["pending"]
        assert br.writes() == []


async def test_nothing_the_signin_check_read_is_stored(tmp_path, caplog):
    """Operator ruling (slice 5): what the sign-in check reads — its packet, the page text, its
    reply — is stored nowhere: no file of the app or the broker, no log line at any level, no
    event, no stored response. Only the verdict, its timing and digests may be."""
    import logging
    marker = "PageEcho" + canary()[8:20]
    caplog.set_level(logging.DEBUG)
    async with signin_rig(tmp_path) as (r, br):
        page = {**ACCOUNT_PAGE, "blocks": ["Your invoices", "Log out", "Echo " + marker]}
        run = await live_signin(r, br, _attempt(canary(), canary()), at_done=page)
        packet = next(j["packet"] for j in (await r.ctl.pending_judgements(run.run_id))[
            "judgements"] if j["kind"] == "signin")
        assert marker in packet                                  # positive control
        reply = await relay_signin(r, run.run_id, {**SIGNED_IN, "reason": "it says " + marker})
        assert reply["outcome"] == "credential_saved"
        await r.ctl.finish(run.run_id, __import__("test_controller").manifest())
        r.store.close()
        br.journal.close()
        stored = b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
        assert marker.encode() not in stored
        assert marker not in "\n".join(rec.getMessage() for rec in caplog.records)
