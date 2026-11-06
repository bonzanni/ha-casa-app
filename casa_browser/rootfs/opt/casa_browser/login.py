"""Session-bound login driver interface."""
import asyncio
import hashlib
import json
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from functools import wraps
from inspect import signature
from pathlib import Path

from casa_browser.broker import BrokerError
from casa_browser.policy import Lease
from casa_browser.protocol import ProviderBinding, is_email_identifier
from casa_browser.credential_rpc import UnixServer

GUEST_ORIGINS = frozenset("https://my.1password." + suffix for suffix in ("com", "eu", "ca"))
EXTENSION_ORIGIN = "chrome-extension://aeblfdkhhhdcdjpifhhbdiojplfjncoa"
# Slice 5v, measured on the real extension (8.12.42.36): its popup, opened as a tab the driver
# owns, shows the lock screen or the vault list; its in-page notification frame carries the
# "Sign in with a passkey" prompt.
EXTENSION_POPUP_PATH = "/popup/index.html"
EXTENSION_POPUP = EXTENSION_ORIGIN + EXTENSION_POPUP_PATH
GUEST_SIGNIN_PATH = "/signin"
EXTENSION_NOTIFICATION = EXTENSION_ORIGIN + "/inline/notification/"
EXTENSION_PAGES = EXTENSION_ORIGIN + "/app/"

log = logging.getLogger("casa_browser.login")

@dataclass(frozen=True)
class LoginFields:
    page_generation: int
    origin_ok: bool
    roles: dict[str, str]
    submit: str | None
    field_error: bool = False
    passkey_frame: bool = False
    # A visible, enabled input on a login origin whose autocomplete ends with the token
    # ``webauthn``: the site's passkey request, and the only sign of one while the extension's
    # guest has never signed in (no prompt is shown then).
    passkey_hint: bool = False
    # ``submit`` is a disabled button now (a widget or the form's own checks hold it): the step
    # fills, then waits for it, within the broker's bound.
    submit_ready: bool = True
    # A shown, empty box of the password's form that this read will not fill: a password never
    # goes out beside one.
    empty_box: bool = False

@dataclass(frozen=True)
class CapturedValue:
    role: str
    value: str = field(repr=False)

@dataclass(frozen=True)
class CapturedFields:
    values: tuple[CapturedValue, ...] = field(repr=False)
    incomplete: bool = False
    # The capture's last sign-in attempt (action model slice 5, S5-4): ``{username, password,
    # failed, typed_after}``, or None. Its values travel only to the broker.
    attempt: dict | None = field(default=None, repr=False)

DRIVER_OPS = {'find_login_fields': ('driver_session', 'page_generation'), 'fill': ('driver_session', 'page_generation', 'field_handle', 'value'), 'submit': ('driver_session', 'page_generation', 'submit_handle'), 'settle': ('driver_session', 'timeout_s'), 'choose_passkey': ('driver_session', 'page_generation', 'title', 'username'), 'guest_sign_in': ('driver_session', 'region_host', 'email', 'secret_key', 'password'), 'guest_unlock': ('driver_session', 'password'), 'guest_state': ('driver_session',), 'begin_live_capture': ('driver_session',), 'take_capture': ('driver_session', 'capture_id'), 'mark_document': ('driver_session', 'page_generation', 'mark'), 'document_marked': ('driver_session', 'page_generation', 'mark'),
              # signing in off the start page: the look's read and its binding
              'read_sign_in': ('driver_session', 'page_generation', 'mark'),
              'sign_in_bound': ('driver_session', 'page_generation', 'mark'),
              'submit_usable': ('driver_session', 'page_generation', 'submit_handle')}

DRIVER_KINDS = {
    'settle': 'login', 'find_login_fields': 'login', 'fill': 'login',
    'submit': 'login', 'choose_passkey': 'login',
    'guest_state': 'guest', 'guest_sign_in': 'guest', 'guest_unlock': 'guest',
    'begin_live_capture': 'capture', 'take_capture': 'capture',
    'mark_document': 'login', 'document_marked': 'login',
    'read_sign_in': 'login', 'sign_in_bound': 'login', 'submit_usable': 'login',
}

@dataclass(repr=False)
class _Session:
    kind: str
    run_id: str
    site_id: str
    browser: object
    page: object
    proxy: object
    origins: frozenset
    generation: int
    broker_origins: frozenset | None = None
    lease: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: object = None
    closed: bool = False
    aborted: bool = False
    cleanup_task: object = None
    token: object = None
    submitted: bool = False
    handles: dict = field(default_factory=dict)
    addresses: dict = field(default_factory=dict)   # handle -> its frame's address origin at issue (5u)
    departed: bool = False   # a frame holding a live handle left its origin or the page (5u)
    watch: object = None     # the login session's frame listener on its page (5u)
    guest_page: object = None
    tabs: frozenset | None = None    # the guest session's start: Chrome's tabs before it (5w)
    capture_id: str | None = None
    capture_origins: frozenset = frozenset()
    collecting: bool = False
    buffer: object = None
    listeners: list = field(default_factory=list)
    tasks: set = field(default_factory=set)
    binding: object = None
    reader: object = None            # slice 5: the controller's page reader (a capture's readings)
    mapping: object = None           # R9: (look mark, {field id: role}) for this discovery call
    readings: list = field(default_factory=list)


_OPERATION_TIMEOUT = 30
# Opening a live view's capture. The driver answers first, so the op has let go of the session
# when its caller gives up, and closing needs no forced close: this bound, then the broker's
# transport (broker.main.DriverClient, 30 s), then the controller's (broker_client, 35 s).
_CAPTURE_OPEN_S = 20
CAPTURE_FRAME_S = 3           # one frame's reader, installed or stopped
CAPTURE_STOP_S = 5            # every frame's stop, within the cleanup bound
_CAPTURE_DISPOSE_S = 2
_CAPTURE_YIELD_S = 2          # a capture op told to stop lets go of the session within this
MAX_PLANNED = 200             # a look plans at most this many fields (ids f1 to f200)
# Slice 5v, the guest's waits (measured: install ~3 s, popup ~1 s, each sign-in step ~1 s,
# landing ~3.5 s). A guest op is cut at _GUEST_TIMEOUT; the broker's transport waits longer
# (broker.main.GUEST_OP_S), so the driver always answers before the broker gives up on it.
_GUEST_TIMEOUT = 45
GUEST_INSTALL_WAIT_S = 15
GUEST_READ_S = 6
GUEST_FIELD_WAIT_S = 10
GUEST_LANDING_WAIT_S = 15
_CLEANUP_TIMEOUT = 10
_SWEEP_S = 4                  # the guest's tab sweep, within the cleanup bound (5w)
SETTLE_QUIET_S = 3.0          # a settle waits at most this long for a quiet page (5u)


def _frame_kept(frame, address):
    """Whether a frame holding a credential handle is still attached and its address has the
    origin it had when the handle was issued (``address``; None for an address with no web
    origin, such as ``about:blank``)."""
    try:
        return not frame.is_detached() and _url_origin(frame.url) == address
    except Exception:
        return False


def _frame_address(frame):
    try:
        return _url_origin(frame.url)
    except Exception:
        return None


def _capture_frame(frame, origins):
    """Whether a frame's document can be of a capture origin, by its address alone: a frame
    elsewhere or an extension's own is left alone, unasked. A frame with no web origin in its
    address (``about:blank``, ``about:srcdoc``) can inherit one, so it is asked."""
    try:
        if frame.url.startswith('chrome-extension:'):
            return False
    except Exception:
        return True
    address = _frame_address(frame)
    return address is None or address in origins


class _FrameStalled(Exception):
    """A frame did not answer the capture's reader within CAPTURE_FRAME_S."""


def _serialized(method):
    """Guard direct calls as well as RPC calls, including nested driver helpers."""
    parameters = signature(method)

    @wraps(method)
    async def guarded(self, driver_session, *args, **kwargs):
        arguments = parameters.bind(self, driver_session, *args, **kwargs).arguments
        async with self._operation(driver_session, method.__name__,
                                   arguments.get('page_generation')):
            return await method(self, driver_session, *args, **kwargs)
    return guarded


# All DOM code is fixed and evaluated in Patchright's isolated world.
_DISCOVER = """e => ({type:e.type, autocomplete:e.autocomplete, connected:e.isConnected,
    invalid:e.getAttribute('aria-invalid') === 'true'})"""
_ROLE = """e => {
    // A trailing "webauthn" token marks a field the site's passkey request may fill; the field's
    // own name is the token before it ("username webauthn", slice 5v).
    const tokens=e.autocomplete.toLowerCase().split(/\\s+/).filter(Boolean);
    if (tokens.length > 1 && tokens[tokens.length-1]==='webauthn') tokens.pop();
    const a=tokens.pop() || '';
    if (a==='username' || a==='email' || e.type==='email') return 'username';
    if (a==='current-password') return 'password';
    if (a==='new-password') return 'new_password';
    if (a==='one-time-code') return 'one_time_code';
    return e.type==='password' && !a ? 'password' : null;
}"""
_SET = """(e, a) => {
    if (location.origin!==a.origin || e.ownerDocument!==document || !e.isConnected || e.disabled ||
        e.type!==a.type || !e.getClientRects().length) return false;
    const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
    set.call(e, ''); set.call(e, a.value);
    e.dispatchEvent(new Event('input',{bubbles:true}));
    e.dispatchEvent(new Event('change',{bubbles:true}));
    if (e.value!==a.value) {set.call(e,''); return false;}
    return true;
}"""


# A shown, editable box of the form that is empty and not one of ``taken``: anything a person
# could type into, whatever its type. A password never goes out beside one.
_EMPTY_BOX = """(f, taken) => [...f.elements].some(x => (x.tagName === 'TEXTAREA' ||
    (x.tagName === 'INPUT' && !['hidden', 'submit', 'button', 'image', 'reset', 'checkbox', 'radio',
                                'file', 'range', 'color'].includes(x.type))) &&
    !taken.includes(x) && x.getClientRects().length > 0 && !x.disabled && !x.readOnly &&
    x.value === '')"""
_PASSWORD_BESIDE = """(e, taken) => !!e.form && (%s)(e.form, taken)""" % _EMPTY_BOX
# Checked in the evaluation that clicks, when the step filled a password (``a.password``; the
# page cannot talk it out of that by hiding or retyping the field): a box that rendered late or
# a field a re-render wiped while the step waited stops the click.
_SUBMIT = """(e, a) => {
    if (location.origin!==a.origin || e.ownerDocument!==document || !e.isConnected ||
        e.disabled || !e.getClientRects().length || !a.form || e.form!==a.form ||
        a.form.ownerDocument!==document || !a.form.isConnected ||
        !['BUTTON','INPUT'].includes(e.tagName) || e.type!=='submit') return false;
    if (a.password && (%s)(a.form, [])) return 'empty_box';
    e.click(); return true;
}""" % _EMPTY_BOX
# A form's submit button as the browser has it. Not Patchright's is_enabled, which also reads
# aria-disabled: a measured sign-in form keeps aria-disabled=true on a button that works.
_NOT_DISABLED = "e => !e.matches(':disabled')"
_USABLE = "e => e.isConnected && !e.matches(':disabled') && e.getClientRects().length > 0"


# The guest's values (5v): like ``_SET``, and the element's document must be the very page the
# driver opened — its exact origin and path — in the same evaluation that sets the value.
_GUEST_SET = """(e, a) => {
    if (location.origin!==a.origin || location.pathname!==a.path || e.ownerDocument!==document ||
        !e.isConnected || e.disabled || e.type!==a.type || !e.getClientRects().length) return false;
    const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
    set.call(e, ''); set.call(e, a.value);
    e.dispatchEvent(new Event('input',{bubbles:true}));
    e.dispatchEvent(new Event('change',{bubbles:true}));
    if (e.value!==a.value) {set.call(e,''); return false;}
    return true;
}"""


_MARK = "m => { window.__casaCodePage = m; return window.__casaCodePage === m; }"
_MARKED = "m => window.__casaCodePage === m"

# Signing in off the start page (the look): the judged sign-in's documents carry their own
# mark, in the isolated world as the code page's does; the binding reads what a verdict covers.
_SIGN_IN_MARK = "m => { window.__casaSignIn = m; return window.__casaSignIn === m; }"
_SIGN_IN_MARKED = "m => window.__casaSignIn === m"
_LABEL = """e => {
    const t = s => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 200);
    if (e.tagName === 'INPUT' && ['submit', 'button', 'image'].includes(e.type))
        return t(e.value) || t(e.getAttribute('aria-label'));
    if (e.tagName === 'BUTTON') return t(e.innerText) || t(e.getAttribute('aria-label'));
    return t(e.getAttribute('aria-label')) || t(e.labels && e.labels[0] && e.labels[0].innerText)
        || t(e.placeholder);
}"""
# R9: the look's plan, in one document: every shown, enabled input with a sign-in role, a
# password field, or a plain text/email/telephone/search field, marked with the look (``look``)
# and a short id, its form's one submit button marked too; for each: id, role (or null), label,
# password or not, the button's label.
_PLAN = """a => {
    const roleOf = (%s), labelOf = (%s);
    const shown = e => e.getClientRects().length > 0 && !e.disabled;
    const plain = ['text', 'email', 'tel', 'search'];
    const submitOf = e => {
        const f = e.form;
        if (!f) return null;
        // A disabled button is the form's when no other can be pressed: a widget or the form's
        // own checks may be holding it.
        const bs = [...f.querySelectorAll('button:not([type]),button[type=submit],input[type=submit]')]
            .filter(b => b.form === f && b.getClientRects().length > 0);
        const usable = bs.filter(b => !b.disabled);
        const pick = usable.length ? usable : bs;
        return pick.length === 1 ? pick[0] : null;
    };
    const out = [];
    let n = a.start;
    for (const e of document.querySelectorAll('input')) {
        if (out.length >= a.max) break;
        if (!shown(e)) continue;
        const role = roleOf(e);
        if (!role && e.type !== 'password' && !plain.includes(e.type)) continue;
        const id = 'f' + (++n);
        e.__casaSignInField = {look: a.look, id};
        const b = submitOf(e);
        if (b) b.__casaSignInField = {look: a.look, id: 'button'};
        out.push({id, role, label: labelOf(e), password: e.type === 'password',
                  button: b ? labelOf(b) : null});
    }
    return out;
}""" % (_ROLE, _LABEL)
# The facts of every field and button the look marked in this document (the binding's part).
_SCAN = """m => {
    const labelOf = (%s);
    const out = [];
    for (const e of document.querySelectorAll('input,button')) {
        const k = e.__casaSignInField;
        if (!k || k.look !== m) continue;
        const f = e.form;
        out.push([k.id, e.tagName, e.type, e.getAttribute('autocomplete'), labelOf(e),
                  f ? f.action : null, f ? f.method : null,
                  e.hasAttribute('formaction') ? e.formAction : null,
                  e.hasAttribute('formmethod') ? e.formMethod : null]);
    }
    return out;
}""" % _LABEL
# The id the look gave this element, or null (another look's mark is no mark).
_PLANNED = "(e, m) => { const k = e.__casaSignInField; return k && k.look === m ? k.id : null; }"
_DOCUMENT_TEXT = "() => document.body ? document.body.innerText : ''"
# 1Password's prompt as shown: every visible notification's text (title bar, item, buttons).
_PROMPT_TEXT = """() => [...document.querySelectorAll('[data-testid=stacked-notification]')]
    .filter(e => e.getClientRects().length > 0).map(e => e.innerText).join('\\n')"""


# The popup's state, by its language-free markers (5v): the lock screen, the vault list.
_POPUP_STATE = """origin => {
    if (location.origin !== origin) return 'foreign';
    const shown = e => !!e && e.getClientRects().length > 0;
    if (shown(document.querySelector('[data-testid=lock-screen-submit-button]')) &&
        document.querySelectorAll('input[type=password]').length === 1) return 'locked';
    if (shown(document.querySelector('[data-testid=password-generator-button]'))) return 'unlocked';
    return null;
}"""

# 1Password's notification frame shows a prompt (5v).
_PROMPT_SHOWN = """() => [...document.querySelectorAll('[data-testid=stacked-notification]')]
    .some(e => e.getClientRects().length > 0)"""

# The passkey prompt's Sign in button, only when the prompt has the measured shape (one
# notification; its title bar holds the two labelled buttons "Use a security key or another
# passkey" and "Close") and offers exactly the expected item: its title, then its username.
_PASSKEY_BUTTON = """a => {
    const shown = e => e.getClientRects().length > 0;
    const notes = [...document.querySelectorAll('[data-testid=stacked-notification]')].filter(shown);
    if (notes.length !== 1) return null;
    const note = notes[0];
    const bar = note.querySelector('#notification-title-bar');
    if (!bar || bar.querySelectorAll('button[aria-label]').length !== 2) return null;
    const labels = [...note.querySelectorAll('[data-testid=item-text-label]')];
    if (labels.length !== 1) return null;
    const label = labels[0], next = label.nextElementSibling;
    if (label.textContent.trim() !== a.title || !next || next.textContent.trim() !== a.username)
        return null;
    const buttons = [...note.querySelectorAll('button:not([aria-label])')]
        .filter(b => !bar.contains(b) && !b.disabled && shown(b));
    return buttons.length === 1 ? buttons[0] : null;
}"""


@dataclass
class DriverServer(UnixServer):
    manager: object
    path: Path
    allowed_uid: int = 1102
    cleanup_timeout: float | None = None
    browser_close_timeout: float = 10
    _sessions: dict = field(default_factory=dict, init=False, repr=False)
    _connections: set = field(default_factory=set, init=False, repr=False)
    _readings: dict = field(default_factory=dict, init=False, repr=False)
    # The look's bound documents' frames, per (run, site): (page, frames), from its last read.
    _sign_in_frames: dict = field(default_factory=dict, init=False, repr=False)

    async def handle(self, reader, writer):
        await self.serve(reader, writer, self.dispatch)

    def _session(self, sid, generation=None, *, capture=False):
        s = self._sessions.get(sid)
        if (s is None or s.closed or s.aborted or self.manager.session is not s.browser or
                s.browser.generation != s.generation or
                (not capture and (s.page.is_closed() or s.browser.page is not s.page)) or
                # Slice 5u: a step's approval (the handles its read issued, until its submit
                # click) holds for every op: once a frame of it left its origin or the page,
                # even if it came back, the sign-in ends, whatever generation the op names.
                s.departed):
            raise BrokerError('refused')
        if generation is not None and generation != s.browser.view_generation:
            # Only the page moved (a route change, another frame's navigation): the broker
            # settles, reads the page again and goes on.
            raise BrokerError('page_changed')
        return s

    @asynccontextmanager
    async def _operation(self, sid, op, generation=None):
        s = self._sessions.get(sid)
        if s is None:
            raise BrokerError('refused')
        task = asyncio.current_task()
        nested = s.owner is task
        if not nested:
            await s.lock.acquire()
            s.owner = task
        try:
            # Validation belongs inside the lock; queued callers may have been closed.
            self._session(sid, generation, capture=s.kind == 'capture')
            kind = 'login' if op == 'replace_login_lease' else DRIVER_KINDS[op]
            # These document readers also serve direct guest-session callers.
            kinds = ('login', 'guest') if op in ('settle', 'find_login_fields') else (kind,)
            if s.kind not in kinds or (op in ('fill', 'submit') and s.lease is None):
                raise BrokerError('refused')
            bound = (_GUEST_TIMEOUT if kind == 'guest' else
                     _CAPTURE_OPEN_S if op == 'begin_live_capture' else _OPERATION_TIMEOUT)
            async with asyncio.timeout(bound):
                yield s
            if not (op == 'take_capture' and s.closed):
                # A click that may move the page (a submit, a passkey's Sign in) is not judged
                # by the page it led to.
                self._session(sid, None if op in ('submit', 'choose_passkey') else generation,
                              capture=s.kind == 'capture')
        except TimeoutError:
            raise BrokerError('timeout') from None
        finally:
            if not nested:
                s.owner = None
                s.lock.release()

    async def open_session(self, kind, run_id, site_id, browser_session, proxy_client,
                           login_origins_controller, reader=None):
        if (kind not in ('login', 'guest', 'capture') or self._sessions or
                self.manager.session is not browser_session or browser_session.kind != 'account' or
                browser_session.site_id != site_id or browser_session.page is None):
            raise BrokerError('refused')
        sid = secrets.token_urlsafe(24)
        s = _Session(kind, run_id, site_id, browser_session, browser_session.page, proxy_client,
                     frozenset(login_origins_controller), browser_session.generation,
                     reader=reader if kind == 'capture' else None)
        self._sessions[sid] = s
        try:
            if kind in ('login', 'guest'):
                browser_session.login_active = True
            if kind == 'login':
                self._watch(s)
                await self.replace_login_lease(sid)
            if kind == 'guest':
                s.tabs = await self._chrome_tabs(s)
            return sid
        except BaseException:
            await self.close_session(sid)
            raise BrokerError('unavailable') from None

    @staticmethod
    def _watch(s):
        """Slice 5u: every navigation or detach of a frame that holds a live handle is seen as
        it happens; one that leaves the address origin the handle was issued at marks the
        session departed for good (a later return does not undo it)."""
        def changed(frame):
            if any(entry[1] is frame and not _frame_kept(frame, s.addresses.get(handle))
                   for handle, entry in s.handles.items()):
                s.departed = True
        s.watch = changed
        s.page.on('framenavigated', changed)
        s.page.on('framedetached', changed)

    @staticmethod
    def _unwatch(s):
        if s.watch is not None:
            for event in ('framenavigated', 'framedetached'):
                try:
                    s.page.remove_listener(event, s.watch)
                except Exception:
                    pass
            s.watch = None

    @_serialized
    async def replace_login_lease(self, driver_session):
        s = self._session(driver_session)
        lease = Lease(secrets.token_urlsafe(24), s.run_id, s.site_id, s.generation, 'login')
        # Own the lease before awaiting: a lost acknowledgement still needs cleanup.
        s.lease = lease
        try:
            s.token = await asyncio.wait_for(s.proxy.begin_login(lease), 10)
            self._session(driver_session)
        except Exception:
            if s.aborted:
                raise BrokerError('refused') from None
            await asyncio.wait_for(s.proxy.revoke(s.run_id), 10)
            raise BrokerError('unavailable') from None
        s.submitted = False

    async def _cleanup(self, sid, s):
        s.closed = True
        released = s.lease is None
        try:
            if s.lease is not None:
                try:
                    await asyncio.wait_for(s.proxy.end_login(s.lease.lease_id), 10)
                except Exception:
                    if s.aborted:
                        raise BrokerError('refused') from None
                    await asyncio.wait_for(s.proxy.revoke(s.run_id), 10)
                if s.aborted:
                    raise BrokerError('refused')
                released = True
            if s.capture_id is not None:
                await self._stop_capture(s)
            if s.aborted:
                raise BrokerError('refused')
            if s.guest_page is not None and not s.guest_page.is_closed():
                await s.guest_page.close()
            if s.aborted:
                raise BrokerError('refused')
            if s.tabs is not None:
                await self._close_guest_tabs(s)
            if s.aborted:
                raise BrokerError('refused')
            if s.token is not None:
                await asyncio.wait_for(s.proxy.close_window(s.token), 10)
        except Exception:
            if s.aborted:
                return
            try:
                await asyncio.wait_for(s.proxy.revoke(s.run_id), 10)
                released = True
            except Exception:
                pass
        finally:
            self._unwatch(s)
            if not s.aborted:
                if released:
                    s.browser.login_active = False
                s.handles.clear()
                if s.buffer is not None:
                    s.buffer.take()
                self._sessions.pop(sid, None)

    async def _force_cleanup(self, sid, s):
        # Cancelling an await cannot retract an evaluation already queued in Chrome.
        log.warning("a %s session could not be closed in time: Chrome is closed by force", s.kind)
        s.aborted = True
        s.closed = False
        if s.owner is not None:
            s.owner.cancel()
        for task in tuple(s.tasks):
            task.cancel()
        if self.manager.session is s.browser:
            closing = self.manager.close(session=s.browser, force=True)
        elif self.manager.session is None:
            # The session's browser is already gone from the manager (its controller closed it
            # after an unconfirmed close): the launcher's proof that no Chrome of the browser
            # user runs is the proof this close needs.
            closing = self.manager.close(force=True)
        else:
            raise BrokerError('unavailable')  # another browser runs: keep everything (fail closed)
        close = asyncio.create_task(closing)
        try:
            done, _ = await asyncio.wait((close,), timeout=self.browser_close_timeout)
            if not done:
                close.cancel()
                close.add_done_callback(self._consume_task)
                raise BrokerError('unavailable')
            await close
        except Exception:
            # Retain the lease, window, session and observation guard without exit proof.
            raise BrokerError('unavailable') from None
        await asyncio.wait_for(s.proxy.revoke(s.run_id), 10)
        self._unwatch(s)
        s.closed = True
        s.browser.login_active = False
        s.handles.clear()
        if s.buffer is not None:
            s.buffer.take()
        self._sessions.pop(sid, None)

    async def _drain_and_cleanup(self, sid, s):
        if s.aborted:
            await self._force_cleanup(sid, s)
            return
        bound = _CLEANUP_TIMEOUT if self.cleanup_timeout is None else self.cleanup_timeout
        if s.kind == 'capture':
            # The reader's queued work in the background ends here and none starts (cleanup_task);
            # the one in hand finishes, within its frame's bound.
            for task in tuple(s.tasks):
                if task is not s.owner:
                    task.cancel()
        try:
            await asyncio.wait_for(s.lock.acquire(), bound)
        except TimeoutError:
            if s.kind != 'capture' or not await self._capture_let_go(s):
                await self._force_cleanup(sid, s)
                return
        cleanup = asyncio.create_task(self._cleanup(sid, s))
        try:
            done, _ = await asyncio.wait((cleanup,), timeout=bound)
            if done:
                await cleanup
            else:
                # Unlike wait_for, this bound does not await cancellation cooperation.
                s.aborted = True
                cleanup.cancel()
                cleanup.add_done_callback(self._consume_task)
                await self._force_cleanup(sid, s)
        finally:
            s.lock.release()

    @staticmethod
    async def _capture_let_go(s):
        """A capture op that holds the session past the bound is stopped instead of Chrome
        being closed under it: a capture has no credential effect queued in Chrome, and what
        Chrome still answers no longer matters to it. True once it let go; the lock is held."""
        for task in (*s.tasks, *((s.owner,) if s.owner is not None else ())):
            task.cancel()
        try:
            await asyncio.wait_for(s.lock.acquire(), _CAPTURE_YIELD_S)
            return True
        except TimeoutError:
            return False

    @staticmethod
    def _consume_task(task):
        if not task.cancelled():
            task.exception()

    async def close_session(self, driver_session):
        s = self._sessions.get(driver_session)
        if s is None:
            return
        if s.cleanup_task is None:
            s.cleanup_task = asyncio.create_task(self._drain_and_cleanup(driver_session, s))
        cleanup = s.cleanup_task
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
        finally:
            if cleanup.done() and self._sessions.get(driver_session) is s and s.cleanup_task is cleanup:
                s.cleanup_task = None

    def open_sessions(self) -> tuple[str, ...]:
        """Every session not yet retired (the run controller retires them after the launcher's
        exit proof, so none outlives its run)."""
        return tuple(self._sessions)

    async def close(self):
        for sid in tuple(self._sessions):
            await self.close_session(sid)
        await super().close()

    async def dispatch(self, request):
        try:
            if (not isinstance(request, dict) or not isinstance(request.get('op'), str) or
                    request['op'] not in DRIVER_OPS):
                raise BrokerError('unsupported')
            op = request['op']
            keys = DRIVER_OPS[op]
            optional = {'login_origins'} | ({'sign_in_look', 'sign_in_fields'}
                                            if op == 'find_login_fields' else set()) \
                if op in ('settle', 'find_login_fields', 'fill', 'submit',
                                                   'mark_document', 'document_marked',
                                                   'choose_passkey', 'read_sign_in',
                                                   'sign_in_bound', 'submit_usable') else set()
            if set(request) - optional != {'op', *keys} or any(
                    not isinstance(request[k], str) for k in keys
                    if k not in ('page_generation', 'timeout_s')):
                raise BrokerError('unsupported')
            if 'page_generation' in keys and (type(request['page_generation']) is not int):
                raise BrokerError('unsupported')
            if 'timeout_s' in keys and (type(request['timeout_s']) not in (int, float) or
                                       not 0 < request['timeout_s'] <= 30):
                raise BrokerError('unsupported')
            if 'login_origins' in request:
                origins = request['login_origins']
                if not isinstance(origins, list) or not all(isinstance(o, str) for o in origins):
                    raise BrokerError('unsupported')
            mapping = None
            if {'sign_in_look', 'sign_in_fields'} & set(request):
                look, ids = request.get('sign_in_look'), request.get('sign_in_fields')
                if (op != 'find_login_fields' or not isinstance(look, str)
                        or not isinstance(ids, dict) or not all(
                            isinstance(k, str) and v in ('username', 'password')
                            for k, v in ids.items())):
                    raise BrokerError('unsupported')
                mapping = (look, dict(ids))

            handlers = {name: method for name, method in (
                ('read_sign_in', self.read_sign_in), ('sign_in_bound', self.sign_in_bound),
                ('find_login_fields', self.find_login_fields), ('fill', self.fill),
                ('submit', self.submit), ('settle', self.settle),
                ('choose_passkey', self.choose_passkey), ('guest_state', self.guest_state),
                ('mark_document', self.mark_document), ('document_marked', self.document_marked),
                ('guest_sign_in', self.guest_sign_in), ('guest_unlock', self.guest_unlock),
                ('begin_live_capture', self.begin_live_capture), ('take_capture', self.take_capture),
                ('submit_usable', self.submit_usable))}
            async with self._operation(request['driver_session'], op,
                                       request.get('page_generation')) as s:
                if s.kind != DRIVER_KINDS[op]:
                    raise BrokerError('refused')
                if 'login_origins' in request:
                    s.broker_origins = frozenset(origins)
                s.mapping = mapping
                reply = await handlers[op](**{k: request[k] for k in keys})
                return asdict(reply) if isinstance(reply, (LoginFields, CapturedFields)) else reply
        except BrokerError as error:
            return {'error': error.code}
        except TimeoutError:
            return {'error': 'timeout'}
        except Exception:
            return {'error': 'internal'}

    @staticmethod
    def _login_origins(s):
        return s.origins if s.broker_origins is None else s.origins & s.broker_origins

    async def _checked(self, sid, effect, generation=None, *, capture=False):
        # A cancelled browser await may still return; never chain another effect
        # from its result after forced cleanup has invalidated the session.
        result = await effect
        self._session(sid, generation, capture=capture)
        return result

    async def _origin(self, sid, frame, generation=None, *, capture=False):
        self._session(sid, generation, capture=capture)
        origin = await frame.evaluate('() => location.origin')
        self._session(sid, generation, capture=capture)
        return origin

    async def _navigation_read(self, sid, read, *, generation=None, pages=None, timeout_s=10):
        try:
            async with asyncio.timeout(timeout_s):
                for attempt in range(3):
                    s = self._session(sid, generation)
                    try:
                        result = await read()
                        self._session(sid, generation)
                        return result
                    except BrokerError:
                        raise
                    except Exception as error:
                        if 'Execution context was destroyed' not in str(error):
                            raise
                    self._session(sid, generation)
                    if attempt == 2:
                        raise BrokerError('timeout')
                    for page in (pages() if pages else (s.page,)):
                        try:
                            await self._checked(
                                sid, page.wait_for_load_state('domcontentloaded', timeout=timeout_s * 1000),
                                generation)
                        except Exception:
                            self._session(sid, generation)
                            raise BrokerError('timeout') from None
        except TimeoutError:
            raise BrokerError('timeout') from None

    @_serialized
    async def find_login_fields(self, driver_session, page_generation):
        return await self._navigation_read(
            driver_session, lambda: self._find_login_fields(driver_session, page_generation),
            generation=page_generation)

    async def _find_login_fields(self, driver_session, page_generation):
        s = self._session(driver_session, page_generation)
        s.handles.clear()
        s.addresses.clear()
        candidates = {}
        forms = []
        field_error = False
        origin_ok = False
        passkey = False
        hint = False
        for frame in s.page.frames:
            if frame.url.startswith(EXTENSION_ORIGIN + '/'):
                # 1Password's own frames are never a login origin; only its prompt is read (5v).
                passkey = passkey or await self._passkey_prompt(
                    driver_session, s, frame, page_generation)
                continue
            origin = await self._origin(driver_session, frame, page_generation)
            if origin not in self._login_origins(s):
                continue
            origin_ok = True
            # R9: a look's sign-in in a document the look marked (the judged one)
            judged = s.mapping is not None and await self._checked(
                driver_session, frame.evaluate(_SIGN_IN_MARKED, s.mapping[0]),
                page_generation) is True
            for element in await self._checked(
                    driver_session, frame.query_selector_all('input'), page_generation):
                if (not await self._checked(driver_session, element.is_visible(), page_generation) or
                        not await self._checked(driver_session, element.is_enabled(), page_generation)):
                    continue
                info = await self._checked(driver_session, element.evaluate(_DISCOVER), page_generation)
                tokens = str(info['autocomplete'] or '').lower().split()
                hint = hint or (bool(tokens) and tokens[-1] == 'webauthn')
                role = await self._checked(driver_session, element.evaluate(_ROLE), page_generation)
                if judged:
                    # R9, the judged document: only fields of the judged reading (the look's
                    # plan); a role the check named goes to its named field alone (a password
                    # only into a password field); a role it did not name stays with the field
                    # the page marks with it.
                    look, ids = s.mapping
                    fid = await self._checked(
                        driver_session, element.evaluate(_PLANNED, look), page_generation)
                    if fid is None:
                        continue
                    wanted = ids.get(fid)
                    if wanted is not None:
                        role = wanted if wanted == (
                            'password' if info['type'] == 'password' else 'username') else None
                    elif role in ids.values():
                        role = None
                if s.mapping is not None and role == 'password' and info['type'] != 'password':
                    role = None          # R9: a look's password goes only into a password field
                if role is None:
                    continue
                field_error |= info['invalid']
                candidates.setdefault(role, []).append((element, frame, info['type'], origin))
                form = await self._checked(
                    driver_session, element.evaluate_handle('e => e.form'), page_generation)
                forms.append((frame, form))
        roles = {}
        ambiguous = False
        for role, elements in candidates.items():
            if role == 'new_password' and len(elements) == 2:
                groups = [('new_password', elements[0]), ('confirm_password', elements[1])]
            elif len(elements) == 1:
                groups = [(role, elements[0])]
            else:
                ambiguous = True
                groups = [(role, elements[0])]
            for name, entry in groups:
                handle = secrets.token_urlsafe(24)
                s.addresses[handle] = _frame_address(entry[1])
                s.handles[handle] = (*entry, page_generation, name)
                roles[name] = handle
        buttons, held = [], []   # the forms' submit buttons, enabled and disabled
        for frame, form in forms:
            alert = await self._checked(driver_session, form.evaluate(
                "f => !!f && [...f.querySelectorAll('[role=alert]')].some(e=>e.getClientRects().length)"),
                page_generation)
            field_error |= alert
            controls = await self._checked(driver_session, form.evaluate_handle(
                "f => f ? [...f.querySelectorAll('button:not([type]),button[type=submit],input[type=submit]')].filter(e=>e.form===f) : []"),
                page_generation)
            for prop in (await self._checked(
                    driver_session, controls.get_properties(), page_generation)).values():
                button = prop.as_element()
                if (button is None or
                        not await self._checked(driver_session, button.is_visible(), page_generation)):
                    continue
                enabled = await self._checked(
                    driver_session, button.evaluate(_NOT_DISABLED), page_generation)
                duplicate = False
                for old in (*buttons, *held):
                    if old[1] is not frame:
                        continue
                    duplicate |= await self._checked(driver_session, button.evaluate(
                        '(e, other) => e===other', old[0]), page_generation)
                if duplicate:
                    continue
                (buttons if enabled else held).append((button, frame, form))
        # A disabled button is the form's only when no other can be pressed.
        chosen = buttons or held
        empty_box = False
        if 'password' in roles:
            element, frame = candidates['password'][0][:2]
            taken = [entry[0] for group in candidates.values() for entry in group
                     if entry[1] is frame]
            empty_box = await self._checked(driver_session, element.evaluate(
                _PASSWORD_BESIDE, taken), page_generation) is True
        submit = None
        # The button's form must be the form of every field the step fills: another form's
        # button would send only part of them.
        one_form = len(chosen) == 1 and not ambiguous
        if one_form:
            element, frame, form = chosen[0]
            for other_frame, other in forms:
                one_form = one_form and other_frame is frame and await self._checked(
                    driver_session, form.evaluate('(f, o) => f === o', other),
                    page_generation) is not False
        if one_form:
            origin = await self._origin(driver_session, frame, page_generation)
            submit = secrets.token_urlsafe(24)
            s.addresses[submit] = _frame_address(frame)
            s.handles[submit] = (element, frame, form, origin, page_generation, 'submit')
        if roles and s.submitted:
            await self.replace_login_lease(driver_session)
        self._session(driver_session, page_generation)
        # A field-free landing on the account origin is a valid completion.
        on_site = await self._on_site(s, driver_session)
        return dict(page_generation=page_generation, origin_ok=origin_ok or (not candidates and on_site),
                    roles=roles, submit=submit, field_error=bool(field_error), passkey_frame=passkey,
                    passkey_hint=hint, submit_ready=bool(buttons), empty_box=empty_box)

    async def _on_site(self, s, sid):
        origins = s.origins | frozenset(await self._site_origins(s))
        generation = s.browser.view_generation
        origin = await self._navigation_read(
            sid, lambda: self._origin(sid, s.page.main_frame, generation), generation=generation)
        return origin in origins

    async def _site_origins(self, s):
        # Status exposes only the active site's registered account origins.
        status = await s.proxy.status()
        if s.closed:
            raise BrokerError('refused')
        if status.get('run_id') != s.run_id or status.get('site_id') != s.site_id:
            raise BrokerError('refused')
        return tuple(status.get('site_origins', ()))

    async def _handle(self, sid, generation, handle, role):
        s = self._session(sid, generation)
        entry = s.handles.get(handle)
        if entry is None or entry[4] != generation or (entry[5] == 'submit') != (role == 'submit'):
            raise BrokerError('refused')
        element, frame, types, origin, _, _ = entry
        if await self._origin(sid, frame, generation) != origin or origin not in self._login_origins(s):
            raise BrokerError('refused')
        if (not await self._checked(sid, element.is_visible(), generation) or
                not await self._checked(sid, element.evaluate(_NOT_DISABLED) if role == 'submit'
                                        else element.is_enabled(), generation)):
            await self._still_there(sid, element, generation)
            raise BrokerError('refused')
        return s, entry

    async def _still_there(self, sid, element, generation):
        """Asked once a check or an action on a step's element failed: an element the page
        re-rendered away (no navigation) is a page that changed under the step, which the
        sign-in settles and reads again (``page_changed``); otherwise the refusal stands."""
        if await self._checked(sid, element.evaluate('e => e.isConnected'), generation) is False:
            raise BrokerError('page_changed')

    @_serialized
    async def fill(self, driver_session, page_generation, field_handle, value):
        s, (element, frame, types, origin, _, role) = await self._handle(
            driver_session, page_generation, field_handle, 'field')
        if role in ('new_password', 'confirm_password'):
            raise BrokerError('refused')
        await self._origin(driver_session, frame, page_generation)
        ok = await self._checked(driver_session, element.evaluate(
            _SET, dict(origin=origin, type=types, value=value)), page_generation)
        if not ok:
            await self._still_there(driver_session, element, page_generation)
            raise BrokerError('refused')
        return {'ok': True}

    @_serialized
    async def submit(self, driver_session, page_generation, submit_handle):
        s, entry = await self._handle(driver_session, page_generation, submit_handle, 'submit')
        if s.lease is None:
            raise BrokerError('refused')
        await asyncio.wait_for(s.proxy.arm_login_close(s.lease.lease_id), 10)
        # Armed: the proxy may close this lease at the next document. A submit that stops here
        # (the page changed) counts as one, so the next form found takes a fresh lease (5u).
        s.submitted = True
        s, entry = await self._handle(driver_session, page_generation, submit_handle, 'submit')
        element, frame, form, origin, _, _ = entry
        await self._origin(driver_session, frame, page_generation)
        password = any(entry[5] == 'password' for entry in s.handles.values())
        s.handles.clear()       # the step's approval ends at its click: the landing may move it
        clicked = await element.evaluate(_SUBMIT, dict(origin=origin, form=form, password=password))
        self._session(driver_session)
        if clicked == 'empty_box':
            return {'ok': False, 'empty_box': True}
        if not clicked:
            await self._still_there(driver_session, element, page_generation)
            raise BrokerError('refused')
        return {'ok': True}

    @_serialized
    async def submit_usable(self, driver_session, page_generation, submit_handle):
        """Whether the step's submit button can be pressed now (one read, nothing done). One
        the page re-rendered away is a page that changed under the step."""
        s = self._session(driver_session, page_generation)
        entry = s.handles.get(submit_handle)
        if entry is None or entry[5] != 'submit':
            raise BrokerError('refused')
        usable = await self._checked(driver_session, entry[0].evaluate(_USABLE), page_generation)
        if usable is not True:
            await self._still_there(driver_session, entry[0], page_generation)
        return {'ok': usable is True}

    @_serialized
    async def settle(self, driver_session, timeout_s):
        return await self._navigation_read(
            driver_session, lambda: self._settle(driver_session, timeout_s), timeout_s=timeout_s)

    async def _settle(self, driver_session, timeout_s):
        s = self._session(driver_session)
        try:
            await self._checked(driver_session, s.page.wait_for_load_state(
                'domcontentloaded', timeout=timeout_s * 1000))
        except Exception:
            self._session(driver_session)
            raise BrokerError('timeout') from None
        # Slice 5u: settled once no frame of the page navigated for a quarter second (a sign-in
        # page's own route changes and subframes come in bursts); the wait — its load waits
        # included — lasts at most SETTLE_QUIET_S, then the page is read as it is.
        loop = asyncio.get_running_loop()
        quiet_by = loop.time() + SETTLE_QUIET_S
        while True:
            seen = s.browser.view_generation
            await asyncio.sleep(min(0.25, max(0.0, quiet_by - loop.time())))
            self._session(driver_session)
            left = quiet_by - loop.time()
            if s.browser.view_generation == seen or left <= 0:
                break
            try:
                await self._checked(driver_session, s.page.wait_for_load_state(
                    'domcontentloaded', timeout=left * 1000))
            except Exception:
                self._session(driver_session)
                break
        site_origins = await self._site_origins(s)
        generation = s.browser.view_generation
        origin = await self._origin(driver_session, s.page.main_frame, generation)
        return dict(page_generation=generation, login_origins_required=True,
                    on_site_origin=origin in site_origins,
                    on_login_origin=origin in self._login_origins(s))

    # Slice 6 (S6-2, S6-3): the code page that asked for a code the site sent is marked in its
    # own document, in Patchright's isolated world (which the page's scripts cannot see or set);
    # another tab, a reload or a navigation is another document and holds no mark.
    @_serialized
    async def mark_document(self, driver_session, page_generation, mark):
        s = self._session(driver_session, page_generation)
        ok = await self._checked(driver_session, s.page.main_frame.evaluate(_MARK, mark),
                                 page_generation)
        return {'ok': ok is True}

    @_serialized
    async def document_marked(self, driver_session, page_generation, mark):
        s = self._session(driver_session, page_generation)
        ok = await self._checked(driver_session, s.page.main_frame.evaluate(_MARKED, mark),
                                 page_generation)
        return {'ok': ok is True}

    # -- signing in off the start page: the look's read and the verdict's binding ------------

    @_serialized
    async def read_sign_in(self, driver_session, page_generation, mark):
        """The look's one read (nothing filled, no vault). The fields a sign-in there could use
        — every shown input of a login origin with a sign-in role, a password field, or a plain
        text, email, telephone or search field (R9: the check maps the plain ones) — each with
        a short id (``f1``, …), its role as the page marks it (or None), its label, whether it
        is a password field and the label of its form's one submit button. Each such field and
        that button carry ``mark`` in Patchright's isolated world. The binding of what the
        check judges: ``mark`` in every credential-bearing frame document (the main one, each
        frame holding a field or the submit, 1Password's notification frame while it shows a
        prompt) and one digest of the marked fields' and buttons' facts, each bound document's
        text and the prompt's text (``binding``). Ids, roles and labels only: never a handle,
        never a value."""
        fields = await self.find_login_fields(driver_session, page_generation)
        s = self._session(driver_session, page_generation)
        planned, holders = [], []
        for frame in s.page.frames:
            if frame.url.startswith(EXTENSION_ORIGIN + '/') or len(planned) >= MAX_PLANNED:
                continue
            if await self._origin(driver_session, frame, page_generation) not in \
                    self._login_origins(s):
                continue
            found = await self._checked(driver_session, frame.evaluate(
                _PLAN, dict(look=mark, start=len(planned), max=MAX_PLANNED - len(planned))),
                page_generation)
            if found:
                planned += found
                holders.append(frame)
        main = s.page.main_frame
        frames = [f for f in s.page.frames if f is main or any(f is h for h in holders)
                  or any(entry[1] is f for entry in s.handles.values())]
        if fields['passkey_frame']:
            frames += [f for f in s.page.frames if f.url.startswith(EXTENSION_NOTIFICATION)
                       and f.parent_frame is main]
        for frame in frames:
            if await self._checked(driver_session, frame.evaluate(_SIGN_IN_MARK, mark),
                                   page_generation) is not True:
                raise BrokerError('refused')
        self._sign_in_frames[(s.run_id, s.site_id)] = (s.page, tuple(frames))
        binding = await self._sign_in_binding(driver_session, page_generation, frames, mark)
        return dict(page_generation=page_generation, origin_ok=fields['origin_ok'],
                    roles=sorted(fields['roles']),
                    fields=[{'id': f['id'], 'role': f['role'], 'label': f['label'],
                             'password': f['password'] is True, 'button': f['button']}
                            for f in planned],
                    passkey_frame=fields['passkey_frame'], passkey_hint=fields['passkey_hint'],
                    binding=binding)

    @_serialized
    async def sign_in_bound(self, driver_session, page_generation, mark):
        """Whether the verdict still covers the page, asked just before a fill, a submit or a
        passkey's Sign in of the judged step: every bound frame document still holds ``mark``,
        every field and the submit of the step's read is one the look marked (so in a bound
        document of the judged reading), no 1Password prompt the look did not bind is shown,
        and the binding reads as it did at the look."""
        s = self._session(driver_session, page_generation)
        kept = self._sign_in_frames.get((s.run_id, s.site_id))
        if kept is None or kept[0] is not s.page:
            return {'ok': False}
        frames = list(kept[1])
        for frame in frames:
            try:
                marked = not frame.is_detached() and await frame.evaluate(_SIGN_IN_MARKED, mark)
            except Exception:
                marked = False
            self._session(driver_session, page_generation)
            if marked is not True:
                return {'ok': False}
        for handle, entry in s.handles.items():
            if not any(entry[1] is f for f in frames) or await self._checked(
                    driver_session, entry[0].evaluate(_PLANNED, mark), page_generation) is None:
                return {'ok': False}             # a field or the submit the look did not judge
        for frame in s.page.frames:
            if (frame.url.startswith(EXTENSION_NOTIFICATION)
                    and not any(frame is f for f in frames)
                    and await self._passkey_prompt(driver_session, s, frame, page_generation)):
                return {'ok': False}             # a passkey prompt the look did not judge
        return {'ok': True, 'binding': await self._sign_in_binding(
            driver_session, page_generation, frames, mark)}

    def sign_in_frames(self, run_id, site_id):
        """The frames of the look's bound documents (the controller reads only these for its
        final comparison), or () when none was read for this run and site."""
        kept = self._sign_in_frames.get((run_id, site_id))
        return kept[1] if kept is not None else ()

    async def _sign_in_binding(self, sid, generation, frames, mark):
        """One digest of what a verdict covers: in each bound site document, the facts of every
        field and button the look marked (its id, tag, type, autocomplete, label, its form's
        destination, a button's own formaction/formmethod) and the document's text; in
        1Password's notification frame, its prompt's text."""
        parts = []
        for frame in frames:
            prompt = frame.url.startswith(EXTENSION_NOTIFICATION)
            text = await self._checked(sid, frame.evaluate(
                _PROMPT_TEXT if prompt else _DOCUMENT_TEXT), generation)
            facts = [] if prompt else await self._checked(sid, frame.evaluate(_SCAN, mark),
                                                         generation)
            parts.append([prompt, hashlib.sha256(str(text).encode('utf-8')).hexdigest(), facts])
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()

    @_serialized
    async def choose_passkey(self, driver_session, page_generation, title, username):
        """Slice 5v: Sign in on 1Password's own passkey prompt (its notification frame, a child of
        this login page's main frame) when the main frame is on a login origin and the prompt
        offers exactly the site's linked item — its title and username — and nothing else. A
        trusted click; anything else is ``{ok: false}`` and nothing is clicked."""
        s = self._session(driver_session, page_generation)
        if not title or not username:
            return {'ok': False}
        main = s.page.main_frame
        if await self._origin(driver_session, main, page_generation) not in self._login_origins(s):
            return {'ok': False}
        frames = [f for f in s.page.frames
                  if f.url.startswith(EXTENSION_NOTIFICATION) and f.parent_frame is main]
        if len(frames) != 1:
            return {'ok': False}
        # 1Password re-renders or replaces its frame at will (measured live): a read or the
        # click failing under that is a page change — the broker reads the page again, its one
        # try spent (the click may have been made). Session refusals stay what they are.
        try:
            if await self._origin(driver_session, frames[0], page_generation) != EXTENSION_ORIGIN:
                return {'ok': False}
            found = await self._checked(driver_session, frames[0].evaluate_handle(
                _PASSKEY_BUTTON, dict(title=title, username=username)), page_generation)
            button = found.as_element()
            if button is None:
                return {'ok': False}
            # Not judged by the page it led to: a successful assertion navigates (as submit).
            await button.click(timeout=5000)
        except BrokerError:
            raise
        except Exception:
            self._session(driver_session)
            raise BrokerError('page_changed') from None
        self._session(driver_session)
        return {'ok': True}

    async def _passkey_prompt(self, sid, s, frame, generation):
        """Whether ``frame`` is 1Password's notification frame of this page showing a prompt."""
        if not (frame.url.startswith(EXTENSION_NOTIFICATION)
                and frame.parent_frame is s.page.main_frame):
            return False
        try:
            shown = await frame.evaluate(_PROMPT_SHOWN)
        except Exception:
            shown = False                    # the extension re-rendered or removed its frame
        self._session(sid, generation)
        return shown is True

    # -- the 1Password guest (slice 5v, measured on the real extension) -----------------------

    async def _guest_tab(self, sid, s, url, *, fresh=False):
        """The guest session's own tab (opened on first need, closed with the session), at
        ``url``; ``fresh``: a new tab (the signed-out popup may close its own tab, measured on
        the production Chrome). None without the note of Chrome's tabs at the session's start
        (5w): a tab opened then could not be told apart at its end, so none is opened."""
        if s.tabs is None:
            raise BrokerError('unavailable')
        if fresh and s.guest_page is not None and not s.guest_page.is_closed():
            await self._checked(sid, s.guest_page.close())
        if fresh or s.guest_page is None or s.guest_page.is_closed():
            s.guest_page = await self._checked(sid, s.browser.context.new_page())
        await self._checked(sid, s.guest_page.goto(url, wait_until='domcontentloaded'))
        return s.guest_page

    @staticmethod
    async def _chrome_tabs(s):
        """Every tab of Chrome by its target id, read through Chrome itself (5w): Patchright
        does not list a tab once Chrome refuses it the debugger (an extension's own pages,
        measured on Chrome 156) nor the tabs the extension opens. None: not readable."""
        cdp = None
        try:
            async with asyncio.timeout(4):
                cdp = await s.browser.context.new_cdp_session(s.page)
                targets = await cdp.send('Target.getTargets')
                return frozenset(t['targetId'] for t in targets['targetInfos']
                                 if t['type'] == 'page')
        except Exception:
            return None
        finally:
            if cdp is not None:
                try:
                    await asyncio.wait_for(cdp.detach(), 1)
                except Exception:
                    pass

    async def _close_guest_tabs(self, s):
        """Best effort (5w): every tab opened while the guest session ran — the driver's own,
        one Patchright lost, one the extension opened — is closed, and the site's page is
        brought to the front, so the run and its live view go on on the site. A read of
        Chrome's tabs that fails is tried again within the bound (it shares the session's
        cleanup bound, _CLEANUP_TIMEOUT, with the window's close)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + _SWEEP_S
        while True:
            try:
                async with asyncio.timeout(max(end - loop.time(), 0.1)):
                    await self._close_tabs_once(s)
                break
            except Exception:
                if loop.time() >= end:
                    break
                await asyncio.sleep(0.25)
        try:
            await asyncio.wait_for(s.page.bring_to_front(), 1)
        except Exception:
            pass

    @staticmethod
    async def _close_tabs_once(s):
        """One pass: close every page target not noted at the start, then read again; one left
        is a failed pass (tried again)."""
        cdp = await s.browser.context.new_cdp_session(s.page)
        try:
            for check in (False, True):
                targets = await cdp.send('Target.getTargets')
                opened = [t['targetId'] for t in targets['targetInfos']
                          if t['type'] == 'page' and t['targetId'] not in s.tabs]
                if check and opened:
                    raise RuntimeError('tabs left')
                for target_id in opened:
                    try:
                        await cdp.send('Target.closeTarget', {'targetId': target_id})
                    except Exception:
                        pass                 # gone already, or left: the check reads again
        finally:
            try:
                await asyncio.wait_for(cdp.detach(), 1)
            except Exception:
                pass

    async def _popup_state(self, sid, s, *, reload=True):
        """``locked`` (the lock screen), ``unlocked`` (the vault list), ``signed_out`` (the popup
        loaded and shows neither: a signed-out extension renders nothing) or ``unknown`` (the
        extension is not installed yet after the install wait)."""
        loop = asyncio.get_running_loop()
        page = s.guest_page
        if reload or page is None or page.is_closed() or page.url != EXTENSION_POPUP:
            deadline = loop.time() + GUEST_INSTALL_WAIT_S
            while True:
                try:
                    page = await self._guest_tab(sid, s, EXTENSION_POPUP)
                    break
                except BrokerError:
                    raise
                except Exception:
                    # Not installed yet (net::ERR_BLOCKED_BY_CLIENT): a fresh tab, later.
                    if s.guest_page is not None and not s.guest_page.is_closed():
                        await self._checked(sid, s.guest_page.close())
                    s.guest_page = None
                    if loop.time() >= deadline:
                        return 'unknown'
                    await asyncio.sleep(2)
        end = loop.time() + GUEST_READ_S
        while True:
            try:
                state = await self._checked(sid, page.main_frame.evaluate(
                    _POPUP_STATE, EXTENSION_ORIGIN))
            except BrokerError:
                raise
            except Exception:
                state = None                 # the popup re-rendering its document
            if state in ('locked', 'unlocked'):
                return state
            if loop.time() >= end:
                return 'signed_out' if page.url == EXTENSION_POPUP else 'unknown'
            await asyncio.sleep(0.5)

    @_serialized
    async def guest_state(self, driver_session):
        s = self._session(driver_session)
        if s.kind != 'guest':
            raise BrokerError('refused')
        return {'state': await self._popup_state(driver_session, s)}

    async def _guest_field(self, sid, frame, selector, wait_s):
        """The first element matching ``selector`` in ``frame`` within ``wait_s``, or None."""
        loop = asyncio.get_running_loop()
        end = loop.time() + wait_s
        while True:
            try:
                element = await self._checked(sid, frame.query_selector(selector))
            except BrokerError:
                raise
            except Exception:
                element = None               # a navigation of the sign-in page under the read
            if element is not None or loop.time() >= end:
                return element
            await asyncio.sleep(0.25)

    async def _guest_set(self, sid, element, origin, path, kind, value):
        """``_GUEST_SET`` refuses in the page unless the element's own document is at exactly
        ``origin`` and ``path`` and its type is ``kind``, in the very evaluation that sets the
        value."""
        if not await self._checked(sid, element.evaluate(
                _GUEST_SET, dict(origin=origin, path=path, type=kind, value=value))):
            raise BrokerError('refused')

    @_serialized
    async def guest_sign_in(self, driver_session, region_host, email, secret_key, password):
        """1Password's two-step sign-in (5v, measured): the email, Enter; then the Secret Key and
        the password, Enter; the tab lands on the extension's own page. Every value goes only
        into the guest tab's main frame at exactly ``https://my.1password.<region>``."""
        s = self._session(driver_session)
        if s.kind != 'guest':
            raise BrokerError('refused')
        suffix = next((suffix for suffix in ('com', 'eu', 'ca')
                       if region_host == 'my.1password.' + suffix or
                       region_host.endswith('.1password.' + suffix)), None)
        if suffix is None:
            raise BrokerError('refused')
        origin = 'https://my.1password.' + suffix
        if s.token is None:
            s.token = await asyncio.wait_for(s.proxy.open_window(), 10)
            self._session(driver_session)
        page = await self._guest_tab(driver_session, s, origin + GUEST_SIGNIN_PATH + '?auth-only=1',
                                     fresh=True)
        frame = page.main_frame
        field = await self._guest_field(driver_session, frame, 'input[type=email]',
                                        GUEST_FIELD_WAIT_S)
        if field is None:
            return {'state': 'unknown'}
        await self._guest_set(driver_session, field, origin, GUEST_SIGNIN_PATH, 'email', email)
        await self._checked(driver_session, field.press('Enter'))
        key = await self._guest_field(driver_session, frame, '#account-key', GUEST_FIELD_WAIT_S)
        secret = key and await self._guest_field(driver_session, frame, 'input[type=password]', 2)
        if key is None or secret is None:
            return {'state': 'unknown'}       # e.g. a verification step: not ours to answer
        await self._guest_set(driver_session, key, origin, GUEST_SIGNIN_PATH, 'text', secret_key)
        await self._guest_set(driver_session, secret, origin, GUEST_SIGNIN_PATH, 'password',
                              password)
        await self._checked(driver_session, secret.press('Enter'))
        loop = asyncio.get_running_loop()
        end = loop.time() + GUEST_LANDING_WAIT_S
        while not page.url.startswith(EXTENSION_PAGES):
            self._session(driver_session)
            if page.is_closed() or loop.time() >= end:
                return {'state': 'unknown'}
            await asyncio.sleep(0.25)
        return {'state': await self._popup_state(driver_session, s)}

    @_serialized
    async def guest_unlock(self, driver_session, password):
        """The password into the extension's own lock screen, in the guest tab only."""
        s = self._session(driver_session)
        if s.kind != 'guest':
            raise BrokerError('refused')
        state = await self._popup_state(driver_session, s, reload=False)
        if state != 'locked':
            return {'state': state}
        field = await self._guest_field(driver_session, s.guest_page.main_frame,
                                        'input[type=password]', 0)
        if field is None:
            return {'state': 'unknown'}
        await self._guest_set(driver_session, field, EXTENSION_ORIGIN, EXTENSION_POPUP_PATH,
                              'password', password)
        await self._checked(driver_session, field.press('Enter'))
        loop = asyncio.get_running_loop()
        end = loop.time() + 5
        while True:
            state = await self._popup_state(driver_session, s, reload=False)
            if state == 'unlocked' or loop.time() >= end:
                return {'state': state}
            await asyncio.sleep(0.25)

    @_serialized
    async def begin_live_capture(self, driver_session):
        s = self._session(driver_session)
        if s.kind != 'capture' or s.capture_id is not None:
            raise BrokerError('refused')
        s.capture_id = secrets.token_urlsafe(24)
        s.buffer = CaptureBuffer()
        loop = asyncio.get_running_loop()
        began = loop.time()
        origins = s.origins | frozenset(await self._site_origins(s))
        s.capture_origins = origins
        binding = 'capture_' + secrets.token_hex(16)
        nonce = secrets.token_urlsafe(32)

        async def receive(source, received_nonce=None, origin=None, role=None, value=None,
                          field=None):
            try:
                if (s.aborted or (s.closed and not s.collecting) or
                        s.capture_id is None or received_nonce != nonce):
                    return
                if not s.collecting:
                    self._session(driver_session, capture=True)
                if origin not in origins:
                    return
                doc = field if isinstance(field, str) and len(field) <= 64 else None
                if role in ATTEMPT_EVENTS:
                    if s.buffer.event(role, value, doc, login=origin in s.origins) and \
                            not s.collecting:
                        s.readings.clear()           # a new attempt: its own pages follow
                        read_later(READ_AFTER_ATTEMPT_S)
                    return
                s.buffer.record(role, value, doc)
            except Exception:
                pass

        async def take_reading(delay):
            # Slice 5 (S5-4): one reading of the main frame after the last attempt, by the
            # controller's reader (the gate's own reader: masked, no control value); the last
            # READINGS_KEPT are kept.
            await asyncio.sleep(delay)
            if s.reader is None or s.capture_id is None or s.closed or s.aborted:
                return
            reading = await s.reader(s.page)
            if reading is None or s.capture_id is None or not s.buffer.has_attempt():
                return
            s.readings.append(reading)
            del s.readings[:-READINGS_KEPT]

        def read_later(delay):
            if s.reader is None:
                return
            task = asyncio.create_task(take_reading(delay))
            s.tasks.add(task)
            task.add_done_callback(finished)

        s.binding = await s.browser.context.expose_binding(binding, receive)
        self._session(driver_session, capture=True)
        s.listeners.append((s.browser.context, 'binding', binding))
        log.info("capture open: reader's binding %.2f s", loop.time() - began)

        async def inject(frame, strict=False):
            for attempt in range(1 if strict else INSTALL_ATTEMPTS):
                try:
                    async with self._operation(driver_session, 'begin_live_capture'):
                        await install(frame)
                    return
                except _FrameStalled:
                    # What is typed there may not be kept, so the capture says so; a later
                    # navigation of the frame installs again. The view opens.
                    s.buffer.mark_incomplete()
                    log.warning("capture open: a frame at %s did not answer in %s s",
                                _frame_address(frame) or 'no address', CAPTURE_FRAME_S)
                    return
                except Exception:
                    if strict:
                        raise BrokerError('unavailable') from None
                    if s.capture_id is None or s.closed or s.aborted or frame.is_detached():
                        return
                    await asyncio.sleep(INSTALL_RETRY_S)   # a document still settling
            # A document of a capture origin that never took the reader: what is typed there is
            # not kept, so the capture says so (Done then offers the manual entry) rather than
            # report that nothing was typed (casa-test F2).
            try:
                url_origin = _url_origin(frame.url)
            except Exception:  # noqa: BLE001
                url_origin = None
            if url_origin is None or url_origin in origins:
                s.buffer.mark_incomplete()

        async def install(frame):
            if s.capture_id is None:
                return
            if not _capture_frame(frame, origins):
                return
            try:
                async with asyncio.timeout(CAPTURE_FRAME_S):
                    origin = await self._origin(driver_session, frame, capture=True)
                    if origin not in origins:
                        return
                    await frame.evaluate(_CAPTURE_INSTALL,
                                         dict(key=binding, origin=origin, nonce=nonce))
            except TimeoutError:
                raise _FrameStalled from None
            self._session(driver_session, capture=True)

        def finished(task):
            s.tasks.discard(task)
            if not task.cancelled():
                self._consume_task(task)

        def schedule(frame):
            if s.cleanup_task is not None:
                return
            task = asyncio.create_task(inject(frame))
            s.tasks.add(task)
            task.add_done_callback(finished)
            try:
                main = frame.page.main_frame is frame
            except Exception:  # noqa: BLE001
                main = False
            if main and s.buffer.has_attempt():
                read_later(READ_AFTER_LOAD_S)

        async def attach(page, strict=False):
            started = loop.time()
            async with self._operation(driver_session, 'begin_live_capture'):
                page.on('framenavigated', schedule)
                s.listeners.append((page, 'framenavigated', schedule))
                frames = list(page.frames)
                for frame in frames:
                    await inject(frame, strict)
            log.info("capture open: a page's %d frames %.2f s", len(frames), loop.time() - started)

        def new_page(page):
            if s.cleanup_task is not None:
                return
            task = asyncio.create_task(attach(page))
            s.tasks.add(task)
            task.add_done_callback(finished)

        s.browser.context.on('page', new_page)
        s.listeners.append((s.browser.context, 'page', new_page))
        try:
            for page in s.browser.context.pages:
                await attach(page, strict=True)
            self._session(driver_session, capture=True)
        except Exception:
            await self._stop_capture(s)
            raise BrokerError('unavailable') from None
        return {'capture_id': s.capture_id}

    async def _stop_capture(self, s):
        s.collecting = True
        try:
            await self._collect_capture(s)
        finally:
            s.collecting = False

    async def _collect_capture(self, s):
        for target, event, callback in s.listeners:
            if event != 'binding':
                target.remove_listener(event, callback)
        for task in tuple(s.tasks):
            task.cancel()
        await asyncio.gather(*s.tasks, return_exceptions=True)
        if s.aborted:
            raise BrokerError('refused')
        # Each frame's stop is bound, and all of them together fit the cleanup bound: what a
        # frame that does not answer held is not kept, and the capture says so.
        loop = asyncio.get_running_loop()
        end = loop.time() + CAPTURE_STOP_S
        for page in s.browser.context.pages:
            if page.is_closed():
                continue
            for frame in page.frames:
                if not _capture_frame(frame, s.capture_origins):
                    continue
                for _, event, key in s.listeners:
                    if event == 'binding':
                        try:
                            await asyncio.wait_for(frame.evaluate(
                                "async key => {const c=globalThis[key + '_state']; if(c && c.stop)await c.stop();}",
                                key), max(min(CAPTURE_FRAME_S, end - loop.time()), 0.01))
                        except TimeoutError:
                            s.buffer.mark_incomplete()
                            log.warning("capture stop: a frame at %s did not answer",
                                        _frame_address(frame) or 'no address')
                        except Exception:
                            pass
                        if s.aborted:
                            raise BrokerError('refused')
        s.listeners.clear()
        s.capture_id = None
        if s.binding is not None:
            binding, s.binding = s.binding, None
            try:
                await asyncio.wait_for(binding.dispose(), _CAPTURE_DISPOSE_S)
            except TimeoutError:
                log.warning("capture stop: the reader's binding did not go in time")

    @_serialized
    async def take_capture(self, driver_session, capture_id):
        s = self._session(driver_session, capture=True)
        if s.kind != 'capture' or s.capture_id != capture_id:
            raise BrokerError('refused')
        await self._stop_capture(s)
        self._session(driver_session, capture=True)
        # The readings after the last attempt and the page now (Done) stay in this process for
        # the controller (``take_readings``); the broker never sees them.
        at_done = None
        if s.reader is not None and s.buffer.has_attempt():
            try:
                at_done = await s.reader(s.page)
            except Exception:  # noqa: BLE001 — a page that cannot be read is not shown
                at_done = None
        readings = {'after': list(s.readings), 'at_done': at_done}
        result = s.buffer.take()
        self._readings[capture_id] = readings
        while len(self._readings) > 4:
            self._readings.pop(next(iter(self._readings)))
        # Final collection and session retirement are one locked operation.
        await self._cleanup(driver_session, s)
        return dict(values=[dict(role=v.role, value=v.value) for v in result.values],
                    incomplete=result.incomplete, attempt=result.attempt)

    def take_readings(self, capture_id):
        """A capture's readings (slice 5), once: ``{"after": [...], "at_done": reading | None}``
        (the pages after its last attempt, and the page when it was taken); {} when none."""
        return self._readings.pop(capture_id, {})


def learn_provider_binding(events, site, outcome):
    if outcome != 'signed_in':
        return None
    candidates = [(index, event) for index, event in enumerate(events)
                  if event.binding and 'candidate' in event.binding]
    if len(candidates) != 1:
        return None
    index, candidate = candidates[0]
    binding = candidate.binding
    if candidate.origin not in site.login_origins:
        return None
    # The provider sent him back to the site itself (r4 Astra, Terra: learned without a tap since
    # the operator's ruling 2026-10-31, the callback must land on one of the site's origins).
    if not any(e.binding == {'callback_of': binding['candidate']} and
               e.run_id == candidate.run_id and e.origin in site.origins
               for e in events[index + 1:]):
        return None
    try:
        if set(binding) != {'candidate', 'path_tag', 'client_id_tag', 'redirect_uri_tag'}:
            return None
        return ProviderBinding(candidate.origin, binding['path_tag'], binding['client_id_tag'],
                               binding['redirect_uri_tag'])
    except (ValueError, TypeError):
        return None


MAX_SEEN_PASSWORDS = 256   # password values a capture remembers; past it no username is offered
ATTEMPT_EVENTS = ('attempt', 'typed', 'shown', 'shown_final')   # the capture reader's (slice 5)
READ_AFTER_ATTEMPT_S = 1.5  # the attempt's own page, read once after it (a single-page app)
READ_AFTER_LOAD_S = 1.0     # a page loaded after the attempt, read once it settled
READINGS_KEPT = 3
INSTALL_ATTEMPTS = 6      # a capture reader's install on a new document, before giving up
INSTALL_RETRY_S = 0.25


def _url_origin(url: str) -> str | None:
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    default = 443 if parts.scheme == "https" else 80
    port = f":{parts.port}" if parts.port and parts.port != default else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


@dataclass(repr=False)
class CaptureBuffer:
    """Bounded in-memory candidate values, canonicalised to the two writable roles. A value read
    from a known field replaces that field's earlier one (typing reports every prefix: only the
    field's latest value is a candidate, casa-test F3); values without a field are kept as
    distinct values. At most eight candidates; an older one dropped marks the capture
    incomplete. A value that is a password is never also a username (``take``)."""
    _values: dict = field(default_factory=dict, repr=False)   # role -> [distinct values]
    _fields: dict = field(default_factory=dict, repr=False)   # (role, field) -> latest value
    _plain: set = field(default_factory=set, repr=False)      # (username, field) keys from a plain text field
    _seen: set = field(default_factory=set, repr=False)       # digests of every password value seen
    _seen_overflow: bool = False
    _password_fields: set = field(default_factory=set, repr=False)   # field ids seen as passwords
    _incomplete: bool = False
    _attempt: dict | None = field(default=None, repr=False)   # the last sign-in attempt (S5-4)
    _last_username: str | None = field(default=None, repr=False)

    def record(self, role, value, field_id=None):
        plain = role == 'plain_username'
        if plain:
            role = 'username'
        if role in ('password', 'new_password', 'confirm_password'):
            role = 'password'
        if role not in ('username', 'password') or not isinstance(value, str) or not value:
            return
        if len(value.encode('utf-8')) > 1024:
            self._incomplete = True
            return
        if role == 'password':
            self._seen_add(value)
        if field_id is not None:
            # A field once reported as a password field stays one for the capture: a later
            # value of it is a password value too, never a username (operator 2026-10-23).
            if role == 'password':
                self._password_fields.add(field_id)
            elif field_id in self._password_fields:
                role, plain = 'password', False
                self._seen_add(value)
            if role == 'username':
                self._last_username = value
            key = (role, field_id)
            self._fields.pop(key, None)       # the field's latest value goes last
            self._fields[key] = value
            (self._plain.add if plain else self._plain.discard)(key)
            if len(self._fields) > 8:
                oldest = next(iter(self._fields))
                self._fields.pop(oldest)
                self._plain.discard(oldest)
                self._incomplete = True
            return
        if role == 'username':
            self._last_username = value
        values = self._values.setdefault(role, [])
        if value in values:
            return
        values.append(value)
        if len(values) > 8:
            values.pop(0)
            self._incomplete = True

    def _seen_add(self, value):
        # Every password value seen, for the capture's whole life — whatever later happens to
        # its candidate (replaced by the field's next value, or dropped for room).
        if len(self._seen) < MAX_SEEN_PASSWORDS:
            self._seen.add(hashlib.sha256(value.encode('utf-8')).digest())
        else:
            self._seen_overflow = True

    def mark_incomplete(self):
        self._incomplete = True

    # -- slice 5 (S5-4): the last sign-in attempt ----------------------------------------------

    def event(self, kind, value, doc, *, login):
        """One capture-reader event. ``attempt``: a sign-in sent (its username and password, as
        JSON); the same attempt repeated in the same document (Enter then submit) is one, and
        a username missing from the attempt's form is the last one recorded (a two-page
        sign-in). ``typed``: a password-role field typed into after the attempt.
        ``shown``: a document other than the attempt's, on a login origin, shows a password
        field; ``shown_final``: the attempt's own document still shows one at the take. Either
        marks the attempt failed. True when a new attempt was recorded."""
        a = self._attempt
        if kind == 'attempt':
            try:
                sent = json.loads(value)
                username, password = sent['username'], sent['password']
            except (TypeError, ValueError, KeyError):
                return False
            if not (isinstance(password, str) and isinstance(username, str)):
                return False
            if len(password.encode('utf-8')) > 1024 or len(username.encode('utf-8')) > 1024:
                self._incomplete = True
                return False
            if not password:
                # Slice 5n (O2): an identifier-only sign-in (an email or username sent with no
                # password field in its form: an emailed code or link follows). Its identifier
                # is the attempt's own field's, never one remembered from another page, and
                # never a value seen as a password anywhere in the capture.
                # r2: only an email address is an identifier on its own — a one-time code never
                # is, whatever field the page put it in.
                if (not username or not is_email_identifier(username)
                        or hashlib.sha256(username.encode('utf-8')).digest() in self._seen):
                    return False
            username = username or self._last_username or ''
            if (a is not None and a['doc'] == doc and not a['typed_after']
                    and (a['username'], a['password']) == (username, password)):
                return False
            self._attempt = {'username': username, 'password': password, 'doc': doc,
                             'failed': False, 'typed_after': False}
            return True
        if a is None:
            return False
        if kind == 'typed':
            a['typed_after'] = True
        elif kind == 'shown' and login and doc != a['doc']:
            a['failed'] = True
        elif kind == 'shown_final' and login and doc == a['doc']:
            a['failed'] = True
        return False

    def has_attempt(self):
        return self._attempt is not None

    def take(self):
        merged: dict = {}
        for role, values in self._values.items():
            merged.setdefault(role, []).extend(values)
        for (role, field_id), value in self._fields.items():
            merged.setdefault(role, []).append(value)
        # The seen-password set overflowed: no username is offered at all, whatever field held
        # it (any of them could be a password the set no longer remembers; operator, r9).
        if self._seen_overflow:
            merged.pop('username', None)
        # One value-level rule (operator ruling E-T14-3 = Q1): a value seen as a password
        # anywhere in the capture is never a username candidate, whatever field carried it —
        # a "show password" toggle, a re-read, a text clone, a value since replaced or dropped.
        # A password typed only into a field that never was a password field cannot be told
        # from text (documented residual).
        if 'username' in merged:
            merged['username'] = [v for v in merged['username']
                                  if hashlib.sha256(v.encode('utf-8')).digest() not in self._seen]
        attempt = None
        if self._attempt is not None:
            attempt = {k: self._attempt[k] for k in ('username', 'password', 'failed',
                                                     'typed_after')}
        result = CapturedFields(tuple(CapturedValue(role, value)
                                      for role, values in merged.items()
                                      for value in dict.fromkeys(values)),
                                self._incomplete or self._seen_overflow, attempt)
        self._values.clear()
        self._fields.clear()
        self._plain.clear()
        self._seen.clear()
        self._password_fields.clear()
        self._seen_overflow = False
        self._incomplete = False
        self._attempt = None
        self._last_username = None
        return result


_CAPTURE_INSTALL = """a => {
    if(location.origin!==a.origin) throw Error('refused');
    if(globalThis[a.key + '_state']) return;
    const binding=globalThis[a.key];
    // A document that existed when the binding was exposed gets the raw CDP binding, which takes
    // one dispatch payload; a document created after it gets the driver library's wrapper,
    // which takes the arguments themselves (casa-test F2: a sign-in page loaded after the view
    // opened was never captured, because its calls went in the raw format).
    const raw=Function.prototype.toString.call(binding).includes('[native code]');
    const callbacks=new Map();
    if(raw) binding.callbacks=callbacks;
    let seq=0;
    const call=args => raw ? new Promise(resolve => {
        const id=++seq;
        // Ignore the raw function's return; dispatch acknowledges via callbacks.
        callbacks.set(id,{resolve,reject:resolve});
        binding(JSON.stringify({name:a.key,seq:id,serializedArgs:args}));
    }) : Promise.resolve().then(() => binding(...args)).catch(() => 0);
    // One id per field of this document (casa-test F3: each keystroke's value is the field's
    // latest, not a new candidate).
    const doc=Math.random().toString(36).slice(2,10);
    const ids=new WeakMap();
    let next=0;
    const fieldId=e => { if(!ids.has(e)) ids.set(e, doc + ':' + (++next)); return ids.get(e); };
    // A field that was a password field when the reader first saw it stays one for the whole
    // capture, whatever type the page gives it later (a "show password" toggle before typing;
    // a field added, filled and shown in one script step — the type change's record, taken at
    // each read, says it was a password when it arrived). Operator 2026-10-23.
    const passwordField=new WeakSet();
    const sawPassword=records => {
        for(const r of records) if(r.oldValue && r.oldValue.toLowerCase()==='password') passwordField.add(r.target);
    };
    const watch=new MutationObserver(sawPassword);
    watch.observe(document,{subtree:true,attributes:true,attributeFilter:['type'],attributeOldValue:true});
    const firstSight=() => {
        sawPassword(watch.takeRecords());
        for(const e of document.querySelectorAll('input[type=password]')) passwordField.add(e);
    };
    // A trailing "webauthn" token marks a field the site's passkey request may fill; the field's
    // own name is the token before it ("username webauthn"), as for the sign-in's reader.
    const last=e => {
        const tokens=e.autocomplete.toLowerCase().split(/\\s+/).filter(Boolean);
        if(tokens.length > 1 && tokens[tokens.length-1]==='webauthn') tokens.pop();
        return tokens.pop() || '';
    };
    const role=e => {
        const ac=last(e);
        if(passwordField.has(e) || e.type==='password' || ac==='new-password' || ac==='current-password') return 'password';
        // Slice 5n (r1 Astra, Terra): a field naming one-time-code anywhere in its autocomplete
        // is a code field, never a username, whatever its type or last token says.
        if(((e.getAttribute('autocomplete') || '') + ' ' + e.autocomplete).toLowerCase()
           .split(/\\s+/).includes('one-time-code')) return null;
        if(ac==='username' || ac==='email' || e.type==='email') return 'username';
        // A plain text field with no autocomplete of its own, beside a password field (its
        // form, or the page when it has none), is the username (casa-test F3).
        if((e.type==='text' || e.type==='tel') && (ac==='' || ac==='on') &&
           (e.form || document).querySelector('input[type=password]')) return 'plain_username';
        return null;
    };
    const read=() => {
        const pending=[];
        if(location.origin!==a.origin) return;
        firstSight();
        for(const e of document.querySelectorAll('input')) {
            const r=role(e);
            if(r && e.value) pending.push(call([a.nonce,location.origin,r,e.value,fieldId(e)]));
        }
        return Promise.all(pending);
    };
    const events=['input','change','submit','pagehide'];
    for(const event of events) addEventListener(event,read,true);
    // Slice 5 (S5-4): sign-in attempts. A filled password-role field in a form (or, with no
    // form, in the document) makes a submit, an Enter in one of its fields or a click on one of
    // its buttons an attempt; the attempt carries that scope's username and password, the
    // new-password field's when the form has one (a password change). Slice 5n (O2): a scope
    // with no password-role field at all and a filled field the page marks as an email or a
    // username (type email, autocomplete email/username; never a one-time-code field) is an
    // identifier-only attempt, sent with an empty password.
    // A scope's own inputs: a form's associated controls (an input placed inside it but owned
    // by another form through form= is not its own; r1 Astra), or the document's form-less ones.
    const controls=scope => scope===document
        ? [...document.querySelectorAll('input')].filter(e => !e.form)
        : [...scope.elements].filter(e => e instanceof HTMLInputElement && e.form===scope);
    const filled=scope => controls(scope).filter(e => role(e)==='password' && e.value);
    const attempt=scope => {
        if(location.origin!==a.origin) return;
        firstSight();
        const pw=filled(scope);
        if(!pw.length) {
            const inputs=controls(scope);
            if(inputs.some(e => role(e)==='password')) return;
            // r2 (Astra, Terra): the identifier is the scope's one shown, filled email/username
            // field's value; a hidden one never counts, and two different values are ambiguous:
            // no attempt (the value must also be an email address: CaptureBuffer.event).
            const ids=inputs.filter(e => role(e)==='username' && e.value && e.type!=='hidden' &&
                e.getClientRects().length > 0);
            const values=[...new Set(ids.map(e => e.value))];
            if(values.length!==1) return;
            call([a.nonce,location.origin,'attempt',JSON.stringify({username:values[0],password:''}),doc]);
            return;
        }
        const fresh=pw.find(e => last(e)==='new-password');
        const user=controls(scope).find(e => {
            const r=role(e); return (r==='username' || r==='plain_username') && e.value; });
        call([a.nonce,location.origin,'attempt',
              JSON.stringify({username:user ? user.value : '', password:(fresh || pw[0]).value}),doc]);
    };
    const BUTTON='button,input[type=submit],input[type=button],input[type=image],[role=button]';
    const onSubmit=e => { if(e.target instanceof HTMLFormElement) attempt(e.target); };
    const onKey=e => {
        if(e.key!=='Enter' || !(e.target instanceof HTMLInputElement)) return;
        attempt(e.target.form || document);
    };
    const onClick=e => {
        const b=e.target instanceof Element ? e.target.closest(BUTTON) : null;
        if(!b) return;
        const form=b.form || b.closest('form');
        attempt(form || document);
    };
    const onInput=e => {
        if(e.target instanceof HTMLInputElement && role(e.target)==='password')
            call([a.nonce,location.origin,'typed','',doc]);
    };
    const visiblePassword=() => [...document.querySelectorAll('input')].some(e =>
        role(e)==='password' && e.getClientRects().length > 0 &&
        getComputedStyle(e).visibility!=='hidden');
    let shownSent=false;
    const shown=() => {
        if(!shownSent && location.origin===a.origin && visiblePassword()) {
            shownSent=true;
            call([a.nonce,location.origin,'shown','',doc]);
        }
    };
    const ticks=() => { read(); shown(); };
    addEventListener('submit',onSubmit,true);
    addEventListener('keydown',onKey,true);
    addEventListener('click',onClick,true);
    addEventListener('input',onInput,true);
    const timer=setInterval(ticks,500);
    globalThis[a.key + '_state']={stop:async () => {
        clearInterval(timer);
        await read();
        if(location.origin===a.origin && visiblePassword())
            await call([a.nonce,location.origin,'shown_final','',doc]);
        watch.disconnect();
        for(const event of events) removeEventListener(event,read,true);
        removeEventListener('submit',onSubmit,true);
        removeEventListener('keydown',onKey,true);
        removeEventListener('click',onClick,true);
        removeEventListener('input',onInput,true);
        delete globalThis[a.key + '_state'];
    }};
    ticks();
}"""
