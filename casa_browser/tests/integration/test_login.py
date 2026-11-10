"""Synthetic credential flows through Chrome, the enforcing addon and a uid-separated broker."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qs

import pytest

import proxyharness as h
from brokerfakes import canary, make_item
from browserprobe import run_scenario
from sitefixture import ChromeSites

pytestmark = pytest.mark.docker
LOGIN, ACCOUNT, OTHER = 'login.example', 'site.example', 'outside.example'


def fixture_config(tmp_path, *, totp=True, identifier_only=False):
    """Test vault persistence belongs only to this harness, never production configuration."""
    tmp_path.chmod(0o755)
    # The image tests run as root; every ancestor must let service users reach the fixture.
    for p in tmp_path.parents:
        if str(p).startswith('/tmp/'):
            p.chmod(0o755)
    root = tmp_path / 'broker'
    root.mkdir(mode=0o711)
    os.chown(root, 1102, 1102)
    controller = tmp_path / 'controller'
    controller.mkdir(mode=0o711)
    os.chown(controller, 1103, 1103)
    seed = make_item()
    if not totp:            # slice 6: an item with no authenticator (the site sends a code)
        seed.fields[:] = [f for f in seed.fields if f.field_type != 'Totp']
    if identifier_only:     # slice 5n (O2): an item holding just the email it signs in with
        seed.fields[:] = [f for f in seed.fields if f.id == 'username' and not f.section_id]
    snapshot = root / 'fake-vault.json'
    snapshot.write_text(json.dumps([seed.model_dump(mode='json', by_alias=True)]))
    os.chown(snapshot, 1102, 1102)
    config = {'root': str(root), 'driver_socket': str(controller / 'driver.sock'),
              'options': {'op_token': canary(), 'vault': 'Synthetic vault',
                          'guest_sign_in_address': 'acct.1password.eu', 'guest_email': canary(),
                          'guest_password': canary(), 'guest_secret_key': canary()}}
    return config


def instruction(config, action, login=LOGIN, **args):
    return {'credential': {'action': action, 'broker_socket': config['root'] + '/rpc.sock',
                           'driver_socket': config['driver_socket'],
                           'login_origins': ['https://' + login], **args}}


def install(sites, login=LOGIN, manual=False):
    s = h.site(origins=('https://' + ACCOUNT,), login=('https://' + login,))
    sites.install(h.account_policy(sites=(s,)))
    sites.call(setattr, sites.harness.proxy, 'controller_uid', 1103)


def form(role, action):
    types = 'password' if 'password' in role else 'text'
    return ('text/html', (f'<form method=post action="{action}"><input id=field name=value '
                         f'type={types} autocomplete={role}><button>Continue</button></form>').encode(), {})


def vault(config):
    return json.loads((Path(config['root']) / 'fake-vault.json').read_text())


@pytest.mark.parametrize('same_origin', [False, True])
def test_login_from_registration(tmp_path, same_origin):
    """Fresh registration supplies username, password and fresh OTP twice after expiry."""
    config = fixture_config(tmp_path)
    host = ACCOUNT if same_origin else LOGIN
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites, host)
        sites.pages['/username'] = form('username', '/password')
        sites.pages['/password'] = form('current-password', '/otp')
        # As real sites do: the last step posts to the login origin, which redirects to the account.
        sites.pages['/otp'] = form('one-time-code', '/finish')
        sites.pages['/finish'] = ('text/html', b'', {'Location': 'https://' + ACCOUNT + '/done'})
        sites.status['/finish'] = 303
        sites.pages['/done'] = ('text/html', b'<p>Signed in</p>', {'Set-Cookie': 'sid=active; Secure; Path=/; Max-Age=1'})
        steps = [{'open': {'site_id': 's1'}}, instruction(config, 'setup', host)]
        for attempt in range(2):
            if attempt:
                steps += [{'wait': 2}, {'cookies': 'https://' + ACCOUNT}]
            steps += [instruction(config, 'open_login', host), {'goto': 'https://' + host + '/username'},
                      instruction(config, 'login', host)]
        result = run_scenario(steps, broker_fixture=config)
        assert [r['status'] for r in result if isinstance(r, dict) and 'status' in r] == ['logged_in', 'logged_in']
        assert result[6] == []  # the fixture's one-second session cookie expired
        posts = [r for r in sites.receipts.requests if r[0] == 'POST']
        assert [p[2] for p in posts] == ['/password', '/otp', '/finish'] * 2
        assert posts[2][3] != posts[5][3]  # fresh OTP at the second use
        assert sites.receipts.connections > 0 and sites.receipts.bytes > 0


def test_login_same_origin(tmp_path):
    """Login and account on one origin can complete and log in again after expiry."""
    test_login_from_registration(tmp_path, True)


def test_lease_closes_at_landing_document(tmp_path):
    """The login lease closes at the landing document; afterwards a top-level document off the
    site opens no upstream connection and sends no bytes (design §4's document rule)."""
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, ACCOUNT, OTHER]) as sites:
        install(sites)
        sites.pages['/password'] = form('current-password', '/finish')
        sites.pages['/finish'] = ('text/html', b'', {'Location': 'https://' + ACCOUNT + '/done'})
        sites.status['/finish'] = 303
        sites.pages['/done'] = ('text/html', b'<p>Signed in</p>', {})
        first = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                              instruction(config, 'open_login'), {'goto': 'https://' + LOGIN + '/password'},
                              instruction(config, 'login')], broker_fixture=config)
        assert first[-1]['status'] == 'logged_in'
        assert any(r[0] == 'POST' and r[2] == '/finish' for r in sites.receipts.requests)
        assert any(r[0] == 'GET' and r[2] == '/done' for r in sites.receipts.requests)
        assert sites.harness.addon.policy.login_lease is None
        # Use a blank browser page and an off-site document only: no page dial masks the receipt count.
        sites.receipts.reset()
        result = run_scenario([{'open': {'site_id': 's1'}}, {'goto': 'https://' + OTHER + '/later'}])
        assert result[-1] == 403 or str(result[-1]).startswith('error:'), result[-1]   # refused
        assert sites.receipts.connections == 0 and sites.receipts.bytes == 0
        assert sites.receipts.requests == []
        # (its origin may be withheld from the event while a credential window is closing)
        assert any(not e.allow and e.reason == 'site_lock' and e.resource_type == 'document'
                   and e.origin in (None, 'https://' + OTHER) for e in sites.events())


def test_secret_never_filled_off_login_origin(tmp_path):
    """An acting frame navigated off-origin after finding receives no credential bytes."""
    config = fixture_config(tmp_path)
    value = canary()
    with ChromeSites([LOGIN, ACCOUNT, OTHER]) as sites:
        install(sites)
        sites.pages['/frame'] = ('text/html', b'<iframe src="https://login.example/password"></iframe>', {})
        sites.pages['/password'] = form('current-password', '/done')
        sites.pages['/off'] = form('current-password', '/collect')
        # A manual lease permits the synthetic off-origin frame document only when it is a registered origin.
        s = h.site(origins=('https://' + ACCOUNT, 'https://' + OTHER), login=('https://' + LOGIN,))
        sites.install(h.account_policy(sites=(s,)))
        # An assist live view (its manual lease admits the site's other origin).
        sites.call(lambda: sites.harness.addon.begin_manual(h.Lease('m', 'r1', 's1', 1, 'manual'),
                                                            purpose='assist'))
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               instruction(config, 'open_login'), {'goto': 'https://' + ACCOUNT + '/frame'},
                               {'wait': 1}, instruction(config, 'attempt_fill', value=value,
                                                        navigate_frame='https://' + OTHER + '/off'),
                               {'eval': "() => frames.length"}], broker_fixture=config)
        assert result[-2] == {'error': 'refused'}
        assert not any(value.encode() in r[3] for r in sites.receipts.requests)
        assert not [r for r in sites.receipts.requests if r[0] == 'POST']


def _bound_action(tmp_path, invalidation, op):
    config = fixture_config(tmp_path)
    value = canary()
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/password'] = ('text/html', b'''<form onsubmit="event.preventDefault()">
            <input id=field type=password autocomplete=current-password>
            <button onclick="window.clicks++">Continue</button></form><script>window.clicks=0;</script>''', {})
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               instruction(config, 'open_login'), {'goto': 'https://' + LOGIN + '/password'},
                               instruction(config, 'attempt_bound_action', invalidate=invalidation, op=op,
                                           value=value, same_origin_page='https://' + LOGIN + '/password')],
                              broker_fixture=config)
        # A form taken out of the DOM (no navigation) is a page that changed under the step: the
        # sign-in reads the page again (the v0.1.0 re-render residual). Either way nothing acts.
        expected = 'page_changed' if invalidation == 'detached' else 'refused'
        assert result[-1]['reply'] == {'error': expected}
        # Inspect the bound page and surviving target; neither may receive a stale action.
        assert result[-1]['effects']
        assert all(effect == {'value': '', 'clicks': 0} for effect in result[-1]['effects'])
        assert not [r for r in sites.receipts.requests if r[0] == 'POST']
        assert not any(value.encode() in r[3] for r in sites.receipts.requests)


@pytest.mark.parametrize('case', ['closed', 'replaced'])
@pytest.mark.parametrize('op', ['fill', 'submit'])
def test_driver_session_bound_to_page(tmp_path, case, op):
    """Valid credential handles cannot act after their bound Page closes or is replaced."""
    _bound_action(tmp_path, case, op)


@pytest.mark.parametrize('case', ['wrong_session', 'closed_session', 'generation', 'manager_session'])
@pytest.mark.parametrize('op', ['fill', 'submit'])
def test_driver_session_binding(tmp_path, case, op):
    """Valid handles cannot fill or click through stale or unowned driver sessions."""
    _bound_action(tmp_path, case, op)


def test_fill_is_element_bound(tmp_path):
    """A hostile frame stealing focus receives no suffix of the element-bound password."""
    config = fixture_config(tmp_path)
    value = canary()
    with ChromeSites([LOGIN, ACCOUNT, OTHER]) as sites:
        install(sites)
        sites.pages['/password'] = ('text/html', b'''<form><input id=field type=password autocomplete=current-password>
            <button>Go</button></form><iframe src="https://outside.example/steal"></iframe>
            <script>field.addEventListener('focus', () => frames[0].postMessage('focus','*'));</script>''', {})
        sites.pages['/steal'] = ('text/html', b'''<input id=stolen><script>
            addEventListener('message', () => stolen.focus());
            setInterval(() => stolen.focus(), 5);</script>''', {})
        s = h.site(origins=('https://' + ACCOUNT, 'https://' + OTHER), login=('https://' + LOGIN,))
        sites.install(h.account_policy(sites=(s,)))
        # An assist live view (its manual lease admits the site's other origin).
        sites.call(lambda: sites.harness.addon.begin_manual(h.Lease('m', 'r1', 's1', 1, 'manual'),
                                                            purpose='assist'))
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               instruction(config, 'open_login'), {'goto': 'https://' + LOGIN + '/password'},
                               {'wait': 1}, instruction(config, 'attempt_fill', value=value),
                               {'eval': '() => document.querySelector("#field").value'},
                               {'frame_value': {'origin': 'https://' + OTHER, 'selector': '#stolen'}}], broker_fixture=config)
        assert result[-3] == {'ok': True}
        assert result[-2] == value and result[-1] == ''
        assert not [r for r in sites.receipts.requests if r[0] == 'POST']


@pytest.mark.parametrize('shape', ['native', 'fetch', 'generator'])
def test_operator_password_change_offered(tmp_path, shape):
    """All three change shapes hand the capture the actual field value."""
    _change(tmp_path, shape)


def test_capture_survives_field_clear(tmp_path):
    """A fetch change followed by reset retains the value read before the clear."""
    _change(tmp_path, 'fetch_clear')


def _change(tmp_path, shape):
    config = fixture_config(tmp_path)
    value, generated = canary(), canary()
    while generated[-2:] == value[-2:]:
        generated = canary()
    actual = generated if shape == 'generator' else value
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/settings'] = ('text/html', b'''<form id=f method=post action=/done>
            <input id=field name=password type=password autocomplete=new-password><button>Save</button></form>''', {})
        sites.pages['/done'] = ('text/html', b'<p>Changed</p>', {})
        typed = "() => {field.value=" + json.dumps(value) + ";field.dispatchEvent(new Event('input',{bubbles:true}));}"
        if shape == 'native':
            send = '() => f.requestSubmit()'
        elif shape == 'generator':
            send = '() => {field.value=' + json.dumps(generated) + ';f.requestSubmit();}'
        else:
            send = "() => fetch('/done',{method:'POST',body:field.value}).then(() => " + ('f.reset()' if shape == 'fetch_clear' else 'true') + ')'
        steps = [{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                 {'goto': 'https://' + ACCOUNT + '/settings'}, instruction(config, 'begin_capture'),
                 {'eval': typed}, {'eval': send}, {'wait': 1}]
        if shape == 'closed_tab':
            steps += [{'new_page': 'https://' + ACCOUNT + '/done'}, {'close_page': None}]
        result = run_scenario(steps + [instruction(config, 'review')], broker_fixture=config)
        # One candidate per field — its latest value (casa-test F3): a value the page put in
        # the field after the operator typed (the generator) replaces the typed one.
        assert len(result[-1]['candidates']) == 1
        assert all(c['role'] == 'password' for c in result[-1]['candidates'])
        assert sum(c['hint'].endswith(actual[-2:]) for c in result[-1]['candidates']) == 1
        posts = [r for r in sites.receipts.requests if r[0] == 'POST' and r[2] == '/done']
        assert len(posts) == 1 and actual.encode() in posts[0][3]
        assert sites.receipts.connections > 0 and sites.receipts.bytes > 0


def test_capture_context_survives_closed_tab(tmp_path):
    """Closing the tab that began capture leaves the context's captured values reviewable."""
    _change(tmp_path, 'closed_tab')


# 1Password's sign-in page as measured (slice 5v, my.1password.eu 2026-11-01): no form, controls of
# type=button; the email first, Enter; then the Secret Key and password fields appear on the same
# address, Enter. Here the second Enter posts the three values to /guest (the real page signs in
# and the tab lands on the extension's own page, which a fixture site cannot be).
SIGNIN_PAGE = b"""<title>Login</title><div id=main>
<label>Email<input type=email autocomplete=username id=email></label>
<button type=button>Continue</button><button type=button id=region>1Password.eu</button></div>
<script>
const email = document.getElementById('email');
email.addEventListener('keydown', e => {
  if (e.key !== 'Enter' || document.getElementById('account-key')) return;
  if (location.search.includes('elsewhere')) history.pushState(null, '', '/elsewhere');
  document.getElementById('main').insertAdjacentHTML('beforeend',
    '<label>Secret Key<input id=account-key autocomplete=off></label>' +
    '<label>Password<input type=password autocomplete=current-password id=master-password' +
    ' aria-label=Password></label><button type=button>Sign In</button>');
  document.getElementById('master-password').addEventListener('keydown', ev => {
    if (ev.key !== 'Enter') return;
    fetch('/guest', {method: 'POST', body: new URLSearchParams({
      email: email.value, secretKey: document.getElementById('account-key').value,
      password: document.getElementById('master-password').value})});
  });
});
</script>"""


@pytest.mark.parametrize('elsewhere', [False, True])
def test_guest_sign_in_opens_window(tmp_path, elsewhere):
    """The real driver opens its owned credential window before its sign-in tab navigates, then
    signs in in the measured two steps (5v); a second step on another address of the same origin
    gets no value (the page's own origin-and-path check at the moment of setting)."""
    config = fixture_config(tmp_path)
    values = {name: canary() for name in ('email', 'secret_key', 'password')}
    values['email'] += '@example.com'  # the sign-in form's type=email field must validate
    with ChromeSites([LOGIN, ACCOUNT, 'my.1password.eu']) as sites:
        install(sites)
        s = h.site(origins=('https://' + ACCOUNT,), login=('https://' + LOGIN, 'https://my.1password.eu'))
        sites.install(h.account_policy(sites=(s,)))
        page = SIGNIN_PAGE.replace(b"location.search.includes('elsewhere')",
                                   b'true' if elsewhere else b'false')
        sites.pages['/signin'] = ('text/html', page, {})
        region = 'my.1password.eu'
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               instruction(config, 'guest_sign_in', region_host=region, **values)],
                              broker_fixture=config)
        order = result[-1]['order']
        assert order[0] == 'open_window' and 'navigate' in order[1:]
        posts = [r for r in sites.receipts.requests if r[0] == 'POST' and r[2] == '/guest']
        from urllib.parse import quote_plus
        if elsewhere:
            assert result[-1]['reply'] == {'error': 'refused'} and posts == []
        else:
            # No extension here, so the tab never lands on its page: unknown, after the values.
            assert result[-1]['reply'] == {'state': 'unknown'}, result[-1]
            assert len(posts) == 1
            assert all(quote_plus(v).encode() in posts[0][3] for v in values.values())
        from casa_browser.proxy_client import encode_event
        events = sites.events()
        for event in events:
            encoded = json.dumps(encode_event(event)).encode()
            assert all(v.encode() not in encoded and quote_plus(v).encode() not in encoded
                       for v in values.values())
        # The guest tab's own traffic is recorded inside its credential window (vocabulary only).
        guest = [e for e in events if e.origin == 'https://my.1password.eu']
        assert guest and all(e.binding is None for e in guest)


# The extension's measured shapes (1Password 8.12.42.36, 2026-11-01), reduced to what the
# driver's scripts read: the popup's lock screen and vault list, its signed-out skeleton, and the
# in-page notification frame's "Sign in with a passkey" prompt.
POPUP_LOCKED = b"""<div id=root><div>Locked account: Example</div><button class=vn></button>
<input type=password autocomplete=off placeholder="Enter your password">
<button type=button role=switch></button>
<button type=button data-testid=lock-screen-submit-button disabled></button></div>"""
POPUP_VAULT = b"""<div id=root><button>All Accounts</button>
<input type=search name=q autocomplete=off placeholder="Search 1Password">
<button type=button data-testid=password-generator-button></button>
<button type=button aria-label="New Item"></button><button data-testid=category>All Items</button>
<div data-testid=item-text-label>Example</div></div>"""
POPUP_SIGNED_OUT = b"""<div id=root><div><div></div><div><div></div></div>
<div aria-live=polite><span aria-live=off></span></div></div></div>"""
PROMPT = """<div role=dialog><div data-testid=notification-scroller>{notes}</div></div>"""
NOTE = """<div data-testid=stacked-notification role=alert><div id=notification-title-bar>
<div><h1>Sign in with a passkey</h1></div><div>{bar}</div></div>
<div><div><div><img alt=""></div><div><div data-testid=item-text-label>{title}</div>
<div>{username}</div></div></div><div><button type=button><span>Sign in</span></button>
</div></div></div>"""
BAR = ('<button type=button aria-label="Use a security key or another passkey"></button>'
       '<button type=button aria-label=Close></button>')
FIELDS = (b'<form><input id=u autocomplete="username webauthn">'
          b'<input id=p type=password autocomplete="current-password webauthn">'
          b'<input id=w autocomplete=webauthn><input id=e type=email></form>')


def prompt_page(notes=1, bar=BAR, title='webauthn.io', username='casa-1p-1'):
    return ('text/html', PROMPT.format(notes=''.join(
        NOTE.format(bar=bar, title=title, username=username) for _ in range(notes))).encode(), {})


def test_driver_scripts_read_the_measured_extension_pages(tmp_path):
    """Slice 5v: the driver's fixed scripts against the measured DOM shapes — the popup's three
    states, the passkey prompt offering exactly the expected item and nothing else, the role of
    a ``username webauthn`` field, and the guest setter's origin-and-path check."""
    config = fixture_config(tmp_path)
    origin = 'https://' + LOGIN
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/locked'] = ('text/html', POPUP_LOCKED, {})
        sites.pages['/vault'] = ('text/html', POPUP_VAULT, {})
        sites.pages['/signed-out'] = ('text/html', POPUP_SIGNED_OUT, {})
        sites.pages['/prompt'] = prompt_page()
        sites.pages['/two'] = prompt_page(notes=2)
        sites.pages['/save'] = prompt_page(bar='<button type=button aria-label=Close></button>')
        sites.pages['/empty'] = ('text/html', PROMPT.format(notes='').encode(), {})
        sites.pages['/fields'] = ('text/html', FIELDS, {})

        def js(name, **kw):
            return {'login_js': {'name': name, **kw}}
        steps = [{'open': {'site_id': 's1'}}]
        expected = []
        for path, state in [('/locked', 'locked'), ('/vault', 'unlocked'), ('/signed-out', None)]:
            steps += [{'goto': origin + path}, js('_POPUP_STATE', arg=origin)]
            expected += [200, state]
        steps += [js('_POPUP_STATE', arg='chrome-extension://aeblfdkhhhdcdjpifhhbdiojplfjncoa')]
        expected += ['foreign']
        who = {'title': 'webauthn.io', 'username': 'casa-1p-1'}
        steps += [{'goto': origin + '/prompt'}, js('_PROMPT_SHOWN'),
                  js('_PASSKEY_BUTTON', handle=True, arg=who),
                  js('_PASSKEY_BUTTON', handle=True, arg={**who, 'username': 'casa-1p-2'}),
                  js('_PASSKEY_BUTTON', handle=True, arg={**who, 'title': 'Other'}),
                  {'goto': origin + '/two'}, js('_PASSKEY_BUTTON', handle=True, arg=who),
                  {'goto': origin + '/save'}, js('_PASSKEY_BUTTON', handle=True, arg=who),
                  {'goto': origin + '/empty'}, js('_PROMPT_SHOWN')]
        expected += [200, True, 'Sign in', None, None, 200, None, 200, None, 200, False]
        steps += [{'goto': origin + '/fields'}] + [
            js('_ROLE', selector=sel) for sel in ('#u', '#p', '#w', '#e')]
        expected += [200, 'username', 'password', None, 'username']
        for arg, ok in [({'origin': origin, 'path': '/fields'}, True),
                        ({'origin': origin, 'path': '/signin'}, False),
                        ({'origin': 'https://my.1password.eu', 'path': '/fields'}, False)]:
            steps += [js('_GUEST_SET', selector='#e', arg={**arg, 'type': 'email',
                                                           'value': 'x@example.com'})]
            expected += [ok]
        result = run_scenario(steps, broker_fixture=config)
        assert result[1:] == expected, result


def test_a_passkey_hint_is_a_shown_webauthn_field_on_a_login_origin(tmp_path):
    """The lazy guest unlock's T1, in Chrome's own DOM: field discovery reports
    ``passkey_hint`` for a visible, enabled input on a login origin whose autocomplete ends with
    the token ``webauthn`` (webauthn.io's ``username webauthn``), and for nothing else."""
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        pages = {
            '/fields': FIELDS,                                                 # T1
            '/only': b'<input autocomplete=webauthn>',                          # T1, no role
            '/hidden': b'<input autocomplete="username webauthn" hidden>',
            '/disabled': b'<input autocomplete="username webauthn" disabled>',
            '/order': b'<input autocomplete="webauthn username">',              # not the last token
            '/plain': (b'<form><input autocomplete=username>'
                       b'<input type=password autocomplete=current-password>'
                       b'<button>Go</button></form>'),
        }
        for path, body in pages.items():
            sites.pages[path] = ('text/html', body, {})
        sites.pages['/off'] = ('text/html', b'<input autocomplete="username webauthn">', {})
        steps = [{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                 instruction(config, 'open_login'),
                 {'goto': 'https://' + ACCOUNT + '/off'}, instruction(config, 'find_login_fields')]
        for path in ('/hidden', '/disabled', '/order', '/plain', '/only', '/fields'):
            steps += [{'goto': 'https://' + LOGIN + path}, instruction(config, 'find_login_fields')]
        result = run_scenario(steps, broker_fixture=config)
        hints = [r.get('passkey_hint') for r in result[4::2]]
        assert hints == [False, False, False, False, False, True, True], result


def test_main_world_cannot_forge_capture(tmp_path):
    """A page can enumerate the binding, but only isolated-world values reach review."""
    config = fixture_config(tmp_path)
    actual, forged = canary(), canary()
    while actual[-2:] == forged[-2:]:
        forged = canary()
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/settings'] = ('text/html', b'<input id=field type=password>', {})
        attack = """async () => {
            const key=Object.getOwnPropertyNames(window).find(k=>k.startsWith('capture_'));
            if(!key) return false;
            const b=window[key];
            if(b.toString().startsWith('(...args) => {')) await b('wrong','password',VALUE);
            else b(JSON.stringify({name:key,seq:9000,serializedArgs:['wrong','password',VALUE]}));
            return true;
        }""".replace('VALUE', json.dumps(forged))
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               {'goto': 'https://' + ACCOUNT + '/settings'},
                               instruction(config, 'begin_capture'), {'eval': attack},
                               {'eval': '() => {field.value=' + json.dumps(actual) +
                                ";field.dispatchEvent(new Event('input',{bubbles:true}));}"},
                               instruction(config, 'review')], broker_fixture=config)
        assert result[-3] is True
        candidates = result[-1]['candidates']
        assert len(candidates) == 1
        assert candidates[0]['role'] == 'password'
        assert candidates[0]['hint'].endswith(actual[-2:])


def test_guest_sign_in_with_valid_email(tmp_path):
    """A valid email in the measured two-step page (5v): all guest fields are posted through
    the owned tab's proxy rule."""
    from urllib.parse import parse_qs
    from casa_browser.proxy_client import encode_event
    config = fixture_config(tmp_path)
    values = {'email': canary() + '@example.test', 'secret_key': canary(), 'password': canary()}
    with ChromeSites([LOGIN, ACCOUNT, 'my.1password.eu']) as sites:
        install(sites)
        sites.pages['/signin'] = ('text/html', SIGNIN_PAGE.replace(
            b"location.search.includes('elsewhere')", b'false'), {})
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               instruction(config, 'guest_sign_in', region_host='my.1password.eu', **values)],
                              broker_fixture=config)
        assert result[-1]['order'][0] == 'open_window'
        posts = [r for r in sites.receipts.requests if r[0] == 'POST' and r[2] == '/guest']
        assert len(posts) == 1
        assert parse_qs(posts[0][3].decode()) == {
            'email': [values['email']], 'secretKey': [values['secret_key']], 'password': [values['password']]}
        events = sites.events()
        assert any(e.method == 'POST' and e.allow for e in events)
        for event in events:
            encoded = json.dumps(encode_event(event))
            assert all(value not in encoded for value in values.values())
        guest = [e for e in events if e.origin == 'https://my.1password.eu']
        assert guest and all(e.binding is None for e in guest)


@pytest.mark.parametrize('case', ['adopted', 'detached'])
@pytest.mark.parametrize('op', ['fill', 'submit'])
def test_login_action_requires_bound_document(tmp_path, case, op):
    """Moving a discovered form into a popup or out of the DOM refuses every action."""
    _bound_action(tmp_path, case, op)


@pytest.mark.parametrize('case', ['type_button', 'other_form'])
def test_submit_revalidates_control(tmp_path, case):
    """A discovered control cannot change its submit type or form owner before clicking."""
    _bound_action(tmp_path, case, 'submit')


def test_discovery_rejects_foreign_form_submit(tmp_path):
    """A descendant button assigned to another form is not the login form's submit."""
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/password'] = ('text/html', b'''<form id=login>
            <input id=field type=password autocomplete=current-password>
            <button form=other>Continue</button></form><form id=other></form>''', {})
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               instruction(config, 'open_login'), {'goto': 'https://' + LOGIN + '/password'},
                               instruction(config, 'find_login_fields')], broker_fixture=config)
        assert result[-1]['roles']['password']
        assert result[-1]['submit'] is None
        assert not [r for r in sites.receipts.requests if r[0] == 'POST']


@pytest.mark.parametrize('op', ['fill', 'submit'])
@pytest.mark.parametrize('stall', [True, False], ids=['forced', 'durable-control'])
def test_forced_close_kills_queued_action(tmp_path, op, stall):
    """A stalled renderer cannot fill or click after forced close; markers survive reopen."""
    from casa_browser.proxy_client import encode_policy
    config = fixture_config(tmp_path)
    value = canary()
    with ChromeSites([ACCOUNT]) as sites:
        install(sites, ACCOUNT)
        sites.pages['/settings'] = ('text/html', b'''<form onsubmit="event.preventDefault()">
            <input id=field type=password autocomplete=current-password value=held><button>Continue</button>
            </form><script>
            field.addEventListener('input', () => {
                localStorage.setItem('late-effect','fill');
                window.effectDone=fetch('/effect',{method:'POST',body:'effect'}).catch(()=>{});
            });
            document.querySelector('button').addEventListener('click', () => {
                localStorage.setItem('late-effect','submit');
                window.effectDone=fetch('/effect',{method:'POST',body:'effect'}).catch(()=>{});
            });</script>''', {})
        url = 'https://' + ACCOUNT + '/settings'
        s = h.site(origins=('https://' + ACCOUNT,), login=('https://' + ACCOUNT,))
        policy = h.account_policy(sites=(s,))
        result = run_scenario([
            {'open': {'site_id': 's1'}}, instruction(config, 'setup', ACCOUNT),
            instruction(config, 'open_login', ACCOUNT), {'goto': url},
            instruction(config, 'forced_close_action', ACCOUNT, op=op, value=value,
                        url=url, policy=encode_policy(policy), stall=stall)], broker_fixture=config)
        if stall:
            assert result[-1]['marker'] is None
            assert not [r for r in sites.receipts.requests if r[0] == 'POST']
            assert result[-1]['order'] == ['browser_exited', 'revoke']
        else:
            assert result[-1]['marker'] == op
            # The same listeners provide a positive control for durable effects and receipts.
            assert any(r[0] == 'POST' and r[2] == '/effect' for r in sites.receipts.requests)


def test_capture_of_a_signin_page_loaded_after_the_view_opened(tmp_path):
    """casa-test F2/F3: the sign-in page loads only after the capture began (the operator
    navigates there in the live view). Typing as xdotool does — one input event per character —
    into a plain text username field (no autocomplete) and a password field leaves exactly one
    candidate per field, its whole value: the username as a username, the password as a password."""
    config = fixture_config(tmp_path)
    user, password = 'it-user-' + canary()[8:20], canary()
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/start'] = ('text/html', b'<p>Welcome</p><a href="/signin">Sign in</a>', {})
        sites.pages['/signin'] = ('text/html', b'<form id=f method=post action=/done>'
                                  b'<input id=u name=user type=text><input id=p name=pass type=password>'
                                  b'<button>Sign in</button></form>', {})
        sites.pages['/done'] = ('text/html', b'<p>Signed in</p>', {})
        typed = ("() => { const type = (el, text) => { for (const ch of text) { el.value += ch; "
                 "el.dispatchEvent(new Event('input', {bubbles: true})); } }; "
                 f"type(u, {json.dumps(user)}); type(p, {json.dumps(password)}); }}")
        steps = [{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                 {'goto': 'https://' + ACCOUNT + '/start'}, instruction(config, 'begin_capture'),
                 {'goto': 'https://' + ACCOUNT + '/signin'}, {'wait': 1},
                 {'eval': typed}, {'eval': '() => f.requestSubmit()'}, {'wait': 1},
                 instruction(config, 'review')]
        result = run_scenario(steps, broker_fixture=config)
        review = result[-1]
        cands = sorted((c['role'], c['hint']) for c in review['candidates'])
        assert [r for r, _ in cands] == ['password', 'username'], review
        hint = dict(cands)
        assert hint['username'] == user and hint['password'].endswith(password[-2:]), cands
        assert str(len(password)) in hint['password'], cands
        assert review.get('capture_incomplete') is False, review
        posts = [r for r in sites.receipts.requests if r[0] == 'POST' and r[2] == '/done']
        assert len(posts) == 1 and password.encode() in posts[0][3]   # the login really went out


@pytest.mark.parametrize('when', ['after_typing', 'before_typing', 'appended_same_task', 'clone',
                                  'clone_then_changed'])
def test_a_shown_password_is_never_a_username(tmp_path, when):
    """Operator ruling E-T14-3 = Q1: a value seen as a password anywhere in the capture is never
    a username candidate (whose hint would show it in full) — after a "show password" toggle
    (after or before typing), a field added, shown and removed in one task, or a text clone that
    replaced the password field. Positive control: the password is captured."""
    config = fixture_config(tmp_path)
    password = canary()
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/start'] = ('text/html', b'<p>Welcome</p>', {})
        sites.pages['/signin'] = ('text/html', b'<form id=f method=post action=/done>'
                                  b'<input id=n name=new type=password><input id=c name=confirm type=password>'
                                  b'<button>Change</button></form>', {})
        sites.pages['/done'] = ('text/html', b'<p>Changed</p>', {})
        toggle = "() => { n.type = 'text'; }"
        typed = ("() => { for (const el of [n, c]) for (const ch of " + json.dumps(password) + ") "
                 "{ el.value += ch; el.dispatchEvent(new Event('input', {bubbles: true})); } }")
        steps = [{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                 {'goto': 'https://' + ACCOUNT + '/start'}, instruction(config, 'begin_capture'),
                 {'goto': 'https://' + ACCOUNT + '/signin'}, {'wait': 1}]
        # r6 S1: a password field added, filled, shown, read and removed in one task — only
        # the read itself can learn it was a password (it takes the observer's records)
        appended = ("() => { const e = document.createElement('input'); e.type = 'password'; "
                    "e.id = 'x'; f.insertBefore(e, c); e.value = " + json.dumps(password) + "; "
                    "e.type = 'text'; e.dispatchEvent(new Event('input', {bubbles: true})); "
                    "for (const ch of " + json.dumps(password) + ") { c.value += ch; "
                    "c.dispatchEvent(new Event('input', {bubbles: true})); } n.remove(); e.remove(); }")
        if when == 'after_typing':
            steps += [{'eval': typed}, {'eval': toggle}]
        elif when == 'before_typing':
            steps += [{'eval': toggle}, {'wait': 0.2}, {'eval': typed}]
        elif when == 'appended_same_task':
            steps += [{'eval': appended}]
        elif when == 'clone_then_changed':   # r8 S1: a text clone keeps P, the field moves on to Q
            other = canary()
            steps += [{'eval': typed}, {'wait': 0.7},
                      {'eval': "() => { const k = n.cloneNode(); k.type = 'text'; k.value = n.value; "
                               "k.id = 'k'; c.after(k); k.dispatchEvent(new Event('input', {bubbles: true})); "
                               "n.value = " + json.dumps(other) + "; c.value = n.value; "
                               "n.dispatchEvent(new Event('input', {bubbles: true})); }"}]
        else:   # r7 S1: the password input replaced by a text clone (a new field)
            steps += [{'eval': typed}, {'wait': 0.7},
                      {'eval': "() => { const k = n.cloneNode(); k.type = 'text'; k.value = n.value; "
                               "k.id = 'k'; n.replaceWith(k); k.dispatchEvent(new Event('input', {bubbles: true})); }"}]
        steps += [{'wait': 1}, {'eval': '() => f.requestSubmit()'}, {'wait': 1},
                  instruction(config, 'review')]
        review = run_scenario(steps, broker_fixture=config)[-1]
        roles = [c['role'] for c in review['candidates']]
        assert 'username' not in roles, review
        assert set(roles) == {'password'}, review
        assert all(password not in json.dumps(c) for c in review['candidates']), review


def test_password_history_overflow_offers_no_username(tmp_path):
    """r9 S1, browser to broker: past the capture's bound of remembered password values, a
    password copied into a field the page marks `autocomplete=username` is not offered as a
    username (no username is, whatever the field); the capture says it is incomplete. The
    password field's current value is still offered."""
    from casa_browser.login import MAX_SEEN_PASSWORDS
    config = fixture_config(tmp_path)
    secret, final = canary(), canary()
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/start'] = ('text/html', b'<p>Welcome</p>', {})
        sites.pages['/signin'] = ('text/html', b'<form id=f method=post action=/done>'
                                  b'<input id=u name=user autocomplete=username>'
                                  b'<input id=n name=pass type=password><button>Go</button></form>', {})
        sites.pages['/done'] = ('text/html', b'<p>Done</p>', {})
        flood = ("() => { for (let i = 0; i < " + str(MAX_SEEN_PASSWORDS + 5) + "; i++) { "
                 "n.value = 'flood-' + i + '-' + Math.random().toString(36).slice(2); "
                 "n.dispatchEvent(new Event('input', {bubbles: true})); } "
                 "n.value = " + json.dumps(secret) + "; n.dispatchEvent(new Event('input', {bubbles: true})); }")
        swap = ("() => { u.value = n.value; u.dispatchEvent(new Event('input', {bubbles: true})); "
                "n.value = " + json.dumps(final) + "; n.dispatchEvent(new Event('input', {bubbles: true})); }")
        steps = [{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                 {'goto': 'https://' + ACCOUNT + '/start'}, instruction(config, 'begin_capture'),
                 {'goto': 'https://' + ACCOUNT + '/signin'}, {'wait': 1},
                 {'eval': flood}, {'wait': 0.7}, {'eval': swap}, {'wait': 1},
                 {'eval': '() => f.requestSubmit()'}, {'wait': 1}, instruction(config, 'review')]
        review = run_scenario(steps, broker_fixture=config)[-1]
        assert [c['role'] for c in review['candidates']] == ['password'], review
        assert review['candidates'][0]['hint'].endswith(final[-2:])
        assert review['capture_incomplete'] is True
        assert secret not in json.dumps(review)


@pytest.mark.parametrize('shape', ['shown_before_typing', 'added_filled_shown_in_one_step'])
def test_a_password_field_counts_from_first_sight(tmp_path, shape):
    """Operator 2026-10-23 (Astra r10's limit): a field that was a password field when the
    reader first saw it stays one, whatever type the page gives it later — a "show password"
    toggle before typing, or a field added as a password, filled and shown in one script step.
    Its value (here different from the other field's) is a password candidate, never a username;
    the other password field's value is the positive control."""
    config = fixture_config(tmp_path)
    shown, other = canary(), canary()
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/start'] = ('text/html', b'<p>Welcome</p>', {})
        sites.pages['/signin'] = ('text/html', b'<form id=f method=post action=/done>'
                                  b'<input id=n name=new type=password><input id=c name=confirm type=password>'
                                  b'<button>Change</button></form>', {})
        sites.pages['/done'] = ('text/html', b'<p>Changed</p>', {})
        type_into = ("(el, text) => { for (const ch of text) { el.value += ch; "
                     "el.dispatchEvent(new Event('input', {bubbles: true})); } }")
        if shape == 'shown_before_typing':
            act = ("() => { const t = " + type_into + "; n.type = 'text'; t(n, " + json.dumps(shown) + "); "
                   "t(c, " + json.dumps(other) + "); }")
            steps_mid = [{'eval': "() => { n.type = 'text'; }"}, {'wait': 0.2}, {'eval': act}]
        else:
            act = ("() => { const t = " + type_into + "; const e = document.createElement('input'); "
                   "e.type = 'password'; e.id = 'x'; f.insertBefore(e, c); "
                   "e.value = " + json.dumps(shown) + "; e.type = 'text'; "
                   "e.dispatchEvent(new Event('input', {bubbles: true})); t(c, " + json.dumps(other) + "); }")
            steps_mid = [{'eval': act}]
        steps = [{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                 {'goto': 'https://' + ACCOUNT + '/start'}, instruction(config, 'begin_capture'),
                 {'goto': 'https://' + ACCOUNT + '/signin'}, {'wait': 1}, *steps_mid,
                 {'wait': 1}, {'eval': '() => f.requestSubmit()'}, {'wait': 1},
                 instruction(config, 'review')]
        review = run_scenario(steps, broker_fixture=config)[-1]
        roles = sorted(c['role'] for c in review['candidates'])
        hints = [c['hint'] for c in review['candidates']]
        assert roles == ['password', 'password'], review
        assert any(h.endswith(shown[-2:]) for h in hints) and any(h.endswith(other[-2:]) for h in hints)
        assert shown not in json.dumps(review) and other not in json.dumps(review)


# -- slice 5 (S5-4, S5-5, design §7 auto-save checks): the live-view sign-in, in Chrome -------

SIGNIN_FORM = (b'<form id=f method=post action="{action}"><input id=u name=user type=text '
               b'autocomplete=username><input id=p name=pass type=password>'
               b'<button id=b>Sign in</button></form>')
ACCOUNT_PAGE = ('text/html', b'<h1>Your account</h1><p>Invoices</p><a href=/logout>Log out</a>', {})


def _form(action, *, user=True):
    body = SIGNIN_FORM.replace(b'{action}', action.encode())
    if not user:
        body = body.replace(b'<input id=u name=user type=text autocomplete=username>', b'')
    return ('text/html', body, {})


def _signin_steps(config, body_steps):
    """A capture on a site with no linked item, the operator's own typing and clicks in real
    Chrome, then Done (``take_attempt``) — the pages read after the attempt come back too."""
    from casa_browser.proxy_client import encode_policy
    s = h.site(origins=('https://' + ACCOUNT,), login=('https://' + LOGIN,))
    policy = encode_policy(h.account_policy(sites=(s,)))
    return ([{'open': {'site_id': 's1'}}, instruction(config, 'setup', linked=False),
             {'goto': 'https://' + ACCOUNT + '/start'},
             instruction(config, 'begin_capture', policy=policy)] + body_steps
            + [{'wait': 2.5}, instruction(config, 'take_attempt')])


def _created(config):
    return [i for i in vault(config) if i['id'] != 'item']


def _value(item, role):
    return next(f['value'] for f in item['fields'] if f['id'] == role)


def _run(config, sites, body_steps, verdict):
    install(sites)
    steps = _signin_steps(config, body_steps)
    steps.append(instruction(config, 'save_attempt' if verdict == 'signed_in' else 'drop_attempt'))
    result = run_scenario(steps, broker_fixture=config)
    return result[-2], result[-1]


def test_s5_a_typo_then_correction_saves_the_corrected_password(tmp_path):
    config = fixture_config(tmp_path)
    user, password = 'it-user-' + canary()[8:16], canary()
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<a href="https://login.example/signin">Sign in</a>', {})
        sites.pages['/signin'] = _form('https://' + ACCOUNT + '/account')
        sites.pages['/account'] = ACCOUNT_PAGE
        done, saved = _run(config, sites, [
            {'goto': 'https://' + LOGIN + '/signin'}, {'wait': 1},
            {'type': {'selector': '#u', 'text': user}},
            {'type': {'selector': '#p', 'text': password + 'X'}},
            {'press': {'selector': '#p', 'key': 'Backspace'}},
            {'click': '#b'}], 'signed_in')
        assert done['taken'] == {'attempt': {'failed': False, 'typed_after': False},
                                 'incomplete': False}, done
        assert saved['saved'] is True, saved
        (item,) = _created(config)
        assert (item['title'], _value(item, 'username'), _value(item, 'password')) == \
            ('Site', user, password)
        after = done['readings']['after']
        assert after and after[-1]['host'] == ACCOUNT and 'Log out' in after[-1]['blocks'][-1]
        at_done = done['readings']['at_done']
        assert at_done['_origin'] == 'https://' + ACCOUNT and 'Log out' in at_done['blocks'][-1]
        assert password not in json.dumps(done) and user not in json.dumps(done['readings'])


def test_s5_b_two_page_sign_in_joins_the_username(tmp_path):
    config = fixture_config(tmp_path)
    user, password = 'it-user-' + canary()[8:16] + '@example.com', canary()
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<p>Start</p>', {})
        sites.pages['/identify'] = ('text/html', b'<form id=f method=post action=/password>'
                                    b'<input id=u name=user type=email><button id=b>Next</button>'
                                    b'</form>', {})
        sites.pages['/password'] = _form('https://' + ACCOUNT + '/account', user=False)
        sites.pages['/account'] = ACCOUNT_PAGE
        done, saved = _run(config, sites, [
            {'goto': 'https://' + LOGIN + '/identify'}, {'wait': 1},
            {'type': {'selector': '#u', 'text': user}}, {'click': '#b'}, {'wait': 1.5},
            {'type': {'selector': '#p', 'text': password}},
            {'press': {'selector': '#p', 'key': 'Enter'}}], 'signed_in')
        assert done['taken']['attempt'] == {'failed': False, 'typed_after': False}, done
        (item,) = _created(config)
        assert (_value(item, 'username'), _value(item, 'password')) == (user, password)


def test_s5_c_a_failed_sign_in_then_a_successful_one_saves_the_second(tmp_path):
    config = fixture_config(tmp_path)
    user, wrong, right = 'it-user-' + canary()[8:16], canary(), canary()
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<p>Start</p>', {})
        sites.pages['/signin'] = _form('/again')
        again = _form('https://' + ACCOUNT + '/account')
        sites.pages['/again'] = ('text/html', b'<p>Wrong password.</p>' + again[1], {})
        sites.pages['/account'] = ACCOUNT_PAGE
        done, saved = _run(config, sites, [
            {'goto': 'https://' + LOGIN + '/signin'}, {'wait': 1},
            {'type': {'selector': '#u', 'text': user}},
            {'type': {'selector': '#p', 'text': wrong}}, {'click': '#b'}, {'wait': 2},
            {'type': {'selector': '#u', 'text': user}},
            {'type': {'selector': '#p', 'text': right}}, {'click': '#b'}], 'signed_in')
        assert done['taken']['attempt'] == {'failed': False, 'typed_after': False}, done
        (item,) = _created(config)
        assert _value(item, 'password') == right
        assert wrong not in json.dumps(vault(config)) and wrong not in json.dumps(done)


def test_s5_c2_a_form_shown_again_fails_the_attempt(tmp_path):
    """The other half of the rule, in Chrome: the attempt answered by the form again on the
    login origin is failed (never judged, never saved)."""
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<p>Start</p>', {})
        sites.pages['/signin'] = _form('/again')
        sites.pages['/again'] = ('text/html', b'<p>Wrong password.</p>' + _form('/again')[1], {})
        install(sites)
        steps = _signin_steps(config, [
            {'goto': 'https://' + LOGIN + '/signin'}, {'wait': 1},
            {'type': {'selector': '#u', 'text': 'it-user'}},
            {'type': {'selector': '#p', 'text': canary()}}, {'click': '#b'}])
        steps.append(instruction(config, 'save_attempt'))
        result = run_scenario(steps, broker_fixture=config)
        assert result[-2]['taken']['attempt']['failed'] is True, result[-2]
        assert result[-1] == {'error': 'refused'} and _created(config) == []


def test_s5_d_a_password_change_page_saves_the_new_password(tmp_path):
    config = fixture_config(tmp_path)
    user, old, new = 'it-user-' + canary()[8:16], canary(), canary()
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<p>Start</p>', {})
        sites.pages['/signin'] = _form('https://' + LOGIN + '/change')
        sites.pages['/change'] = ('text/html', b'<p>Please choose a new password.</p>'
                                  b'<form id=c method=post action="https://' + ACCOUNT.encode()
                                  + b'/account"><input id=o type=password autocomplete=current-password>'
                                  b'<input id=n type=password autocomplete=new-password>'
                                  b'<input id=n2 type=password autocomplete=new-password>'
                                  b'<button id=cb>Change</button></form>', {})
        sites.pages['/account'] = ACCOUNT_PAGE
        done, saved = _run(config, sites, [
            {'goto': 'https://' + LOGIN + '/signin'}, {'wait': 1},
            {'type': {'selector': '#u', 'text': user}},
            {'type': {'selector': '#p', 'text': old}}, {'click': '#b'}, {'wait': 2},
            {'type': {'selector': '#o', 'text': old}}, {'type': {'selector': '#n', 'text': new}},
            {'type': {'selector': '#n2', 'text': new}}, {'click': '#cb'}], 'signed_in')
        assert done['taken']['attempt'] == {'failed': False, 'typed_after': False}, done
        (item,) = _created(config)
        assert (_value(item, 'username'), _value(item, 'password')) == (user, new)


@pytest.mark.parametrize('shape', ['signed_out_homepage', 'email_link'])
def test_s5_e_a_sign_in_that_did_not_work_saves_nothing(tmp_path, shape):
    """A wrong password answered by a plain signed-out homepage (no form again), or a rejected
    password followed by an email-link sign-in: the attempt is not failed by the mechanical
    rule, so the check's verdict decides — scripted ``not_signed_in`` — and nothing is saved."""
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<p>Start</p>', {})
        sites.pages['/signin'] = _form('https://' + ACCOUNT + '/home')
        sites.pages['/home'] = ('text/html', b'<h1>Welcome</h1><a href=/login>Sign in</a>'
                                if shape == 'signed_out_homepage' else
                                b'<p>Check your email: we sent you a sign-in link.</p>', {})
        sites.pages['/magic'] = ACCOUNT_PAGE
        body = [{'goto': 'https://' + LOGIN + '/signin'}, {'wait': 1},
                {'type': {'selector': '#u', 'text': 'it-user'}},
                {'type': {'selector': '#p', 'text': canary()}}, {'click': '#b'}]
        if shape == 'email_link':
            body += [{'wait': 1.5}, {'goto': 'https://' + ACCOUNT + '/magic'}]
        done, dropped = _run(config, sites, body, 'not_signed_in')
        assert done['taken']['attempt'] == {'failed': False, 'typed_after': False}, done
        assert dropped == {} and _created(config) == []
        hosts = [r.get('host') for r in done['readings']['after']]
        assert ACCOUNT in hosts


# -- slice 6 (S6-2, S6-3): a sign-in code the site sent ---------------------------------------

def test_s6_emailed_code_fills_the_code_page_and_signs_in(tmp_path):
    """No authenticator in the item: the sign-in stops on the code page (``needs_code``), the
    page stays and can be observed, and the code given later is filled through a fresh driver
    session into exactly that page's code field, which signs in; no driver session is left."""
    from casa_browser.proxy_client import encode_policy
    config = fixture_config(tmp_path, totp=False)
    code = '7Q-' + canary()[8:13]
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/username'] = form('username', '/password')
        sites.pages['/password'] = form('current-password', '/code')
        sites.pages['/code'] = ('text/html', b'<p>We emailed you a code.</p>' + form(
            'one-time-code', '/finish')[1], {})
        sites.pages['/finish'] = ('text/html', b'', {'Location': 'https://' + ACCOUNT + '/done'})
        sites.status['/finish'] = 303
        sites.pages['/done'] = ('text/html', b'<p>Signed in</p>', {})
        s = h.site(origins=('https://' + ACCOUNT,), login=('https://' + LOGIN,))
        policy = encode_policy(h.account_policy(sites=(s,)))
        steps = [{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                 instruction(config, 'open_login'), {'goto': 'https://' + LOGIN + '/username'},
                 instruction(config, 'login'), {'observe': policy},
                 instruction(config, 'fill_code', code=code), {'wait': 1}, {'observe': policy}]
        result = run_scenario(steps, broker_fixture=config)
        asked = result[4]
        assert (asked['status'], asked['code_field'], asked['totp_held']) == \
            ('needs_code', True, False), asked
        assert type(asked['page_generation']) is int
        assert 'We emailed you a code.' in result[5]['text']           # the page stayed
        filled, after = result[6], result[7]
        assert filled == {'status': 'logged_in', 'reason': 'completed'}, filled
        assert after == {'open_driver_sessions': 0}
        assert 'Signed in' in result[-1]['text']
        posts = [r for r in sites.receipts.requests if r[0] == 'POST']
        assert [p[2] for p in posts] == ['/password', '/code', '/finish']
        assert code.encode() in posts[-1][3] and all(code.encode() not in p[3] for p in posts[:-1])
        assert code not in json.dumps(result)
        for path in Path(config['root']).rglob('*'):
            if path.is_file():
                assert code.encode() not in path.read_bytes(), path


def test_s6_a_code_for_a_reloaded_page_is_never_filled(tmp_path):
    """Terra d2: the code goes only into the page that asked; after a reload of the same
    address (another document) nothing is filled or submitted."""
    config = fixture_config(tmp_path, totp=False)
    code = '5K-' + canary()[8:13]
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/code'] = form('one-time-code', '/finish')
        sites.pages['/finish'] = ('text/html', b'<p>Signed in</p>', {})
        steps = [{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                 instruction(config, 'open_login'), {'goto': 'https://' + LOGIN + '/code'},
                 instruction(config, 'login'), {'goto': 'https://' + LOGIN + '/code'},
                 instruction(config, 'fill_code', code=code)]
        result = run_scenario(steps, broker_fixture=config)
        assert result[4]['status'] == 'needs_code', result[4]
        assert result[6] == {'status': 'needs_live_view', 'reason': 'code_page_gone'}, result[6]
        assert not [r for r in sites.receipts.requests if r[0] == 'POST']


@pytest.mark.parametrize('how', ['tab_closed', 'surviving_tab_reloaded'])
def test_s6_a_code_for_another_tab_is_never_filled(tmp_path, how):
    """Astra r1/r2: the code page's tab closes and another tab of the session, on the same code
    address, becomes the page (its navigations never move the generation); the code reaches no
    document but the one that reported — the mark lives only in that document."""
    config = fixture_config(tmp_path, totp=False)
    code = '3M-' + canary()[8:13]
    url = 'https://' + LOGIN + '/code'
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/code'] = form('one-time-code', '/finish')
        sites.pages['/finish'] = ('text/html', b'<p>Signed in</p>', {})
        steps = [{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                 instruction(config, 'open_login'), {'goto': url}, instruction(config, 'login'),
                 {'new_page': url}, {'close_page': True}]
        if how == 'surviving_tab_reloaded':
            steps += [{'goto': url}]
        steps += [instruction(config, 'fill_code', code=code)]
        result = run_scenario(steps, broker_fixture=config)
        assert result[4].get('status') == 'needs_code', result
        filled = result[-2]
        assert filled == {'status': 'needs_live_view', 'reason': 'code_page_gone'}, filled
        assert result[-1] == {'open_driver_sessions': 0}
        assert not [r for r in sites.receipts.requests if r[0] == 'POST']


# -- slice 5n (O2): an email-only sign-in, saved and then used ------------------------------

def test_s5n_an_email_only_sign_in_saves_just_the_email_never_the_code(tmp_path):
    """In real Chrome: the operator types his email (a field the page marks as an email, no
    password field in its form) and Next; the site emails a code, which he types on the next
    page; signed in. The attempt is the email's; the saved item holds that email and no
    password; the code is nowhere in the vault, the replies or the broker's files."""
    config = fixture_config(tmp_path)
    email = 'it-' + canary()[8:16] + '@example.com'
    code = '7Q-' + canary()[8:13]
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<p>Start</p>', {})
        sites.pages['/identify'] = ('text/html', b'<form id=f method=post action=/code>'
                                    b'<input id=u name=email type=email autocomplete=email>'
                                    b'<button id=b>Next</button></form>', {})
        sites.pages['/code'] = ('text/html', b'<p>We emailed you a code.</p><form id=c method=post '
                                b'action="https://' + ACCOUNT.encode() + b'/account"><input id=k '
                                b'name=code type=text autocomplete=one-time-code>'
                                b'<button id=n>Sign in</button></form>', {})
        sites.pages['/account'] = ACCOUNT_PAGE
        done, saved = _run(config, sites, [
            {'goto': 'https://' + LOGIN + '/identify'}, {'wait': 1},
            {'type': {'selector': '#u', 'text': email}}, {'click': '#b'}, {'wait': 1.5},
            {'type': {'selector': '#k', 'text': code}}, {'click': '#n'}], 'signed_in')
        assert done['taken']['attempt'] == {'failed': False, 'typed_after': False}, done
        assert saved['saved'] is True, saved
        (item,) = _created(config)
        assert _value(item, 'username') == email
        assert [f['id'] for f in item['fields']] == ['username']
        assert code not in json.dumps([done, saved, vault(config)])
        for path in Path(config['root']).rglob('*'):
            if path.is_file():
                assert code.encode() not in path.read_bytes(), path


def test_s5n_a_code_field_alone_is_never_an_attempt(tmp_path):
    """A code typed into a one-time-code field (no email or password field in its form) is no
    sign-in attempt: nothing is judged or saved."""
    config = fixture_config(tmp_path)
    code = '9Z-' + canary()[8:13]
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<p>Start</p>', {})
        sites.pages['/code'] = ('text/html', b'<form id=c method=post action="https://'
                                + ACCOUNT.encode() + b'/account"><input id=k name=code type=text '
                                b'autocomplete=one-time-code><button id=n>Sign in</button></form>', {})
        sites.pages['/account'] = ACCOUNT_PAGE
        install(sites)
        steps = _signin_steps(config, [{'goto': 'https://' + LOGIN + '/code'}, {'wait': 1},
                                       {'type': {'selector': '#k', 'text': code}},
                                       {'click': '#n'}])
        done = run_scenario(steps, broker_fixture=config)[-1]
        assert done['taken']['attempt'] is None, done
        assert _created(config) == [] and code not in json.dumps(done)


def test_s5n_a_field_naming_one_time_code_is_never_an_identifier(tmp_path):
    """r1 Terra: a field whose autocomplete names one-time-code anywhere (its last token says
    email) is never taken as the identifier: nothing is judged or saved."""
    config = fixture_config(tmp_path)
    code = '4X-' + canary()[8:13]
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<p>Start</p>', {})
        sites.pages['/code'] = ('text/html', b'<form id=c method=post action="https://'
                                + ACCOUNT.encode() + b'/account"><input id=k name=code type=text '
                                b'autocomplete="one-time-code email"><button id=n>Sign in</button>'
                                b'</form>', {})
        sites.pages['/account'] = ACCOUNT_PAGE
        install(sites)
        steps = _signin_steps(config, [{'goto': 'https://' + LOGIN + '/code'}, {'wait': 1},
                                       {'type': {'selector': '#k', 'text': code}},
                                       {'click': '#n'}])
        done = run_scenario(steps, broker_fixture=config)[-1]
        assert done['taken']['attempt'] is None, done
        assert _created(config) == [] and code not in json.dumps(done)


def test_s5n_a_code_field_typed_as_email_never_replaces_the_identifier(tmp_path):
    """r1 Astra: email first, then a code field the page types as email but marks as
    one-time-code (in a novalidate form): the saved identifier is the email, never the code."""
    config = fixture_config(tmp_path)
    email = 'it-' + canary()[8:16] + '@example.com'
    code = '731946'
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<p>Start</p>', {})
        sites.pages['/identify'] = ('text/html', b'<form id=f method=post action=/code>'
                                    b'<input id=u name=email type=email autocomplete=email>'
                                    b'<button id=b>Next</button></form>', {})
        sites.pages['/code'] = ('text/html', b'<form id=c method=post novalidate action="https://'
                                + ACCOUNT.encode() + b'/account"><input id=k name=code type=email '
                                b'autocomplete=one-time-code><button id=n>Sign in</button></form>', {})
        sites.pages['/account'] = ACCOUNT_PAGE
        done, saved = _run(config, sites, [
            {'goto': 'https://' + LOGIN + '/identify'}, {'wait': 1},
            {'type': {'selector': '#u', 'text': email}}, {'click': '#b'}, {'wait': 1.5},
            {'type': {'selector': '#k', 'text': code}}, {'click': '#n'}], 'signed_in')
        (item,) = _created(config)
        assert _value(item, 'username') == email, item
        assert code not in json.dumps([done, saved, vault(config)])


def test_s5n_an_input_owned_by_another_form_is_never_the_identifier(tmp_path):
    """r1 Astra: an input placed inside the sign-in form but owned by another form (form=) is
    not the sign-in's: the identifier saved is the one the form submitted."""
    config = fixture_config(tmp_path)
    actual = 'it-' + canary()[8:16] + '@example.com'
    other = ('it-other-' + canary()[8:14] + '@example.com').encode()
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<p>Start</p>', {})
        sites.pages['/identify'] = ('text/html', b'<form id=other></form><form id=f method=post '
                                    b'action="https://' + ACCOUNT.encode() + b'/account">'
                                    b'<input type=hidden form=other autocomplete=username '
                                    b'value="' + other + b'"><input id=u name=email '
                                    b'type=email autocomplete=email><button id=b>Next</button>'
                                    b'</form>', {})
        sites.pages['/account'] = ACCOUNT_PAGE
        done, saved = _run(config, sites, [
            {'goto': 'https://' + LOGIN + '/identify'}, {'wait': 1},
            {'type': {'selector': '#u', 'text': actual}}, {'click': '#b'}], 'signed_in')
        (item,) = _created(config)
        assert _value(item, 'username') == actual, item


def test_s5n_an_unmarked_code_field_after_the_email_is_never_the_identifier(tmp_path):
    """r2 Terra: email first, then a code typed into a field the page marks only as an email
    (type=email, autocomplete=email, a novalidate form): a code is never an email address, so
    the identifier saved stays the email."""
    config = fixture_config(tmp_path)
    email = 'it-' + canary()[8:16] + '@example.com'
    code = '432198'
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<p>Start</p>', {})
        sites.pages['/identify'] = ('text/html', b'<form id=f method=post action=/code>'
                                    b'<input id=u name=email type=email autocomplete=email>'
                                    b'<button id=b>Next</button></form>', {})
        sites.pages['/code'] = ('text/html', b'<form id=c method=post novalidate action="https://'
                                + ACCOUNT.encode() + b'/account"><input id=k name=code type=email '
                                b'autocomplete=email><button id=n>Sign in</button></form>', {})
        sites.pages['/account'] = ACCOUNT_PAGE
        done, saved = _run(config, sites, [
            {'goto': 'https://' + LOGIN + '/identify'}, {'wait': 1},
            {'type': {'selector': '#u', 'text': email}}, {'click': '#b'}, {'wait': 1.5},
            {'type': {'selector': '#k', 'text': code}}, {'click': '#n'}], 'signed_in')
        (item,) = _created(config)
        assert _value(item, 'username') == email, item
        assert code not in json.dumps([done, saved, vault(config)])


def test_s5n_a_hidden_identifier_field_never_counts(tmp_path):
    """r2 Astra: a hidden autocomplete=username input before the real email field of the same
    form is never the identifier: the one shown field's value is."""
    config = fixture_config(tmp_path)
    actual = 'it-' + canary()[8:16] + '@example.com'
    decoy = ('it-decoy-' + canary()[8:14] + '@example.com').encode()
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<p>Start</p>', {})
        sites.pages['/identify'] = ('text/html', b'<form id=f method=post action="https://'
                                    + ACCOUNT.encode() + b'/account"><input type=hidden '
                                    b'autocomplete=username value="' + decoy + b'"><input id=u '
                                    b'name=email type=email autocomplete=email><button id=b>Next'
                                    b'</button></form>', {})
        sites.pages['/account'] = ACCOUNT_PAGE
        done, saved = _run(config, sites, [
            {'goto': 'https://' + LOGIN + '/identify'}, {'wait': 1},
            {'type': {'selector': '#u', 'text': actual}}, {'click': '#b'}], 'signed_in')
        (item,) = _created(config)
        assert _value(item, 'username') == actual, item


def test_s5n_the_saved_email_fills_and_the_emailed_code_signs_in(tmp_path):
    """The item holds only the email: the broker fills it on the email page, the site emails a
    code (needs_code), and the code given later (Ellen's, from Gmail) signs in."""
    from casa_browser.proxy_client import encode_policy
    config = fixture_config(tmp_path, totp=False, identifier_only=True)
    code = '5K-' + canary()[8:13]
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/username'] = form('username', '/code')
        sites.pages['/code'] = ('text/html', b'<p>We emailed you a code.</p>' + form(
            'one-time-code', '/finish')[1], {})
        sites.pages['/finish'] = ('text/html', b'', {'Location': 'https://' + ACCOUNT + '/done'})
        sites.status['/finish'] = 303
        sites.pages['/done'] = ('text/html', b'<p>Signed in</p>', {})
        s = h.site(origins=('https://' + ACCOUNT,), login=('https://' + LOGIN,))
        policy = encode_policy(h.account_policy(sites=(s,)))
        steps = [{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                 instruction(config, 'open_login'), {'goto': 'https://' + LOGIN + '/username'},
                 instruction(config, 'login'), {'observe': policy},
                 instruction(config, 'fill_code', code=code), {'wait': 1}, {'observe': policy}]
        result = run_scenario(steps, broker_fixture=config)
        asked = result[4]
        assert asked['status'] == 'needs_code', asked
        assert result[6] == {'status': 'logged_in', 'reason': 'completed'}, result[6]
        posts = [r for r in sites.receipts.requests if r[0] == 'POST']
        assert [p[2] for p in posts] == ['/code', '/finish']
        assert code not in json.dumps(vault(config))


# --------------------------------------------------------------------------- slice 5u

_SIGNIN = (b'<form method=post action="/finish"><input name=u autocomplete=username>'
           b'<input name=p type=password autocomplete=current-password><button>Go</button></form>')


def _signin_run(tmp_path, path, body, *, start=LOGIN):
    """One automatic sign-in from ``https://<start><path>``: the broker's reply and the
    fixture's requests. /finish redirects to the account's field-free /done."""
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages[path] = ('text/html', body, {})
        sites.pages['/frame'] = ('text/html', b'<p>frame</p>', {})
        sites.pages['/finish'] = ('text/html', b'', {'Location': 'https://' + ACCOUNT + '/done'})
        sites.status['/finish'] = 303
        sites.pages['/done'] = ('text/html', b'<p>Signed in</p>', {})
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               instruction(config, 'open_login'), {'goto': 'https://' + start + path},
                               instruction(config, 'login')], broker_fixture=config)
        return result[-1], [r for r in sites.receipts.requests if r[0] == 'POST']


def test_s5u_a_page_that_moves_during_the_sign_in_is_signed_into(tmp_path):
    """casa-test run_25cb: the sign-in page's subframes navigate (a reCAPTCHA's) and its route
    changes while the broker reads it; the sign-in settles, reads it again and signs in —
    before slice 5u every such change ended the run (broker_refused)."""
    churn = (b'<iframe id=f src="/frame"></iframe><script>let i=0;const t=setInterval(()=>{'
             b'document.getElementById("f").src="/frame?i="+(++i);'
             b'if(i%5==0)history.pushState({},"","/signin?r="+i);if(i>=40)clearInterval(t);},40);</script>')
    reply, posts = _signin_run(tmp_path, '/signin', _SIGNIN + churn)
    assert reply == {'status': 'logged_in', 'reason': 'completed'}, reply
    assert [p[2] for p in posts] == ['/finish']


def test_s5u_a_form_that_renders_late_is_signed_into(tmp_path):
    """Operator ruling 2: a form that renders after the broker's first read is waited for and
    signed into (it was reported logged_in with nothing posted)."""
    late = (b'<div id=app></div><script>setTimeout(()=>{document.getElementById("app").innerHTML='
            + json.dumps(_SIGNIN.decode()).encode() + b'},900);</script>')
    reply, posts = _signin_run(tmp_path, '/signin', late)
    assert reply == {'status': 'logged_in', 'reason': 'completed'}, reply
    assert [p[2] for p in posts] == ['/finish']


def test_s5u_a_site_page_with_no_form_claims_nothing(tmp_path):
    """Operator ruling 2: a sign-in that filled nothing and found no form on the site's page is
    no_form — not logged_in; the model reading the page decides."""
    reply, posts = _signin_run(tmp_path, '/home', b'<p>Welcome back</p>', start=ACCOUNT)
    assert reply == {'status': 'no_form', 'reason': 'no_form'}, reply
    assert posts == []


@pytest.mark.parametrize('replaced', ['password', 'button'])
def test_a_form_the_page_re_renders_under_the_fill_is_signed_into(tmp_path, replaced):
    """The residual of v0.1.0: a sign-in form the page re-renders (without navigating) between
    the broker finding its fields and filling or submitting them — here the username's first
    input event replaces the password field or the submit button with a new one, as a
    framework's re-render does — ended the sign-in as broker_refused, which interrupted the
    run. The sign-in now reads the page again and signs in."""
    selector = 'input[type=password]' if replaced == 'password' else 'button'
    rerender = (b'<script>const u=document.querySelector("input[name=u]");'
                b'u.addEventListener("input",()=>{const e=document.querySelector("'
                + selector.encode() + b'");e.replaceWith(e.cloneNode(true));},{once:true});</script>')
    reply, posts = _signin_run(tmp_path, '/signin', _SIGNIN + rerender)
    assert reply == {'status': 'logged_in', 'reason': 'completed'}, reply
    assert [p[2] for p in posts] == ['/finish']


@pytest.mark.parametrize('shape', ['password', 'email_only'])
def test_a_username_marked_for_passkeys_is_saved_from_the_live_view(tmp_path, shape):
    """The residual of v0.1.0: a site that offers passkeys marks its username field
    ``autocomplete="username webauthn"``; the live view's capture read that field as no role,
    so a login typed there was saved without its username (or, email-only, not at all). The
    trailing ``webauthn`` token is dropped first, as the sign-in's own reader does."""
    config = fixture_config(tmp_path)
    user = ('it-' + canary()[8:16] + '@example.com') if shape == 'email_only' else 'it-user-' + canary()[8:16]
    password = canary()
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        sites.pages['/start'] = ('text/html', b'<p>Start</p>', {})
        field = b'<input id=u name=user type=text autocomplete="username webauthn">'
        if shape == 'password':
            body = (b'<form id=f method=post action="https://' + ACCOUNT.encode() + b'/account">'
                    + field + b'<input id=p name=pass type=password><button id=b>Sign in</button></form>')
            steps = [{'type': {'selector': '#u', 'text': user}},
                     {'type': {'selector': '#p', 'text': password}}, {'click': '#b'}]
        else:
            body = (b'<form id=f method=post action="https://' + ACCOUNT.encode() + b'/account">'
                    + field + b'<button id=b>Next</button></form>')
            steps = [{'type': {'selector': '#u', 'text': user}}, {'click': '#b'}]
        sites.pages['/signin'] = ('text/html', body, {})
        sites.pages['/account'] = ACCOUNT_PAGE
        done, saved = _run(config, sites, [{'goto': 'https://' + LOGIN + '/signin'}, {'wait': 1}]
                           + steps, 'signed_in')
        assert done['taken']['attempt'] == {'failed': False, 'typed_after': False}, done
        assert saved['saved'] is True, saved
        (item,) = _created(config)
        assert _value(item, 'username') == user, item
        if shape == 'password':
            assert _value(item, 'password') == password
        else:
            assert [f['id'] for f in item['fields']] == ['username']


# -- signing in off the start page: the look's read and the binding, in Chrome's DOM ---------------

def _look_run(tmp_path, body, *, between=(), frame=None, settle_s=0, path='/signin',
              fields=None):
    """The look's read of ``https://<login><path>``, then ``between`` (the page's own scripts),
    then the sign-in on the judged page: the read, its bound frames, the sign-in's reply and
    the fixture's POSTs."""
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages[path] = ('text/html', body, {})
        sites.pages['/frame'] = ('text/html', b'<p>frame</p>', {})
        if frame is not None:
            sites.pages['/form'] = ('text/html', frame, {})
        sites.pages['/finish'] = ('text/html', b'', {'Location': 'https://' + ACCOUNT + '/done'})
        sites.status['/finish'] = 303
        sites.pages['/done'] = ('text/html', b'<p>Signed in</p>', {})
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               {'goto': 'https://' + LOGIN + path},
                               instruction(config, 'login', auto=True, between=list(between),
                                           settle_s=settle_s, fields=fields)],
                              broker_fixture=config)
        return result[-3], result[-2], result[-1], [r for r in sites.receipts.requests
                                                    if r[0] == 'POST']


_LABELLED = (b'<form method=post action="/finish"><label for=u>Email address</label>'
             b'<input id=u name=u autocomplete=username><label for=p>Password</label>'
             b'<input id=p name=p type=password autocomplete=current-password>'
             b'<button>Sign in</button></form>')


def test_look_reads_the_judged_page_and_signs_in_on_it(tmp_path):
    """The look's read in Chrome: role names and labels (never a handle), the main document
    bound; the sign-in on the page as it is fills both fields and posts once."""
    read, frames, reply, posts = _look_run(tmp_path, _LABELLED)
    assert read == {'found': True, 'passkey': False, 'fields': [
        {'id': 'f1', 'role': 'username', 'label': 'Email address', 'password': False,
         'button': 'Sign in'},
        {'id': 'f2', 'role': 'password', 'label': 'Password', 'password': True,
         'button': 'Sign in'}]}, read
    assert frames == {'bound_frames': ['https://' + LOGIN + '/signin']}, frames
    assert reply == {'status': 'logged_in', 'reason': 'completed'}, reply
    assert [p[2] for p in posts] == ['/finish']


@pytest.mark.parametrize('change', [
    'setTimeout(() => location.reload(), 0)',                                  # a new document
    'document.querySelector("label").textContent = "Confirm your password"',   # other text
    'document.querySelector("form").action = "/account/delete"',               # another destination
])
def test_a_judged_page_that_changed_is_never_filled(tmp_path, change):
    """R7 in Chrome's DOM: after the look, the same address reloaded, the same form's text or
    its destination changed: 0 fills, 0 posts, ``no_form/page_moved``."""
    _read, _frames, reply, posts = _look_run(tmp_path, _LABELLED, between=[change], settle_s=1)
    assert reply == {'status': 'no_form', 'reason': 'page_moved'}, reply
    assert posts == []


def test_the_credential_iframe_navigating_is_never_filled(tmp_path):
    """The form in an iframe of the login origin is bound; that iframe loading again (the same
    shape, another document) after the look: 0 posts."""
    page = b'<p>Sign in below</p><iframe id=f src="/form"></iframe>'
    read, frames, reply, posts = _look_run(
        tmp_path, page, frame=_LABELLED, settle_s=1,
        between=['document.getElementById("f").src = "/form?again"'])
    assert read['found'] is True and len(frames['bound_frames']) == 2, frames
    assert reply == {'status': 'no_form', 'reason': 'page_moved'}, reply
    assert posts == []


def test_an_unbound_iframe_navigating_never_stops_the_sign_in(tmp_path):
    """Astra r5 in Chrome: a reCAPTCHA-like iframe that keeps navigating, between the look and
    the sign-in and during it, is not bound: the sign-in goes on and posts once."""
    churn = (b'<iframe id=c src="/frame"></iframe><script>let i=0;const t=setInterval(()=>{'
             b'document.getElementById("c").src="/frame?i="+(++i);if(i>=30)clearInterval(t);},60);'
             b'</script>')
    read, frames, reply, posts = _look_run(tmp_path, _LABELLED + churn, settle_s=0.5)
    assert frames == {'bound_frames': ['https://' + LOGIN + '/signin']}, frames
    assert reply == {'status': 'logged_in', 'reason': 'completed'}, reply
    assert [p[2] for p in posts] == ['/finish']


def test_a_page_with_only_a_plain_field_is_judged_and_never_filled_unmapped(tmp_path):
    """the-internet's reset form as served (a plain text e-mail field): planned for the check
    (R9); with no mapping (the check said nothing about it) nothing is filled or posted."""
    reset = (b'<form method=post action="/forgot_password"><label for=email>E-mail</label>'
             b'<input type=text name=email id=email><button>Retrieve password</button></form>')
    read, _frames, reply, posts = _look_run(tmp_path, reset)
    assert read['found'] is True and [(f['id'], f['role']) for f in read['fields']] == [
        ('f1', None)], read
    assert reply == {'status': 'no_form', 'reason': 'no_form'}, reply
    assert posts == []


def test_r9_a_password_never_goes_into_a_text_field_the_page_marks_as_one(tmp_path):
    """Astra (R9 round): a text field with autocomplete=current-password is no password field:
    a look's sign-in never fills the password into it."""
    page = (b'<form method=post action="/finish"><input type=text name=u>'
            b'<input type=text name=p autocomplete=current-password><button>Login</button></form>')
    _read, _frames, _reply, posts = _look_run(tmp_path, page, fields={'username': 'f1'})
    for _path, body in _posted(posts):
        assert not body.get('p', [''])[0]


# R9: the check maps the fields the page does not mark (the-internet's plain username)
_INTERNET = (b'<form method=post action="/finish"><label for=username>Username</label>'
             b'<input type=text name=username id=username><label for=password>Password</label>'
             b'<input type=password name=password id=password>'
             b'<button class=radius type=submit><i> Login</i></button></form>')
_SEARCH = (b'<form method=get action="/search"><label for=q>Search</label>'
           b'<input type=text id=q name=q><button type=submit>Search</button></form>')


def _posted(posts):
    from urllib.parse import parse_qs
    return [(p[2], parse_qs(p[3].decode())) for p in posts]


def test_r9_a_plain_username_the_check_mapped_is_filled(tmp_path):
    """the-internet as served: its username field marks no role; the read plans it as a plain
    field, the mapping names it, and both fields are posted once."""
    read, _frames, reply, posts = _look_run(tmp_path, _INTERNET, fields={'username': 'f1'})
    assert [(f['id'], f['role']) for f in read['fields']] == [('f1', None), ('f2', 'password')]
    assert reply == {'status': 'logged_in', 'reason': 'completed'}, reply
    (path, body), = _posted(posts)
    assert path == '/finish' and body['username'][0] and body['password'][0]


def test_r9_a_search_box_beside_the_form_is_not_filled(tmp_path):
    """The decoy: a search box in its own form before the sign-in form; the mapping names the
    sign-in form's username (f2), so only the sign-in form is filled and sent."""
    read, _frames, reply, posts = _look_run(tmp_path, _SEARCH + _INTERNET,
                                            fields={'username': 'f2'})
    assert [f['label'] for f in read['fields']] == ['Search', 'Username', 'Password']
    assert reply == {'status': 'logged_in', 'reason': 'completed'}, reply
    assert [p[0] for p in _posted(posts)] == ['/finish']


def test_r9_a_password_never_goes_into_a_plain_field(tmp_path):
    """A mapping that names a plain text field for the password: the driver gives that field no
    role (a password goes only into a password field), so it is never filled."""
    _read, _frames, reply, posts = _look_run(tmp_path, _INTERNET, fields={'password': 'f1'})
    for _path, body in _posted(posts):
        assert not body.get('username', [''])[0]


def test_r9_a_marked_field_the_check_did_not_name_is_never_filled(tmp_path):
    """Terra (R9 post-fix): a newsletter box the page marks as an email field, in its own form
    after the-internet's form; the check names the login form's fields (f1, f2): the sign-in
    posts that form once and never fills or sends the newsletter box."""
    news = (b'<form method=post action="/subscribe"><label for=nl>Email</label>'
            b'<input type=email id=nl name=nl><button type=submit>Subscribe</button></form>')
    read, _frames, reply, posts = _look_run(tmp_path, _INTERNET + news,
                                            fields={'username': 'f1', 'password': 'f2'})
    assert [(f['id'], f['role']) for f in read['fields']] == [
        ('f1', None), ('f2', 'password'), ('f3', 'username')], read
    assert reply == {'status': 'logged_in', 'reason': 'completed'}, reply
    (path, body), = _posted(posts)
    assert path == '/finish' and body['username'][0] and body['password'][0]


def test_r9_a_marked_field_beyond_the_plan_never_spoils_the_named_ones(tmp_path):
    """Astra (R9 round 3): the plan holds at most 200 fields; a newsletter box the page marks as
    an email field after 198 plain inputs is not in it. In the judged document only planned
    fields count, so the named login form signs in (one post) and the box is never filled."""
    filler = b''.join(b'<input type=text name=x%d>' % n for n in range(198))
    news = (b'<form method=post action="/subscribe"><input type=email id=nl name=nl>'
            b'<button type=submit>Subscribe</button></form>')
    read, _frames, reply, posts = _look_run(tmp_path, _INTERNET + b'<div>' + filler + b'</div>'
                                            + news, fields={'username': 'f1', 'password': 'f2'})
    assert len(read['fields']) == 200 and read['fields'][-1]['id'] == 'f200', read['fields'][-1]
    assert reply == {'status': 'logged_in', 'reason': 'completed'}, reply
    assert [p for p, _b in _posted(posts)] == ['/finish']


# -- forms like two measured sign-in pages: a button a widget holds, a button the form's own checks
# hold, a plain username box. The fixtures are synthetic; each shape ends in exactly one POST.

_HELD_BY_WIDGET = (
    b'<form method=post action="/finish"><label for=u>Username</label>'
    b'<input id=u name=userName type=text><label for=p>Password</label>'
    b'<input id=p name=password type=password>'
    b'<input type=hidden name=cf-turnstile-response id=t><button id=b disabled>Sign In</button></form>'
    # the widget's cross-origin iframe, and its solve a while after the form
    b'<iframe src="https://' + OTHER.encode() + b'/frame"></iframe>'
    b'<script>setTimeout(() => {document.getElementById("t").value = "token";'
    b'document.getElementById("b").disabled = false;}, 3000);</script>')

_HELD_BY_FORM = (
    b'<form method=post action="/finish">'
    b'<input name=username type=text aria-label=Username placeholder=Username required>'
    b'<input name=userPassword type=password aria-label=Password required>'
    b'<input id=b type=submit value="Log in" disabled aria-disabled=true></form>'
    # both fields filled: the same node comes on, and keeps aria-disabled=true throughout
    b'<script>const f = document.forms[0], b = document.getElementById("b");'
    b'f.addEventListener("input", () => {b.disabled = !(f.username.value && f.userPassword.value);});'
    b'</script>')

_NEVER_ON = (
    b'<form method=post action="/finish"><input name=username type=text aria-label=Username>'
    b'<input name=password type=password aria-label=Password><button disabled>Sign In</button></form>')

_ON_WITH_A_LATE_BOX = _HELD_BY_FORM.replace(
    b'</script>', b'f.addEventListener("input", () => {if (!b.disabled && !f.code) {'
    b'const x = document.createElement("input"); x.name = "code"; x.type = "text";'
    b'f.append(x);}});</script>')

_ON_WIPING_THE_PASSWORD = _HELD_BY_FORM.replace(
    b'</script>', b'f.addEventListener("input", () => {if (!b.disabled) setTimeout(() => {'
    b'f.userPassword.value = ""; }, 0);});</script>')

_BOTH = {'username': 'f1', 'password': 'f2'}


def _held_run(tmp_path, body, *, fields=_BOTH, finish=None):
    """The start page's sign-in on the page, the look's read and the sign-in with the check's
    ``fields``: the start page's reply, the read, the sign-in's reply, the POSTs."""
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, ACCOUNT, OTHER]) as sites:
        install(sites)
        sites.pages['/signin'] = ('text/html', body, {})
        sites.pages['/frame'] = ('text/html', b'<p>widget</p>', {})
        sites.pages['/finish'] = ('text/html', b'', {'Location': 'https://' + ACCOUNT + '/done'})
        sites.status['/finish'] = 303
        sites.pages['/done'] = ('text/html', b'<p>Signed in</p>', {})
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               {'goto': 'https://' + LOGIN + '/signin'},
                               instruction(config, 'login'),
                               instruction(config, 'login', auto=True, fields=fields)],
                              broker_fixture=config)
        return (result[3], result[4], result[6],
                [r for r in sites.receipts.requests if r[0] == 'POST'], vault(config)[0])


def _value_of(item, field_id):
    return next(f['value'] for f in item['fields'] if f['id'] == field_id)


@pytest.mark.parametrize('body, button, names', [
    (_HELD_BY_WIDGET, 'Sign In', (b'userName', b'password')),
    (_HELD_BY_FORM, 'Log in', (b'username', b'userPassword'))],
    ids=['a widget holds the button', 'the form holds the button'])
def test_a_plain_username_form_with_a_held_button_is_sent_once(tmp_path, body, button, names):
    """The start page does nothing and hands over; the look plans the plain box and the disabled
    button; the sign-in fills both fields, waits for the button, presses once. One POST with
    both values."""
    start, read, reply, posts, item = _held_run(tmp_path, body)
    assert start == {'status': 'no_form', 'reason': 'needs_check'}, start
    assert [(f['id'], f['role'], f['button']) for f in read['fields']][:2] == [
        ('f1', None, button), ('f2', 'password', button)], read
    assert reply == {'status': 'logged_in', 'reason': 'completed'}, reply
    assert [p[2] for p in posts] == ['/finish'], posts
    sent = parse_qs(posts[0][3].decode())
    assert sent[names[0].decode()] == [_value_of(item, 'username')]
    assert sent[names[1].decode()] == [_value_of(item, 'password')]


def test_a_box_of_another_form_stops_nothing(tmp_path):
    """A search form beside the sign-in form (its box planned, unmapped, empty) is not this
    form's box: one POST."""
    body = _HELD_BY_FORM + b'<form action="/search"><input name=q type=search aria-label=Search><button>Go</button></form>'
    start, read, reply, posts, _item = _held_run(tmp_path, body)
    assert start == {'status': 'no_form', 'reason': 'needs_check'}, start
    assert reply == {'status': 'logged_in', 'reason': 'completed'}, reply
    assert [p[2] for p in posts] == ['/finish'], posts


def test_a_button_that_never_comes_on_sends_nothing(tmp_path):
    _start, _read, reply, posts, _item = _held_run(tmp_path, _NEVER_ON)
    assert reply == {'status': 'needs_live_view', 'reason': 'submit_disabled'}, reply
    assert posts == []


def test_the_password_alone_never_goes_beside_an_empty_box(tmp_path):
    """The check named the password only, with the plain box of its form left empty: nothing
    filled, nothing sent, the live view."""
    start, _read, reply, posts, _item = _held_run(tmp_path, _HELD_BY_FORM, fields={'password': 'f2'})
    assert start == {'status': 'no_form', 'reason': 'needs_check'}, start
    assert reply == {'status': 'needs_live_view', 'reason': 'empty_field'}, reply
    assert posts == []


_ON_HIDING_THE_PASSWORD = _ON_WITH_A_LATE_BOX.replace(
    b'const x =', b'f.userPassword.style.display = "none"; const x =')

_ON_RETYPING_THE_PASSWORD = _ON_WITH_A_LATE_BOX.replace(
    b'const x =', b'f.userPassword.type = "text"; const x =')


@pytest.mark.parametrize('body', [_ON_WITH_A_LATE_BOX, _ON_WIPING_THE_PASSWORD,
                                  _ON_HIDING_THE_PASSWORD],
                         ids=['a box renders late', 'a re-render wipes the password',
                              'the password is hidden with a late box'])
def test_the_click_itself_stops_at_an_empty_box(tmp_path, body):
    """The form changes after the fill, during the wait: the check in the click's own
    evaluation stops it. 0 POSTs, the live view, no error."""
    _start, _read, reply, posts, _item = _held_run(tmp_path, body)
    assert reply == {'status': 'needs_live_view', 'reason': 'empty_field'}, reply
    assert posts == []


def test_a_form_that_hides_its_sent_password_is_not_pressed_again(tmp_path):
    """An AJAX form that hides the password after the send, keeping its value: the next read
    shows only the username, and the form would post the password again. 1 POST, the operator."""
    body = _LABELLED.replace(b'<form method=post action="/finish">', b'<form id=f>') + (
        b'<script>document.getElementById("f").addEventListener("submit", ev => {ev.preventDefault();'
        b'fetch("/finish", {method: "POST", body: new URLSearchParams(new FormData(ev.target))});'
        b'document.getElementById("p").style.display = "none";});</script>')
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/signin'] = ('text/html', body, {})
        sites.pages['/finish'] = ('text/plain', b'ok', {})
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               {'goto': 'https://' + LOGIN + '/signin'},
                               instruction(config, 'login')], broker_fixture=config)
        assert result[-1] == {'status': 'needs_live_view', 'reason': 'password_again'}, result[-1]
        assert [r[2] for r in sites.receipts.requests if r[0] == 'POST'] == ['/finish']


@pytest.mark.parametrize('extra', [b'', b'const x = document.createElement("input"); x.name = "code"; f.append(x);'],
                         ids=['alone', 'with an empty box'])
def test_a_form_that_hides_the_password_before_the_click_is_not_pressed(tmp_path, extra):
    """The second reviewer case that sent six POSTs: the password's input listener hides it and
    clones the button, an AJAX form posts its FormData on every click. The read after the page
    change shows only the username: 0 POSTs, the operator."""
    body = _LABELLED.replace(b'<form method=post action="/finish">', b'<form id=f>') + (
        b'<script>const f = document.getElementById("f"), p = document.getElementById("p");'
        b'f.addEventListener("submit", ev => {ev.preventDefault();'
        b'fetch("/finish", {method: "POST", body: new URLSearchParams(new FormData(f))});});'
        b'p.addEventListener("input", () => {if (p.style.display === "none") return;'
        b'p.style.display = "none"; const b = f.querySelector("button"); b.replaceWith(b.cloneNode(true));'
        + extra + b'});</script>')
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/signin'] = ('text/html', body, {})
        sites.pages['/finish'] = ('text/plain', b'ok', {})
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               {'goto': 'https://' + LOGIN + '/signin'},
                               instruction(config, 'login')], broker_fixture=config)
        assert result[-1] == {'status': 'needs_live_view', 'reason': 'password_again'}, result[-1]
        assert [r for r in sites.receipts.requests if r[0] == 'POST'] == []


@pytest.mark.parametrize('body', [
    b'<form action="/finish" method=post><input name=u autocomplete=username><button>Go</button></form>'
    b'<form action="/finish" method=post><input name=p type=password autocomplete=current-password></form>',
    b'<form action="/finish" method=post><input name=p type=password autocomplete=current-password><button>Go</button></form>'
    b'<form action="/finish" method=post><input name=u autocomplete=username></form>'],
    ids=['the button is the username form', 'the button is the password form'])
def test_a_button_of_another_form_is_never_pressed(tmp_path, body):
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/signin'] = ('text/html', body, {})
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               {'goto': 'https://' + LOGIN + '/signin'},
                               instruction(config, 'login')], broker_fixture=config)
        assert result[-1] == {'status': 'needs_live_view', 'reason': 'ambiguous_fields'}, result[-1]
        assert [r for r in sites.receipts.requests if r[0] == 'POST'] == []


def test_a_password_retyped_with_a_late_box_is_not_sent(tmp_path):
    """The binding sees the field's type change before the click: 0 POSTs."""
    _start, _read, reply, posts, _item = _held_run(tmp_path, _ON_RETYPING_THE_PASSWORD)
    assert reply == {'status': 'no_form', 'reason': 'page_moved'}, reply
    assert posts == []


@pytest.mark.parametrize('box', [b'<input type=number name=code>', b'<input type=url name=code>',
                                 b'<textarea name=code></textarea>'],
                         ids=['number', 'url', 'textarea'])
def test_a_box_of_any_type_holds_the_password_back(tmp_path, box):
    """Marked username and password, and an empty box of a type the look does not plan: the
    start page does nothing, the check's mapping leaves it out: 0 POSTs, the live view."""
    body = _LABELLED.replace(b'<button>', box + b'<button>')
    start, _read, reply, posts, _item = _held_run(tmp_path, body)
    assert start == {'status': 'no_form', 'reason': 'needs_check'}, start
    assert reply == {'status': 'needs_live_view', 'reason': 'empty_field'}, reply
    assert posts == []


def test_a_form_that_comes_back_gets_the_password_once(tmp_path):
    """The start page's sign-in on a form that returns with no error mark (the old loop posted
    it up to six times): 1 POST, then the operator."""
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, ACCOUNT]) as sites:
        install(sites)
        sites.pages['/signin'] = ('text/html', _LABELLED, {})
        sites.pages['/finish'] = ('text/html', _LABELLED, {})
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               {'goto': 'https://' + LOGIN + '/signin'},
                               instruction(config, 'login')], broker_fixture=config)
        assert result[-1] == {'status': 'needs_live_view', 'reason': 'password_again'}, result[-1]
        assert [r[2] for r in sites.receipts.requests if r[0] == 'POST'] == ['/finish']


def test_a_capture_opens_beside_a_frame_elsewhere_and_chrome_stays(tmp_path):
    """The live view's capture on the widget page, a cross-origin frame in it: armed, then
    closed with the same Chrome running (the session generation unchanged)."""
    config = fixture_config(tmp_path)
    with ChromeSites([LOGIN, ACCOUNT, OTHER]) as sites:
        install(sites)
        sites.pages['/signin'] = ('text/html', _HELD_BY_WIDGET, {})
        sites.pages['/frame'] = ('text/html', b'<p>widget</p>', {})
        result = run_scenario([{'open': {'site_id': 's1'}}, instruction(config, 'setup'),
                               {'goto': 'https://' + LOGIN + '/signin'},
                               instruction(config, 'begin_capture'),
                               instruction(config, 'review'),
                               {'chrome_alive': True}], broker_fixture=config)
        assert 'capture_id' in result[3], result[3]
        assert result[-1] == {'pages': ['https://' + LOGIN + '/signin'], 'generation': 1}, result[-1]
