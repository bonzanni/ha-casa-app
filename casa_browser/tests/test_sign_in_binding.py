"""The look's binding in the login driver (R7, rev 5.1): what a ``sign_in`` verdict covers is
marked in every credential-bearing frame document (and 1Password's notification frame) at the
look's read, and ``sign_in_bound`` holds only while every bound document keeps its mark, every
field and the submit stay in a bound document, and those fields' and that submit's facts read as
they did. The text around them is not bound. Page-free: a document is a small double; the real
DOM reads are image-tested."""
from __future__ import annotations

import pytest

from casa_browser import login
from test_login import driver

pytestmark = pytest.mark.asyncio

ORIGIN = 'https://login.example'


class Doc:
    def __init__(self, text='Login Page', prompt='', els=()):
        self.mark, self.text, self.prompt, self.els = None, text, prompt, list(els)


def _origin(url):
    parts = url.split('/')
    return parts[0] + '//' + parts[2] if url.startswith('http') else None


class Frame:
    def __init__(self, url, doc, parent=None):
        self.url, self.doc, self.parent_frame, self.detached = url, doc, parent, False

    def is_detached(self):
        return self.detached

    async def evaluate(self, script, arg=None):
        if script == '() => location.origin':
            return _origin(self.url)
        if script == login._SIGN_IN_MARK:
            self.doc.mark = arg
            return True
        if script == login._SIGN_IN_MARKED:
            return self.doc.mark == arg
        if script == login._PROMPT_SHOWN:
            return bool(self.doc.prompt)
        if script == login._PLAN:              # as the script does: mark, then report
            out, n = [], arg['start']
            buttons = [e for e in self.doc.els if e.tag == 'BUTTON']
            for e in self.doc.els:
                if e.tag != 'INPUT':
                    continue
                n += 1
                e.planned = (arg['look'], f'f{n}')
                for btn in buttons:
                    btn.planned = (arg['look'], 'button')
                out.append({'id': f'f{n}', 'role': e.role, 'label': e.label,
                            'password': e.type == 'password',
                            'button': buttons[0].label if buttons else None})
            return out
        if script == login._SCAN:
            return {e.planned[1]: e.facts() for e in self.doc.els
                    if e.planned and e.planned[0] == arg}
        raise AssertionError(script)


class El:
    def __init__(self, label, tag='INPUT', type='text', role=None, autocomplete=None,
                 action='https://login.example/session'):
        self.label, self.tag, self.type, self.role = label, tag, type, role
        self.autocomplete, self.dest, self.planned = autocomplete, [action, 'post', None, None], None

    def facts(self):
        return [self.tag, self.type, self.autocomplete, self.label, *self.dest]

    async def evaluate(self, script, arg=None):
        if script == login._FACTS:
            return ([self.planned[1], self.facts()] if self.planned and self.planned[0] == arg
                    else None)
        raise AssertionError(script)


class Rig:
    """A login driver session on a page of a main frame, a credential iframe holding the form,
    an unbound (reCAPTCHA-like) iframe and, with ``passkey``, 1Password's notification frame."""

    def __init__(self, tmp_path, *, passkey=False):
        self.server, _, self.session, self.page, self.proxy = driver(tmp_path)
        self.user = El('Username')                                   # plain: no role marked
        self.password = El('Password', type='password', role='password',
                           autocomplete='current-password')
        self.button = El('Log in', tag='BUTTON', type='submit')
        self.main = Frame(ORIGIN + '/login', Doc('Sign in to Example'))
        self.form = Frame(ORIGIN + '/form', Doc('Username Password Log in',
                                                 els=(self.user, self.password, self.button)),
                          self.main)
        self.captcha = Frame('https://www.google.com/recaptcha/anchor', Doc('I am not a robot'),
                             self.main)
        self.note = Frame(login.EXTENSION_NOTIFICATION + 'x', Doc('', 'Sign in with a passkey'),
                          self.main) if passkey else None
        self.page.main_frame = self.main
        self.page.frames = [f for f in (self.main, self.form, self.captcha, self.note) if f]
        self.where = {'user': self.form, 'password': self.form, 'button': self.form}
        self.passkey = passkey

    async def open(self):
        self.sid = await self.server.open_session('login', 'run', 'site', self.session,
                                                  self.proxy, (ORIGIN,))
        self.s = self.server._sessions[self.sid]
        self.server.find_login_fields = self._find
        return self

    async def _find(self, driver_session, page_generation):
        """What ``find_login_fields`` leaves: the step's handles, each in its frame."""
        s = self.s
        s.handles.clear()
        entries = {'u': (self.user, 'user', 'text', 'username'),
                   'p': (self.password, 'password', 'password', 'password'),
                   'b': (self.button, 'button', None, 'submit')}
        for handle, (el, where, kind, role) in entries.items():
            s.handles[handle] = (el, self.where[where], kind, ORIGIN, page_generation, role)
        return {'page_generation': page_generation, 'origin_ok': True,
                'roles': {'username': 'u', 'password': 'p'}, 'submit': 'b',
                'field_error': False, 'passkey_frame': self.passkey, 'passkey_hint': False}

    @property
    def generation(self):
        return self.session.view_generation

    async def read(self, mark='m' * 32):
        return await self.server.dispatch({'op': 'read_sign_in', 'driver_session': self.sid,
                                           'page_generation': self.generation, 'mark': mark})

    async def bound(self, mark='m' * 32):
        await self._find(self.sid, self.generation)          # the judged step's own read
        return await self.server.dispatch({'op': 'sign_in_bound', 'driver_session': self.sid,
                                           'page_generation': self.generation, 'mark': mark})


async def test_the_read_names_roles_and_labels_never_a_handle(tmp_path):
    """R9: each planned field by id, its marked role or none, label, password or not and its
    button; the fields and the button marked with the look; never a handle."""
    r = await Rig(tmp_path).open()
    reply = await r.read()
    assert reply['fields'] == [
        {'id': 'f1', 'role': None, 'label': 'Username', 'password': False, 'button': 'Log in'},
        {'id': 'f2', 'role': 'password', 'label': 'Password', 'password': True,
         'button': 'Log in'}]
    assert reply['roles'] == ['password', 'username']
    assert not {'u', 'p', 'b'} & {str(v) for v in reply.values()}
    assert r.main.doc.mark and r.form.doc.mark and r.captcha.doc.mark is None
    assert r.user.planned == ('m' * 32, 'f1') and r.button.planned == ('m' * 32, 'button')
    assert r.server.sign_in_frames('run', 'site') == (r.main, r.form)


async def test_a_field_the_look_did_not_mark_is_not_bound(tmp_path):
    """Only fields of the judged reading are filled: a field that appeared in a bound document
    after the look carries no mark of it."""
    r = await Rig(tmp_path).open()
    read = await r.read()
    r.user = El('Username')                                      # a new element, unmarked
    r.form.doc.els[0] = r.user
    assert await r.bound() == {'ok': False}
    assert read['binding']


async def test_an_unchanged_page_holds_its_binding(tmp_path):
    r = await Rig(tmp_path).open()
    read = await r.read()
    assert await r.bound() == {'ok': True, 'binding': read['binding']}


async def test_an_unbound_frame_that_navigates_changes_nothing(tmp_path):
    """Astra r5: the reCAPTCHA-like frame's new documents are not bound."""
    r = await Rig(tmp_path).open()
    read = await r.read()
    for n in range(3):
        r.captcha.doc = Doc(f'challenge {n}')
    assert await r.bound() == {'ok': True, 'binding': read['binding']}


async def _moved(r, read):
    reply = await r.bound()
    return not (reply.get('ok') is True and all(
        read['binding'].get(fid) == facts for fid, facts in reply['binding'].items()))


async def test_the_same_address_reloaded_into_a_confirmation_is_not_bound(tmp_path):
    r = await Rig(tmp_path).open()
    read = await r.read()
    r.main.doc = Doc('Sign in to Example')                     # a reload: a new document
    assert await _moved(r, read)


async def test_the_text_around_the_form_is_not_bound(tmp_path):
    """A banner, a price, a password manager's line: the judged fields and button are unchanged."""
    r = await Rig(tmp_path).open()
    read = await r.read()
    r.main.doc.text = 'We use cookies. From 2 EUR'
    r.form.doc.text = 'The menu is available. Username Password Log in'
    assert not await _moved(r, read)


async def test_a_judged_field_with_another_label_is_not_bound(tmp_path):
    r = await Rig(tmp_path).open()
    read = await r.read()
    r.password.label = 'Confirm your password'
    assert await _moved(r, read)


async def test_a_field_the_step_does_not_use_is_not_held_to_its_facts(tmp_path):
    """A search box beside the form, planned by the look with a placeholder that rotates: only
    what the step fills and presses is held, under its own id."""
    r = await Rig(tmp_path).open()
    search = El('Search')
    r.main.doc.els = [search]
    read = await r.read()
    assert len(read['binding']) == 4
    search.label = 'Search Black Lotus'
    assert not await _moved(r, read)


async def test_a_same_shaped_form_with_another_destination_is_not_bound(tmp_path):
    r = await Rig(tmp_path).open()
    read = await r.read()
    r.button.dest[2] = 'https://login.example/account/delete'   # the submit's own formaction
    assert await _moved(r, read)


async def test_the_credential_iframe_navigating_is_not_bound(tmp_path):
    r = await Rig(tmp_path).open()
    read = await r.read()
    r.form.doc = Doc('Username Password Log in')               # the same shape, a new document
    assert await _moved(r, read)


async def test_a_field_moved_into_an_unbound_frame_is_not_bound(tmp_path):
    r = await Rig(tmp_path).open()
    read = await r.read()
    r.where['password'] = r.captcha
    assert await r.bound() == {'ok': False}
    assert read['binding']


async def test_a_passkey_prompt_in_a_new_document_is_not_bound(tmp_path):
    """The notification frame's document is bound by its mark, not its prompt's text (the prompt's
    answer itself still needs the linked item's title and username: ``_PASSKEY_BUTTON``)."""
    r = await Rig(tmp_path, passkey=True).open()
    read = await r.read()
    assert r.note.doc.mark
    assert await r.bound() == {'ok': True, 'binding': read['binding']}
    r.note.doc.prompt = 'Sign in with a passkey, 1 item'
    assert not await _moved(r, read)
    r.note.doc = Doc('', 'Sign in with a passkey')             # its frame's new document
    assert await _moved(r, read)


async def test_another_mark_is_not_the_looks(tmp_path):
    r = await Rig(tmp_path).open()
    await r.read('m' * 32)
    assert await r.bound('n' * 32) == {'ok': False}


async def test_a_binding_is_never_read_for_another_page(tmp_path):
    r = await Rig(tmp_path).open()
    await r.read()
    r.server._sign_in_frames[('run', 'site')] = (object(), (r.main, r.form))
    assert await r.bound() == {'ok': False}


async def test_a_passkey_prompt_that_appears_after_the_look_is_not_bound(tmp_path):
    """Terra (applied diff r1): a look that saw no prompt binds none; 1Password's prompt shown
    afterwards is not the judged page's, so the binding no longer holds."""
    r = await Rig(tmp_path).open()
    read = await r.read()
    late = Frame(login.EXTENSION_NOTIFICATION + 'late', Doc('', 'Sign in with a passkey'), r.main)
    r.page.frames.append(late)
    assert await r.bound() == {'ok': False}
    late.doc.prompt = ''                                       # a frame that shows no prompt
    assert await r.bound() == {'ok': True, 'binding': read['binding']}
