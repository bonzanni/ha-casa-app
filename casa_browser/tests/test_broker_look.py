"""Signing in off the start page, the broker's side: the look's read (``read_sign_in``) and the
sign-in on the judged page (``login(auto=True)``), over the fake driver. Synthetic values."""
from __future__ import annotations

import pytest

from brokerfakes import Rig, Step
from casa_browser.broker import BrokerError

pytestmark = pytest.mark.asyncio


async def _look(r):
    return await r.call('read_sign_in', run_id='run', site_id='site', driver_session='driver')


async def _auto(r, **extra):
    return await r.call('login', run_id='run', site_id='site', driver_session='driver',
                        auto=True, **extra)


def _count(r, op):
    return len([c for c in r.driver.calls if c[0] == op])


def _password_fills(r):
    return [f for f in r.driver.fills if f[1] == 'password']


async def test_the_read_says_what_a_sign_in_would_do_and_does_nothing(tmp_path):
    """The look's read: each planned field by id, the role the page marks (or none), its
    label, whether it is a password field and its button; with 0 fills, 0 submits, 0 passkey
    clicks and 0 vault reads; never a handle or a value."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('password',))]
    r.driver.plain = ('Username',)
    reply = await _look(r)
    assert reply == {'found': True, 'passkey': False, 'fields': [
        {'id': 'f1', 'role': 'password', 'label': 'Password', 'password': True, 'button': 'Login'},
        {'id': 'f2', 'role': None, 'label': 'Username', 'password': False, 'button': 'Login'}]}
    assert r.driver.fills == [] and _count(r, 'submit') == 0 and _count(r, 'choose_passkey') == 0
    assert not [c for c in r.sdk.calls if c[0].startswith('items.')]
    assert set(r.broker._looks) == {('run', 'site')}


async def test_the_checks_mapping_reaches_the_judged_steps_discovery(tmp_path):
    """R9: the plain fields the check mapped are named to the driver's discovery (by the
    look's ids, with the look's mark), so the page's unmarked username is filled."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step()]
    await _look(r)
    mark = r.broker._looks[('run', 'site')]['mark']
    seen = []
    real = r.driver.find_login_fields

    async def find(driver_session, page_generation, **kw):
        seen.append(kw)
        return await real(driver_session, page_generation, **kw)
    r.driver.find_login_fields = find
    assert await _auto(r, fields={'username': 'f2'}) == {'status': 'logged_in',
                                                         'reason': 'completed'}
    assert seen[0] == {'sign_in_look': mark, 'sign_in_fields': {'f2': 'username'}}
    r.driver.index = 0
    await _look(r)
    seen.clear()
    await _auto(r)                                          # no mapping: the look alone
    assert seen[0] == {'sign_in_look': r.driver.calls and seen[0]['sign_in_look'],
                       'sign_in_fields': {}}
    assert seen[0]['sign_in_look'] != mark                  # the new look's own mark


@pytest.mark.parametrize('fields', [{'username': 'x1'}, {'user': 'f1'}, {'username': 'f1',
                                    'password': 'f1'}, {'username': 3}, ['f1']])
async def test_a_mapping_outside_its_shape_is_refused(tmp_path, fields):
    r = await Rig(tmp_path).setup()
    assert await _auto(r, fields=fields) == {'error': 'unsupported'}


@pytest.mark.parametrize('step, found', [
    (Step(), False),                                   # no field: nothing more
    (Step(('username',), origin_ok=False), False),     # off the login origins
    (Step(passkey_hint=True), True),                   # the site's passkey request
    (Step(passkey_frame=True), True),                  # 1Password's prompt
])
async def test_only_a_sign_in_field_or_a_passkey_goes_on(tmp_path, step, found):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [step]
    reply = await _look(r)
    assert reply['found'] is found
    assert (('run', 'site') in r.broker._looks) is found


async def test_a_plain_field_alone_goes_to_the_check(tmp_path):
    """Terra (R9 round): a username-only sign-in whose field the page does not mark is
    planned and judged; the check's mapping then names it."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(())]
    r.driver.plain = ('Username',)
    reply = await _look(r)
    assert reply['found'] is True and [f['role'] for f in reply['fields']] == [None]


async def test_any_id_the_plan_can_give_is_accepted(tmp_path):
    """Astra (R9 round): the plan numbers fields without a cap below a million."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step()]
    await _look(r)
    assert await _auto(r, fields={'username': 'f1000'}) == {'status': 'logged_in',
                                                            'reason': 'completed'}


async def test_a_read_that_fails_is_an_error_and_keeps_no_look(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.read_failure = BrokerError('refused')
    assert await _look(r) == {'error': 'refused'}
    assert r.broker._looks == {}


async def test_an_unlinked_site_is_never_read(tmp_path):
    r = await Rig(tmp_path).setup(item=False)
    assert await _look(r) == {'found': False}
    assert _count(r, 'read_sign_in') == 0


@pytest.mark.parametrize('roles, fills', [(('username',), 1), (('username', 'password'), 2)])
async def test_the_judged_page_is_signed_into_as_it_is(tmp_path, roles, fills):
    """R3: a username-only first step fills the username and submits once; a username and
    password form fills both and submits once."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(roles), Step()]
    await _look(r)
    assert await _auto(r) == {'status': 'logged_in', 'reason': 'completed'}
    assert len(r.driver.fills) == fills and _count(r, 'submit') == 1
    assert _count(r, 'sign_in_bound') == fills + 1          # before each fill and the submit


async def test_a_sign_in_without_its_look_does_nothing(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step()]
    assert await _auto(r) == {'status': 'no_form', 'reason': 'page_moved'}
    assert r.driver.fills == [] and _count(r, 'settle') == 0
    await _look(r)
    await _auto(r)
    assert await _auto(r) == {'status': 'no_form', 'reason': 'page_moved'}   # one look, one sign-in


@pytest.mark.parametrize('second', [Step(('username', 'password')), Step(('password',))])
async def test_the_password_is_sent_once_per_look(tmp_path, second):
    """The same form again, or only its password field (role contraction): exactly one password
    submit, then ``password_again``."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), second, Step()]
    await _look(r)
    assert await _auto(r) == {'status': 'needs_live_view', 'reason': 'password_again'}
    assert len(_password_fills(r)) == 1 and _count(r, 'submit') == 1


async def test_a_password_sent_by_the_look_before_the_reload_is_not_sent_again(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step()]
    await _look(r)
    assert await _auto(r, password_sent=True) == {'status': 'needs_live_view',
                                                  'reason': 'password_again'}
    assert r.driver.fills == [] and _count(r, 'submit') == 0


async def test_after_a_passkey_answer_a_newsletter_box_is_not_filled(tmp_path):
    """Astra r2: after the judged passkey prompt was answered, a username-only box on the page
    after it is the page after the sign-in: 0 fills, ``no_form/passkey``."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(passkey_frame=True), Step(('username',))]
    await _look(r)
    assert await _auto(r) == {'status': 'no_form', 'reason': 'passkey'}
    assert r.driver.fills == [] and len(r.driver.passkeys) == 1


async def test_a_passkey_offered_on_a_later_step_is_never_answered(tmp_path):
    """R5: only the judged page's passkey is answered."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username',)), Step(passkey_frame=True), Step()]
    await _look(r)
    assert await _auto(r) == {'status': 'needs_live_view', 'reason': 'challenge'}
    assert _count(r, 'choose_passkey') == 0 and r.driver.passkeys == []


async def test_no_guest_is_asked_for_after_the_judged_step(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username',)), Step(('password',), passkey_hint=True), Step()]
    await _look(r)
    assert await _auto(r, guest_ask=True) == {'status': 'logged_in', 'reason': 'completed'}


async def test_the_judged_step_asks_for_the_guest_and_says_what_it_sent(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username',), passkey_hint=True)]
    await _look(r)
    assert await _auto(r, guest_ask=True) == {'status': 'needs_guest', 'reason': 'passkey',
                                              'password_sent': False}
    assert r.driver.fills == []


@pytest.mark.parametrize('failure, reply', [
    ('field_free', {'status': 'no_form', 'reason': 'no_form'}),
    ('error', {'status': 'no_form', 'reason': 'probe_failed'})])
async def test_a_judged_page_that_cannot_be_read_or_shows_no_form_claims_nothing(
        tmp_path, failure, reply):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password'))]
    await _look(r)
    if failure == 'field_free':
        r.driver.steps = [Step()]
    else:
        async def broken(driver_session, page_generation, **_kw):
            raise BrokerError('refused')
        r.driver.find_login_fields = broken
    assert await _auto(r) == reply
    assert r.driver.fills == [] and _count(r, 'find_login_fields') <= 1


@pytest.mark.parametrize('at', [1, 2, 3])
async def test_a_binding_that_no_longer_holds_stops_the_sign_in(tmp_path, at):
    """R7: the binding is read before each fill and the submit of the judged step; once it no
    longer holds (another document, other text, another destination, a field outside the bound
    documents) nothing more is done: ``no_form/page_moved``."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step()]
    await _look(r)
    r.driver.bound = lambda n: (True, {'f1': ['other']}) if n >= at else None
    assert await _auto(r) == {'status': 'no_form', 'reason': 'page_moved'}
    assert len(r.driver.fills) == at - 1 and _count(r, 'submit') == 0


async def test_a_mark_that_is_gone_stops_the_sign_in(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step()]
    await _look(r)
    r.driver.bound = lambda n: (False, None)
    assert await _auto(r) == {'status': 'no_form', 'reason': 'page_moved'}
    assert r.driver.fills == [] and _count(r, 'submit') == 0


async def test_a_named_field_gone_from_the_page_is_a_page_that_moved(tmp_path):
    """A form drawn anew has new fields, which the look did not mark: nothing filled, and the
    controller's one fresh look gets a chance."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password'))]
    await _look(r)
    r.driver.steps = [Step()]
    assert await _auto(r, fields={'username': 'f1', 'password': 'f2'}) == {
        'status': 'no_form', 'reason': 'page_moved'}
    assert r.driver.fills == []


async def test_a_field_the_look_never_saw_stops_the_sign_in(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step()]
    await _look(r)
    r.driver.bound = lambda n: (True, {'f9': ['facts']})
    assert await _auto(r) == {'status': 'no_form', 'reason': 'page_moved'}
    assert r.driver.fills == [] and _count(r, 'submit') == 0


async def test_a_passkey_prompt_that_moved_is_never_answered(tmp_path):
    """The notification frame's new document no longer holds the look's mark: 0
    ``choose_passkey``."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(passkey_frame=True), Step()]
    await _look(r)
    r.driver.bound = lambda n: (False, None)
    assert await _auto(r) == {'status': 'no_form', 'reason': 'page_moved'}
    assert _count(r, 'choose_passkey') == 0


async def test_an_unbound_frame_that_keeps_moving_never_stops_the_sign_in(tmp_path):
    """Astra r5: an unbound iframe's navigations change the page generation, never the
    binding: the step reads the page again and signs in (1 login, 2 fills, 1 submit)."""
    from test_broker import _moving
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password')), Step()]
    await _look(r)
    _moving(r, {('fill', 1): 'changed', ('settle', 2): 'changed'})
    assert await _auto(r) == {'status': 'logged_in', 'reason': 'completed'}
    assert sorted({f[1] for f in r.driver.fills}) == ['password', 'username']
    assert _count(r, 'submit') == 1


@pytest.mark.parametrize('third, reply, clicks', [
    (Step(('password',)), {'status': 'needs_live_view', 'reason': 'password_again'}, 0),
    (Step(passkey_frame=True), {'status': 'needs_live_view', 'reason': 'challenge'}, 0)])
async def test_a_code_continuation_keeps_the_looks_limits(tmp_path, third, reply, clicks):
    """Astra r3: judged username/password, then an emailed code, then a password form: 1
    password fill and 2 submits in all, then ``password_again``; a passkey prompt after the
    code is never answered."""
    from test_broker import _no_totp
    r = await Rig(tmp_path).setup()
    _no_totp(r)
    r.driver.steps = [Step(('username', 'password')), Step(('one_time_code',)), third, Step()]
    await _look(r)
    asked = await _auto(r)
    assert asked['status'] == 'needs_code'
    again = await r.call('fill_code', run_id='run', site_id='site', driver_session='driver',
                         page_generation=asked['page_generation'], code='123456')
    assert again == reply
    assert len(_password_fills(r)) == 1 and _count(r, 'submit') == 2
    assert _count(r, 'choose_passkey') == clicks


@pytest.mark.parametrize('extra', [{'auto': 1}, {'password_sent': 'yes'}, {'auto': None}])
async def test_the_new_login_flags_must_be_booleans(tmp_path, extra):
    r = await Rig(tmp_path).setup()
    assert await r.call('login', run_id='run', site_id='site', driver_session='driver',
                        **extra) == {'error': 'unsupported'}


async def test_a_look_ends_with_its_run(tmp_path):
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username',))]
    await _look(r)
    await r.call('end_run', run_id='run')
    assert r.broker._looks == {}


@pytest.mark.parametrize('roles', [('username',), ('username', 'password')])
async def test_a_page_change_before_the_submit_click_keeps_the_judged_step(tmp_path, roles):
    """Astra (applied diff r2): an unbound frame's navigation that stops the submit before its
    click is no submit: the step reads the page again under the same binding and submits once."""
    from test_broker import _moving
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(roles), Step()]
    await _look(r)
    _moving(r, {('submit', 1): 'changed'})
    assert await _auto(r) == {'status': 'logged_in', 'reason': 'completed'}
    assert _count(r, 'submit') == 1            # the step typed again, then submitted once


async def test_a_box_the_check_did_not_map_keeps_the_password_at_home(tmp_path):
    """The judged step with a shown empty box the mapping left out: the live view, 0 fills."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('password',), empty_box=True)]
    await _look(r)
    assert await _auto(r, fields={'password': 'f1'}) == {'status': 'needs_live_view',
                                                         'reason': 'empty_field'}
    assert r.driver.fills == [] and _count(r, 'submit') == 0


async def test_a_look_waits_for_the_held_button_under_its_binding(tmp_path):
    """The binding is asked after the wait too, right before the click: a page that moved
    during the wait is not pressed."""
    r = await Rig(tmp_path).setup()
    r.driver.steps = [Step(('username', 'password'), submit_ready=False), Step()]
    await _look(r)
    checks = {'n': 0}

    def bound(_count_so_far):
        checks['n'] += 1
        return (False, 'other') if checks['n'] >= 3 else None      # 2 fills, then the click
    r.driver.bound = bound
    assert await _auto(r) == {'status': 'no_form', 'reason': 'page_moved'}
    assert _count(r, 'fill') == 2 and _count(r, 'submit') == 0
    assert _count(r, 'submit_usable') == 1
