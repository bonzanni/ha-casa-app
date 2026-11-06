"""Credential broker process, registry and bounded login orchestration."""
import asyncio
import json
import os
import re
import secrets
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
import time
from casa_browser.broker import BrokerError
from casa_browser.broker.guest import assess_guest
from casa_browser.broker.items import EXTRA_FIELDS_WARNING, Vault
from casa_browser.broker.journal import Journal
from casa_browser.credential_rpc import UnixServer, call as rpc_call, encode
from casa_browser.login import CaptureBuffer, CapturedFields, LoginFields
from casa_browser.options import BrokerOptions
from casa_browser.protocol import ID_PATTERN, _https_origin, is_email_identifier

BROKER_OPS = {'status': (), 'begin_run': ('run_id', 'account_site_ids'), 'end_run': ('run_id',), 'register_item': ('site_id', 'item_id', 'login_origins', 'operator_ref'), 'register_capture': ('site_id', 'login_origins', 'operator_ref', 'title'), 'login': ('run_id', 'site_id', 'driver_session'), 'unlock_guest': ('run_id', 'site_id', 'driver_session'), 'begin_capture': ('run_id', 'site_id', 'driver_session', 'purpose'), 'end_capture': ('run_id', 'site_id', 'capture_id'), 'forget_site': ('site_id', 'operator_ref'), 'pending_notices': ('after',),
              # action model slice 5 (S5-1, S5-5, S5-6)
              'list_login_items': ('run_id', 'site_id'),
              'take_attempt': ('run_id', 'site_id', 'capture_id'),
              'save_attempt': ('run_id', 'site_id', 'capture_id', 'title'),
              'drop_attempt': ('run_id', 'site_id', 'capture_id'),
              # slice 5n: the vault's logins for a sign-in card, before any site exists
              'list_vault_logins': ('run_id',),
              # action model slice 6 (S6-3): the sign-in code the site sent
              'fill_code': ('run_id', 'site_id', 'driver_session', 'page_generation', 'code'),
              # signing in off the start page: the look's read (nothing filled, no vault)
              'read_sign_in': ('run_id', 'site_id', 'driver_session')}

#: keys an op may carry beyond its required ones: ``login``'s ``guest_ask``, ``auto`` (a sign-in
#: on the page a look judged) and ``password_sent`` (the look already sent the password); each
#: False when absent
BROKER_OPTIONAL = {'login': ('guest_ask', 'auto', 'password_sent', 'fields')}
#: R9: a look's sign-in names the plain fields the check mapped, by the ids the look gave them
FIELD_ID = r'f[0-9]{1,6}'

#: a site's one-time sign-in code (``protocol.LOGIN_CODE``)
CODE_PATTERN = r'[A-Za-z0-9-]{4,10}'

#: page changes a sign-in settles and reads again before it asks for the live view (slice 5u)
PAGE_CHANGES = 8
#: the look's read (signing in off the start page): its bound, its settle, and the reads a page
#: that changes under it gets
SIGN_IN_READ_S = 25
SETTLE_QUIET_READ_S = 10
SIGN_IN_READS = 3
#: how long a sign-in waits, once it filled the form, for a disabled submit button to enable
#: (a widget still solving, the form's own checks), and how often it looks
SUBMIT_WAIT_S = 20
SUBMIT_POLL_S = 0.25
#: reads of a field-free page a sign-in that filled nothing yet waits for a form with (5u):
#: each is a settle (at least a quarter second) and a discovery, about two seconds in all
FORM_WAITS = 6
_AGAIN = object()     # a step's answer: read the page again (the late-form wait)
# Slice 5v: a guest op's transport wait (above the driver's 45 s bound on the op, so the driver
# answers first) and the whole guest flow (its two ops, under the run controller's 125 s).
GUEST_OPS = frozenset({'guest_state', 'guest_sign_in', 'guest_unlock'})
GUEST_OP_S = 50
GUEST_FLOW_S = 2 * GUEST_OP_S + 5

LIST_TITLE_CHARS = 300     # a longer title leaves its item out: a title is never cut (d1)
LIST_HOSTS = 10            # website hosts per listed item
LIST_BYTES = 60_000        # a listing stays under one broker line (MAX_LINE, 64 KiB)

@dataclass(repr=False)
class _Capture:
    run_id: str
    site_id: str
    driver_session: str
    expires: float | None
    journal_id: str
    candidates: dict = field(default_factory=dict)
    reviewed: bool = False
    pending: bool = True
    taking: bool = False
    incomplete: bool = False
    expiry: object = None
    attempt: dict | None = None       # the last sign-in attempt, values included (slice 5)
    judging: bool = False             # taken at Done for the sign-in check: held for its verdict


@dataclass
class Broker:
    vault: object = field(repr=False)
    driver: object = field(repr=False)
    journal: object
    registry_path: Path
    options: object = field(repr=False)
    clock: object = time.monotonic
    _registry: dict = field(default_factory=dict, init=False, repr=False)
    _runs: dict = field(default_factory=dict, init=False, repr=False)
    _locks: dict = field(default_factory=dict, init=False, repr=False)
    _captures: dict = field(default_factory=dict, init=False, repr=False)
    # slice 6: (run_id, site_id) -> the mark of the code page that reported needs_code
    _code_marks: dict = field(default_factory=dict, init=False, repr=False)
    # slice 5v: the (run_id, site_id) whose sign-in already tried 1Password's passkey prompt:
    # one try per run and site, whichever sign-in call of the run makes it
    _passkey_tries: set = field(default_factory=set, init=False, repr=False)
    # (run_id, site_id) -> the last look's binding: its mark and digest, until a sign-in on it
    _looks: dict = field(default_factory=dict, init=False, repr=False)

    async def start(self):
        self.registry_path = Path(self.registry_path)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.registry_path.exists():
            os.chmod(self.registry_path, 0o600)
            self._registry = json.loads(self.registry_path.read_text(encoding='utf-8'))
        else:
            self._persist_registry()
        self.journal.recover()

    def _persist_registry(self):
        fd, temporary = tempfile.mkstemp(dir=self.registry_path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(self._registry, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.registry_path)
            directory = os.open(self.registry_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _validate(self, request):
        if not isinstance(request, dict):
            raise BrokerError('unsupported')
        op = request.get('op')
        if not isinstance(op, str) or op not in BROKER_OPS:
            raise BrokerError('unsupported')
        required = {'op', *BROKER_OPS[op]}
        if not required <= set(request) <= required | set(BROKER_OPTIONAL.get(op, ())):
            raise BrokerError('unsupported')
        for key, value in request.items():
            if key in ('op', 'login_origins'):
                continue
            if key == 'after':
                valid = type(value) is int and value >= 0
            elif key == 'account_site_ids':
                valid = isinstance(value, list) and all(
                    isinstance(v, str) and ID_PATTERN.fullmatch(v) for v in value)
            elif key == 'operator_ref':
                valid = isinstance(value, str) and re.fullmatch(r'[\x20-\x7e]{1,128}', value)
            elif key == 'title':   # the site's display name: the new vault item's title
                valid = isinstance(value, str) and re.fullmatch(r'[^\x00-\x1f\x7f]{1,80}', value)
            elif key == 'purpose':
                valid = isinstance(value, str) and value in ('login', 'assist')
            elif key == 'page_generation':
                valid = type(value) is int and value >= 0
            elif key == 'code':
                valid = isinstance(value, str) and re.fullmatch(CODE_PATTERN, value)
            elif key in ('guest_ask', 'auto', 'password_sent'):
                valid = type(value) is bool
            elif key == 'fields':
                valid = (isinstance(value, dict) and len(value) <= 2 and all(
                    role in ('username', 'password') and isinstance(fid, str)
                    and re.fullmatch(FIELD_ID, fid) for role, fid in value.items())
                    and len(set(value.values())) == len(value))
            else:
                valid = isinstance(value, str) and ID_PATTERN.fullmatch(value)
            if not valid:
                raise BrokerError('unsupported')
        if 'login_origins' in request:
            if not isinstance(request['login_origins'], list) or not all(
                    isinstance(origin, str) for origin in request['login_origins']):
                raise BrokerError('unsupported')
            self._origins(request['login_origins'])
        return op, {k: v for k, v in request.items() if k != 'op'}

    @staticmethod
    def _origins(origins):
        if not isinstance(origins, list) or not origins:
            raise BrokerError('refused')
        try:
            return [_https_origin(origin) for origin in origins]
        except Exception:
            raise BrokerError('refused') from None

    async def dispatch(self, request):
        try:
            self._expire_captures()
            op, args = self._validate(request)
            handlers = {
                'status': self.status, 'begin_run': self.begin_run, 'end_run': self.end_run,
                'register_item': self.register_item, 'register_capture': self.register_capture,
                'login': self.login, 'unlock_guest': self.unlock_guest,
                'begin_capture': self.begin_capture, 'end_capture': self.end_capture,
                'forget_site': self.forget_site, 'pending_notices': self.pending_notices,
                'list_login_items': self.list_login_items, 'take_attempt': self.take_attempt,
                'list_vault_logins': self.list_vault_logins,
                'save_attempt': self.save_attempt, 'drop_attempt': self.drop_attempt,
                'fill_code': self.fill_code, 'read_sign_in': self.read_sign_in}
            if 'site_id' in args:
                async with self._locks.setdefault(args['site_id'], asyncio.Lock()):
                    return await handlers[op](**args)
            return await handlers[op](**args)
        except BrokerError as error:
            return {'error': error.code}
        except TimeoutError:
            return {'error': 'timeout'}
        except Exception:
            return {'error': 'internal'}

    async def status(self):
        sdk = 'unconfigured'
        role = assess_guest(None)
        if self.options.configured:
            try:
                role = assess_guest(await self.vault.vault_accessors())
                sdk = 'ok'
            except BrokerError:
                sdk = 'unavailable'
        return dict(configured=self.options.configured, sdk=sdk, guest_role=role.guest_role,
                    readonly_users=role.readonly_users, editable_users=role.editable_users)

    def _account_site(self, run_id, site_id):
        if site_id not in self._runs.get(run_id, ()) or site_id not in self._registry:
            raise BrokerError('refused')
        return self._registry[site_id]

    async def begin_run(self, run_id, account_site_ids):
        self._runs[run_id] = frozenset(account_site_ids)
        return {}

    async def end_run(self, run_id):
        for capture_id, capture in tuple(self._captures.items()):
            if capture.run_id == run_id:
                async with self._locks.setdefault(capture.site_id, asyncio.Lock()):
                    if capture_id not in self._captures:
                        continue
                    if capture.judging:
                        # Taken at Done, never judged: the run's end saves nothing (S5-5).
                        self._drop_capture(capture_id, declined=True)
                        continue
                    if not capture.reviewed:
                        try:
                            await self._review_capture(run_id, capture.site_id, capture_id)
                        except Exception:
                            pass
                    self._drop_capture(capture_id)
        self._runs.pop(run_id, None)
        for key in [k for k in self._code_marks if k[0] == run_id]:
            del self._code_marks[key]
        for key in [k for k in self._looks if k[0] == run_id]:
            del self._looks[key]
        self._passkey_tries = {k for k in self._passkey_tries if k[0] != run_id}
        return {}

    async def register_item(self, site_id, item_id, login_origins, operator_ref):
        origins = self._origins(login_origins)
        item = await self.vault.get(item_id)
        if item.category not in ('Login', 'Password'):
            raise BrokerError('refused')
        roles, extras = [], []
        for f in item.fields:
            if f.id in ('username', 'password') and not f.section_id:
                roles.append(f.id)
            elif f.field_type == 'Totp':
                roles.append('one_time_code')
            elif f.field_type != 'Url':
                extras.append(f.title)
        self._registry[site_id] = dict(item_id=item_id, login_origins=origins, operator_ref=operator_ref)
        self._persist_registry()
        self.journal.record_control('register_item', site_id, operator_ref)
        return dict(site_id=site_id, roles_available=list(dict.fromkeys(roles)),
                    extra_field_names=extras, warning=EXTRA_FIELDS_WARNING)

    async def register_capture(self, site_id, login_origins, operator_ref, title):
        self._registry[site_id] = dict(item_id=None, login_origins=self._origins(login_origins),
                                       operator_ref=operator_ref, title=title)
        self._persist_registry()
        self.journal.record_control('register_capture', site_id, operator_ref)
        return {'site_id': site_id}

    async def _driver_call(self, op, **args):
        reply = await self.driver.call(op, **args)
        if isinstance(reply, (LoginFields, CapturedFields)):
            reply = asdict(reply)
        if not isinstance(reply, dict):
            raise BrokerError('internal')
        if 'error' in reply:
            raise BrokerError(reply['error'])
        return reply

    async def login(self, run_id, site_id, driver_session, guest_ask=False, code=None,
                    page_generation=None, *, auto=False, password_sent=False, fields=None):
        """The automatic sign-in. With ``guest_ask`` (the run controller's: the extension's guest
        has not been unlocked in this browser session), a read that shows a passkey hint ends it
        as ``needs_guest`` before anything more is filled or clicked: the controller unlocks the
        guest and signs in again from the start page, with ``guest_ask`` False.

        With ``auto`` the sign-in is on the page as it is, the page the run's check judged the
        site's sign-in (``read_sign_in``): its first step only while the look's binding holds
        (``no_form``/``page_moved`` otherwise), and after its first submit or passkey answer no
        guest, no passkey and no second password (``password_again``; ``password_sent``: this
        look sent one already). ``fields`` (R9): the check's mapping of the look's plain fields
        to ``username``/``password``, by the ids the look gave them."""
        site = self._account_site(run_id, site_id)
        deadline = self.clock() + 120
        try:
            async with asyncio.timeout(120):
                look = self._looks.pop((run_id, site_id), None) if auto and code is None else None
                if site['item_id'] is None:
                    return {'status': 'needs_live_view', 'reason': 'not_found'}
                mark, answered = None, False
                if code is None:
                    self._code_marks.pop((run_id, site_id), None)   # a new sign-in replaces it
                    if auto and look is None:
                        return {'status': 'no_form', 'reason': 'page_moved'}
                else:
                    held = self._code_marks.pop((run_id, site_id), None)   # one report, one code
                    if held is None:
                        return {'status': 'needs_live_view', 'reason': 'code_page_gone'}
                    # The sign-in goes on: whether it answered a passkey goes on with it (5v),
                    # and a look's sign-in keeps its limits (Astra r3).
                    mark, answered, auto, password_sent = held
                outcome = await self._login_flow(site, driver_session, deadline, code,
                                                 page_generation, mark, answered,
                                                 tries_key=(run_id, site_id),
                                                 guest_ask=guest_ask and code is None,
                                                 auto=auto, look=look,
                                                 password_sent=password_sent,
                                                 mapping=fields)
                if outcome.get('status') == 'needs_code':
                    self._code_marks[(run_id, site_id)] = (
                        outcome.pop('_mark'), outcome.pop('_passkey'), outcome.pop('_auto'),
                        outcome.pop('_password_sent'))
                self._after_login(run_id, site, outcome)
                return outcome
        except TimeoutError:
            return {'status': 'failed', 'reason': 'timeout'}
        except BrokerError as error:
            if error.code == 'timeout':
                return {'status': 'failed', 'reason': 'timeout'}
            raise

    async def read_sign_in(self, run_id, site_id, driver_session):
        """The look (signing in off the start page): one settle and one read of the page in a
        ``login`` driver session — nothing filled or clicked, the vault not read. The reply says
        what a sign-in there would do, never a value or a handle: ``found`` (a role field on a
        login origin, the site's passkey request or 1Password's prompt), the roles, each field's
        label, whether there is one submit and its label, and whether a passkey is offered. A
        found read's binding (its mark, its digest) is kept for ``login(auto=True)``."""
        site = self._account_site(run_id, site_id)
        key = (run_id, site_id)
        self._looks.pop(key, None)
        if site['item_id'] is None:
            return {'found': False}
        mark = secrets.token_urlsafe(24)
        async with asyncio.timeout(SIGN_IN_READ_S):
            for _ in range(SIGN_IN_READS):
                settled = await self._driver_call('settle', driver_session=driver_session,
                                                  timeout_s=SETTLE_QUIET_READ_S)
                try:
                    read = await self._driver_call(
                        'read_sign_in', driver_session=driver_session,
                        page_generation=settled['page_generation'], mark=mark,
                        login_origins=site['login_origins'])
                except BrokerError as error:
                    if error.code == 'page_changed':
                        continue
                    raise
                break
            else:
                raise BrokerError('timeout')
        passkey = bool(read.get('passkey_hint') or read.get('passkey_frame'))
        planned = [f for f in read.get('fields') or () if isinstance(f, dict)]
        # R9: any planned field on a login origin (a plain username included) is the check's
        found = bool(read.get('origin_ok') and (read.get('roles') or planned)) or passkey
        if not found:
            return {'found': False}
        self._looks[key] = {'mark': mark, 'binding': read['binding']}
        return {'found': True, 'passkey': passkey,
                'fields': [{'id': str(f.get('id')), 'role': f.get('role') if isinstance(
                    f.get('role'), str) else None, 'label': str(f.get('label') or ''),
                            'password': f.get('password') is True,
                            'button': str(f['button']) if f.get('button') else None}
                           for f in planned]}

    async def fill_code(self, run_id, site_id, driver_session, page_generation, code):
        """Slice 6 (S6-3): the code the site sent, into the very page ``login`` stopped on
        (``needs_code`` named its ``page_generation``; any navigation since changes it), then the
        rest of the sign-in with the item's values. The code is filled once and kept nowhere:
        not in a reply, the journal, the registry or a log."""
        return await self.login(run_id, site_id, driver_session, False, code, page_generation)

    def _after_login(self, run_id, site, outcome):
        """Slice 5 (S5-6): the linked item's password rejected in this run is recorded (a
        live-view login may then overwrite it); a sign-in that worked clears ``unverified``.
        The private ``_password_rejected`` mark never leaves the broker."""
        if outcome.pop('_password_rejected', False):
            site['rejected'] = {'run_id': run_id, 'item_id': site['item_id']}
        elif outcome.get('status') == 'logged_in' and site.get('unverified'):
            site.pop('unverified')
        else:
            return
        self._persist_registry()

    async def _login_flow(self, site, driver_session, deadline, code=None, page_generation=None,
                          mark=None, answered=False, tries_key=None, guest_ask=False, *,
                          auto=False, look=None, password_sent=False, mapping=None):
        values, has_totp, title = {}, False, ''
        # R9: a look's discovery always carries the look (a password only into a password
        # field), and the plain fields of the judged document the check mapped, by its ids
        mapped = ({'sign_in_look': look['mark'],
                   'sign_in_fields': {fid: role for role, fid in (mapping or {}).items()}}
                  if look is not None else {})

        async def load():
            """The linked item, read once: at the start of a sign-in from the start page, at
            the first use of a value for a look's sign-in (its first read fills nothing)."""
            nonlocal item, values, has_totp, title
            if item is None:
                item = await self.vault.get(site['item_id'])
                values = {f.id: f.value for f in item.fields
                          if f.id in ('username', 'password') and not f.section_id}
                has_totp = any(f.field_type == 'Totp' for f in item.fields)
                title = _shown(item.title) if isinstance(item.title, str) else ''

        item = None
        if not auto or code is not None:
            await load()

        scope_required = False
        approved = False      # this step found fields: set at the read, cleared by our submit
        waits = 0
        # Slice 5v: one passkey try per run and site (``_passkey_tries``), and whether this
        # sign-in's try may have signed in (then it claims nothing; a sign-in continued with a
        # site's code keeps that).
        passkey_answered = answered
        unique = None
        # A look's sign-in: ``bound`` while its judged step has not been submitted (or its
        # passkey answered); after it, the later steps' limits. A code continuation is after.
        after = auto and (look is None or code is not None)
        # One send of the password per sign-in, on either route.
        sent = password_sent
        in_form = False       # this sign-in put the password into the page, however it shows now
        touched = False       # a look's sign-in filled or clicked something

        async def unique_title():
            """The linked item's title names no other login in the vault (5v): the passkey
            prompt shows a title and a username, never an item id. Titles are compared as the
            driver matches the prompt's text: trimmed as JavaScript trims."""
            nonlocal unique
            if unique is None:
                titles = [_shown(o.title) for o in await self.vault.list_logins()]
                unique = bool(title) and titles.count(title) == 1
            return unique

        async def drive(op, **args):
            nonlocal scope_required
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise BrokerError('timeout')
            async with asyncio.timeout(remaining):
                if scope_required:
                    args['login_origins'] = site['login_origins']
                reply = await self._driver_call(op, **args)
                scope_required |= reply.get('login_origins_required') is True
            if self.clock() >= deadline:
                raise BrokerError('timeout')
            return reply

        async def held(generation):
            """The judged step's binding, just before each of its fills, its submit and its
            passkey answer (R7): False ends the sign-in with nothing more done."""
            if look is None or after:
                return True
            reply = await drive('sign_in_bound', driver_session=driver_session,
                                page_generation=generation, mark=look['mark'])
            return reply.get('ok') is True and reply.get('binding') == look['binding']

        moved = {'status': 'no_form', 'reason': 'page_moved'}

        async def enabled(generation, handle):
            """The step's disabled submit button became usable within the bound."""
            until = self.clock() + SUBMIT_WAIT_S
            while True:
                usable = await drive('submit_usable', driver_session=driver_session,
                                     page_generation=generation, submit_handle=handle)
                if usable.get('ok'):
                    return True
                if self.clock() >= until:
                    return False
                await asyncio.sleep(SUBMIT_POLL_S)

        async def step(code, previous_roles):
            """One sign-in step: settle, read the form, fill it, submit it. The roles it filled
            once submitted; ``_AGAIN`` to read the page again; otherwise the sign-in's
            outcome."""
            nonlocal approved, waits, passkey_answered, after, sent, in_form, touched
            remaining = deadline - self.clock()
            if remaining <= 0:
                return {'status': 'failed', 'reason': 'timeout'}
            settled = await drive('settle', driver_session=driver_session,
                                  timeout_s=min(30, remaining))
            generation = settled['page_generation']
            if code is not None and (generation != page_generation or not (await drive(
                    'document_marked', driver_session=driver_session,
                    page_generation=generation, mark=mark)).get('ok')):
                # Only the very document that reported needs_code takes its code (r2): another
                # tab, a reload or a navigation since holds no mark.
                return {'status': 'needs_live_view', 'reason': 'code_page_gone'}
            fields = await drive('find_login_fields', driver_session=driver_session,
                                 page_generation=generation, **mapped)
            roles = fields['roles']
            if after and code is None and not {'password', 'one_time_code'} & set(roles):
                # After a look's first submit, a page with neither a password nor a code to fill
                # is the page after the sign-in (a newsletter box in its footer included).
                roles = {}
            approved |= bool(roles)
            if code is not None and (not fields['origin_ok'] or set(roles) != {'one_time_code'}
                                     or not fields['submit']):
                # The code is for the step that asked for it alone (S6-3), nothing else.
                return {'status': 'needs_live_view', 'reason': 'code_page_gone'}
            if not fields['origin_ok']:
                return {'status': 'needs_live_view', 'reason': 'off_origin'}
            if 'new_password' in roles or 'confirm_password' in roles:
                return {'status': 'needs_live_view', 'reason': 'password_change_page'}
            if guest_ask and not after and (fields['passkey_hint'] or fields['passkey_frame']):
                # A passkey hint (the site's webauthn field, or 1Password's prompt, the locked
                # "Unlock 1Password" one included) while the guest is not unlocked: nothing more
                # is filled or clicked, and the passkey try is not spent.
                return {'status': 'needs_guest', 'reason': 'passkey',
                        **({'password_sent': sent} if auto or sent else {})}
            if fields['passkey_frame'] and not after:
                await load()
            if (fields['passkey_frame'] and code is None and tries_key is not None
                    and not after and tries_key not in self._passkey_tries
                    and values.get('username') and await unique_title()):
                # Slice 5v: 1Password's passkey prompt offering exactly this site's linked
                # item (its title, unique in the vault, and its username) is signed in by its
                # own Sign in; that counts as this step's submit. A look's sign-in answers only
                # its judged page's prompt (R5).
                if not await held(generation):
                    return moved
                touched = True
                self._passkey_tries.add(tries_key)   # before the call: whatever answers it
                try:
                    chosen = await drive('choose_passkey', driver_session=driver_session,
                                         page_generation=generation, title=title,
                                         username=_shown(values['username']))
                except BrokerError:
                    passkey_answered = True   # it may have clicked: claim nothing after it
                    after = auto
                    raise
                if chosen.get('ok'):
                    passkey_answered = True
                    after = auto
                    return {'passkey'}
            if fields['field_error'] and (not roles or set(roles) == previous_roles):
                # The item's own password was rejected only when the step sent it and no code
                # (a username-only step or a code step says nothing about the password).
                return {'status': 'failed', 'reason': 'rejected', '_password_rejected': bool(
                    previous_roles and 'password' in previous_roles
                    and 'one_time_code' not in previous_roles)}
            if not roles:
                if approved:
                    # The fields this step found went away without a submit of ours (slice
                    # 5u, operator ruling): not a completion, whatever the page shows now.
                    return {'status': 'needs_live_view', 'reason': 'page_unsettled'}
                if auto and not after:
                    # The judged page shows no form now: nothing was filled, nothing is claimed.
                    return {'status': 'no_form', 'reason': 'no_form'}
                if previous_roles is None and not auto and waits < FORM_WAITS:
                    waits += 1               # nothing filled yet: a form may still render
                    return _AGAIN
                if settled['on_site_origin'] and not fields['passkey_frame']:
                    # Only a page after a submit of ours is a completion; a sign-in that
                    # filled nothing claims nothing (no_form: the model reading the page
                    # decides whether he is signed in). Neither does a passkey's Sign in
                    # (5v): the site checks the assertion on its own time, and a page without
                    # a form is no proof that it took it.
                    if previous_roles is None and not after:
                        return {'status': 'no_form', 'reason': 'no_form'}
                    if passkey_answered:
                        return {'status': 'no_form', 'reason': 'passkey'}
                    return {'status': 'logged_in', 'reason': 'completed'}
                return {'status': 'needs_live_view', 'reason': 'challenge'}
            if fields.get('empty_box') and 'password' in roles:
                # A shown box of the form that nothing here would fill: a password never goes
                # out beside it. From the start page, with nothing done yet, the check names the
                # fields (the look, on Fido's next observe); nothing is claimed or counted.
                if not auto and not touched:
                    return {'status': 'no_form', 'reason': 'needs_check'}
                return {'status': 'needs_live_view', 'reason': 'empty_field'}
            if not fields['submit'] or any(role not in ('username', 'password', 'one_time_code')
                                           for role in roles):
                return {'status': 'needs_live_view', 'reason': 'ambiguous_fields'}
            await load()
            if set(roles) == {'one_time_code'} and not has_totp and code is None:
                # The site sent a code (email or SMS): the page stays as it is (S6-2), marked
                # in its own document while this session still holds it.
                fresh = secrets.token_urlsafe(24)
                marked = await drive('mark_document', driver_session=driver_session,
                                     page_generation=generation, mark=fresh)
                if not marked.get('ok'):
                    return {'status': 'needs_live_view', 'reason': 'missing_role'}
                return {'status': 'needs_code', 'code_field': True, 'totp_held': False,
                        'page_generation': generation, '_mark': fresh,
                        '_passkey': passkey_answered, '_auto': auto, '_password_sent': sent}
            if ('one_time_code' in roles and not has_totp and code is None) or any(
                    role != 'one_time_code' and not values.get(role) for role in roles):
                return {'status': 'needs_live_view', 'reason': 'missing_role'}
            if (sent and (set(roles) != {'one_time_code'} or previous_roles == {'one_time_code'})
                    or in_form and not sent and 'password' not in roles):
                # The password went out once, or is in a form whose read no longer shows it. A
                # form keeps it however the page shows it, so the only step left to press is the
                # one code step after its send: it is filled and sent once.
                return {'status': 'needs_live_view', 'reason': 'password_again'}
            for role, handle in roles.items():
                try:
                    if role != 'one_time_code':
                        value = values[role]
                    elif code is not None:
                        value = code
                    else:
                        value = await self.vault.totp_code(site['item_id'])
                except BrokerError as error:
                    if error.code == 'not_found':
                        return {'status': 'needs_live_view', 'reason': 'missing_role'}
                    raise
                if not await held(generation):
                    return moved
                touched = True
                in_form = in_form or role == 'password'
                filled = await drive('fill', driver_session=driver_session,
                                     page_generation=generation, field_handle=handle, value=value)
                if not filled.get('ok'):
                    raise BrokerError('refused')
            if not fields.get('submit_ready', True) and not await enabled(generation,
                                                                          fields['submit']):
                # Nothing is sent; what was filled stays for the operator's view.
                return {'status': 'needs_live_view', 'reason': 'submit_disabled'}
            if not await held(generation):
                return moved
            submitted = await drive('submit', driver_session=driver_session,
                                    page_generation=generation, submit_handle=fields['submit'])
            if submitted.get('empty_box'):
                return {'status': 'needs_live_view', 'reason': 'empty_field'}
            if not submitted.get('ok'):
                raise BrokerError('refused')
            # Only a submit that clicked ends the judged step: a page_changed before the click
            # (another frame's navigation) reads the page again under the same binding.
            if 'password' in roles:
                sent = True
            after = auto
            return set(roles)

        previous_roles = None
        steps = changes = 0
        while steps < 6:
            try:
                outcome = await step(code, previous_roles)
            except BrokerError as error:
                if error.code != 'page_changed':
                    if auto and look is not None and not touched and error.code != 'timeout':
                        # The judged page could not be read or acted on before anything was
                        # submitted: nothing is claimed (the observation shows the page).
                        return {'status': 'no_form', 'reason': 'probe_failed'}
                    raise
                # The page changed under the step (slice 5u): settle, read it again, go on.
                changes += 1
                if changes > PAGE_CHANGES:
                    return {'status': 'needs_live_view', 'reason': 'page_unsettled'}
                continue
            if outcome is _AGAIN:
                continue
            if isinstance(outcome, dict):
                return outcome
            steps += 1
            approved = False
            previous_roles = outcome
            code = None                      # filled once: a later code step is a new one
        return {'status': 'needs_live_view', 'reason': 'step_limit'}

    async def unlock_guest(self, run_id, site_id, driver_session):
        self._account_site(run_id, site_id)
        try:
            async with asyncio.timeout(GUEST_FLOW_S):
                return await self._unlock_flow(driver_session)
        except TimeoutError:
            return {'state': 'needs_live_view', 'reason': 'timeout'}
        except BrokerError as error:
            # Best effort (5s, 5v): a driver op that failed or timed out stops nothing; its
            # fixed code is the reason the run controller logs.
            return {'state': 'needs_live_view', 'reason': error.code}

    async def _unlock_flow(self, driver_session):
        reply = await self._driver_call('guest_state', driver_session=driver_session)
        state = reply.get('state')
        if state == 'signed_out':
            reply = await self._driver_call('guest_sign_in', driver_session=driver_session,
                region_host=self.options.guest_sign_in_address, email=self.options.guest_email,
                secret_key=self.options.guest_secret_key, password=self.options.guest_password)
        elif state == 'locked':
            reply = await self._driver_call('guest_unlock', driver_session=driver_session,
                                            password=self.options.guest_password)
        state = reply.get('state')
        if state in ('unlocked', 'signed_in'):
            return {'state': state, 'reason': 'completed'}
        return {'state': 'needs_live_view', 'reason': 'guest_prompt'}

    def _capture(self, run_id, site_id, capture_id):
        self._account_site(run_id, site_id)
        capture = self._captures.get(capture_id)
        if capture is None or (capture.run_id, capture.site_id) != (run_id, site_id):
            raise BrokerError('refused')
        return capture

    def _drop_capture(self, capture_id, *, declined=False):
        capture = self._captures.pop(capture_id, None)
        if capture is not None:
            if capture.expiry is not None:
                capture.expiry.cancel()
            if capture.pending:
                # What a warning names: a username only when that is all that stayed
                # unsaved, of a capture whose values were all kept.
                roles = {role for role, _ in capture.candidates.values()}
                unsaved = ('username' if roles == {'username'} and not capture.incomplete
                           else 'password')
                self.journal.capture_finish(capture.journal_id,
                    'capture_declined' if declined else 'capture_warned', unsaved)
            capture.candidates.clear()

    def _expire_captures(self):
        for capture_id, capture in tuple(self._captures.items()):
            if (capture.expires is not None and self.clock() >= capture.expires
                    and not self._locks.setdefault(capture.site_id, asyncio.Lock()).locked()):
                self._drop_capture(capture_id)

    async def _expire_capture(self, capture_id):
        capture = self._captures.get(capture_id)
        if capture is not None:
            async with self._locks.setdefault(capture.site_id, asyncio.Lock()):
                if (self._captures.get(capture_id) is capture and capture.expires is not None
                        and self.clock() >= capture.expires):
                    self._drop_capture(capture_id)

    def _schedule_expiry(self, capture_id, capture):
        if capture.expiry is not None:
            capture.expiry.cancel()
        capture.expires = self.clock() + 900
        capture.expiry = asyncio.get_running_loop().call_later(
            900, lambda: asyncio.create_task(self._expire_capture(capture_id)))

    async def begin_capture(self, run_id, site_id, driver_session, purpose):
        self._account_site(run_id, site_id)
        if purpose not in ('login', 'assist'):
            raise BrokerError('refused')
        if any(c.site_id == site_id for c in self._captures.values()):
            raise BrokerError('conflict')
        journal_id = secrets.token_urlsafe(24)
        self.journal.capture_pending(journal_id, run_id, site_id)
        try:
            reply = await self._driver_call('begin_live_capture', driver_session=driver_session)
        except BrokerError as error:
            # A timeout or lost reply cannot prove that the driver never armed.
            if error.code in ('refused', 'unsupported'):
                self.journal.capture_finish(journal_id, 'capture_declined')
            raise
        capture_id = reply['capture_id']
        self._captures[capture_id] = _Capture(run_id, site_id, driver_session,
                                             None, journal_id)
        return {'capture_id': capture_id}

    async def _review_capture(self, run_id, site_id, capture_id):
        """The capture's values, taken from the driver once into broker memory (its last
        attempt included); values equal to the linked item's are left out."""
        capture = self._capture(run_id, site_id, capture_id)
        if capture.reviewed:
            return
        if capture.taking:
            raise BrokerError('conflict')
        capture.taking = True
        self._schedule_expiry(capture_id, capture)
        # The arming row already covers cancellation or process exit during take.
        reply = await self._driver_call('take_capture', driver_session=capture.driver_session,
                                        capture_id=capture_id)
        buffer = CaptureBuffer()
        for entry in reply['values']:
            buffer.record(entry['role'], entry['value'])
        captured = buffer.take()
        capture.attempt = _attempt(reply.get('attempt'))
        capture.incomplete = bool(reply.get('incomplete')) or captured.incomplete
        capture.candidates = {secrets.token_urlsafe(24): (v.role, v.value) for v in captured.values}
        capture.reviewed = True
        self._schedule_expiry(capture_id, capture)
        site = self._registry[site_id]
        current = {}
        if site['item_id'] is not None:
            try:
                item = await self.vault.get(site['item_id'])
                current = {f.id: f.value for f in item.fields
                           if f.id in ('username', 'password') and not f.section_id}
            except Exception:
                pass
        capture.candidates = {key: (role, value) for key, (role, value) in capture.candidates.items()
                              if current.get(role) != value}
        if not capture.candidates and not capture.incomplete:
            self.journal.capture_finish(capture.journal_id, 'capture_declined')
            capture.pending = False

    async def end_capture(self, run_id, site_id, capture_id):
        """The live view that took this capture ended (its Done): a capture never reviewed is
        reviewed first (its values reach broker memory), then the capture is dropped — with the
        durable warning when a value that differed from the item stayed unsaved (§8), silently
        when nothing differed or everything was saved — so the site's next live view can
        capture again."""
        self._account_site(run_id, site_id)
        if capture_id not in self._captures:
            return {}  # already ended (dropped by a save that resolved it)
        capture = self._capture(run_id, site_id, capture_id)
        if capture.judging:
            return {}  # held for the sign-in check's verdict (S5-5): save or drop decides
        if not capture.reviewed and not capture.taking:
            try:
                await self._review_capture(run_id, site_id, capture_id)
            except Exception:  # noqa: BLE001 — the arming row already warns at restart
                pass
        self._drop_capture(capture_id)
        return {}

    async def _save(self, capture_id, capture, chosen, consent_id, kind, title=None):
        if not isinstance(consent_id, str) or not re.fullmatch(r'[\x20-\x7e]{1,128}', consent_id):
            raise BrokerError('refused')
        site = self._account_site(capture.run_id, capture.site_id)
        item_id = site['item_id']
        version = None
        read_error = None
        if item_id is not None:
            try:
                version = (await self.vault.get(item_id)).version
            except Exception as error:
                read_error = error if isinstance(error, BrokerError) else BrokerError('unavailable')
        op_id = secrets.token_urlsafe(24)
        self.journal.begin(op_id, kind, capture.site_id, capture.run_id, capture.journal_id,
                           item_id, version, consent_id=consent_id)
        self.journal.transition(op_id, 'attempt')
        try:
            if read_error is not None:
                raise read_error
            if item_id is None:
                # Titled with the site's name as registered (casa-test F8), else its id.
                item = await self.vault.create_login(title or site.get('title') or capture.site_id,
                    chosen.get('username', ''),
                    chosen.get('password', ''), site['login_origins'])
            else:
                item = await self.vault.update_fields(item_id, version, **chosen)
        except Exception as error:
            fixed = error if isinstance(error, BrokerError) else BrokerError('unavailable')
            self.journal.write_failed(op_id, fixed)
            reason = 'failed' if fixed.code in ('refused', 'conflict') else 'unknown'
            item = None
            if reason == 'unknown' and item_id is not None:
                try:
                    reread = await self.vault.get(item_id)
                    actual = {f.id: f.value for f in reread.fields if not f.section_id}
                    if all(actual.get(role) == value for role, value in chosen.items()):
                        item = reread
                except Exception:
                    pass
            if item is None:
                return dict(saved=False, reason=reason, item_id=item_id)
        remaining = {key: (role, value) for key, (role, value) in capture.candidates.items()
                     if role not in chosen}
        resolved = self.journal.transition(op_id, 'vault_written', item_version=item.version,
                                          capture_resolved=not remaining)
        site['item_id'] = item.id
        if item_id is None and consent_id.startswith('auto:'):
            site['unverified'] = True      # until its next automatic sign-in works (S5-6)
        self._persist_registry()
        capture.candidates = remaining
        capture.pending = not resolved
        if resolved:
            self._drop_capture(capture_id)
        return dict(saved=True, item_id=item.id)

    # -- action model slice 5: vault listing and the automatic save --------------------------

    async def list_login_items(self, run_id, site_id):
        """S5-1: the browser vault's active Login and Password items, metadata only — id,
        title, website hosts — for an account site of the run; as many as one broker line
        holds (``truncated`` says when some were left out)."""
        self._account_site(run_id, site_id)
        return await self._vault_logins()

    async def list_vault_logins(self, run_id):
        """Slice 5n: the same listing for the sign-in card of a site that is not registered yet
        (no site, so no per-site lock and no account-site check): metadata only, the item ids
        stay in the controller's memory and never reach a packet or a reply."""
        return await self._vault_logins()

    async def _vault_logins(self):
        items, size = [], len(encode({'items': [], 'truncated': True}))
        for overview in await self.vault.list_logins():
            entry = {'item_id': overview.id, 'title': overview.title,
                     'website_hosts': _hosts(overview.websites)[:LIST_HOSTS]}
            if not ID_PATTERN.fullmatch(entry['item_id']) or not 0 < len(entry['title']) <= \
                    LIST_TITLE_CHARS:
                continue   # a title is shown whole or its item is not offered
            grown = len(json.dumps(entry)) + 2
            if size + grown > LIST_BYTES:
                return {'items': items, 'truncated': True}
            items.append(entry)
            size += grown
        return {'items': items, 'truncated': False}

    async def take_attempt(self, run_id, site_id, capture_id):
        """S5-5, at Done: the capture's last attempt as flags only (never a value), taken from
        the driver first when it was not yet; a capture with an attempt (and complete) is then
        held for the sign-in check's verdict (``save_attempt`` / ``drop_attempt``) or the run's
        end."""
        capture = self._capture(run_id, site_id, capture_id)
        if not capture.reviewed:
            await self._review_capture(run_id, site_id, capture_id)
        if capture.attempt is not None and not capture.incomplete:
            # Only an attempt can be judged; without one the view's release ends the capture
            # as before (``end_capture``).
            capture.judging = True
            if capture.expiry is not None:
                capture.expiry.cancel()
                capture.expiry = None
            capture.expires = None
        a = capture.attempt
        return {'attempt': None if a is None else {'failed': a['failed'],
                                                   'typed_after': a['typed_after']},
                'incomplete': capture.incomplete}

    async def save_attempt(self, run_id, site_id, capture_id, title):
        """S5-6, after a ``signed_in`` verdict: the attempt's credential, written by the rules
        of design §5.3 step 4 — a new item when none is linked (marked ``unverified``); a
        linked item only when its own password was rejected in this run or it is still
        ``unverified``; nothing when the values are equal. The capture ends either way: with a
        failed save's warning when the linked item cannot be read."""
        capture = self._capture(run_id, site_id, capture_id)
        a = capture.attempt
        if (not capture.judging or a is None or a['failed'] or a['typed_after']
                or capture.incomplete):
            raise BrokerError('refused')
        site = self._registry[site_id]
        item_id = site['item_id']
        # Slice 5n (O2): a sign-in that sent only an email or username (an emailed code, a
        # link) saves just that identifier; a password is written only when one was sent.
        chosen = {**({'password': a['password']} if a['password'] else {}),
                  **({'username': a['username']} if a['username'] else {})}
        if not chosen:
            raise BrokerError('refused')
        try:
            if item_id is not None:
                try:
                    item = await self.vault.get(item_id)
                except Exception:  # noqa: BLE001 — any read failure: nothing can be decided
                    # The login he typed is not saved: told with a failed save's warning
                    # (``capture_warned``), never ended as a silent decline.
                    self._drop_capture(capture_id)
                    return {'saved': False, 'reason': 'unknown', 'item_id': item_id}
                current = {f.id: f.value for f in item.fields
                           if f.id in ('username', 'password') and not f.section_id}
                if all(current.get(role) == value for role, value in chosen.items()):
                    return {'saved': False, 'reason': 'equal', 'item_id': item_id}
                rejected = site.get('rejected') or {}
                if not (site.get('unverified') or rejected == {'run_id': run_id,
                                                               'item_id': item_id}):
                    return {'saved': False, 'reason': 'item_kept', 'item_id': item_id}
            reply = await self._save(capture_id, capture, chosen, 'auto:' + capture_id, 'live',
                                     title=title)
            return {k: reply[k] for k in ('saved', 'item_id', 'reason') if k in reply}
        finally:
            self._drop_capture(capture_id, declined=True)

    async def drop_attempt(self, run_id, site_id, capture_id):
        """S5-5: not signed in, unsure or no verdict — the capture ends unsaved, silently."""
        capture = self._capture(run_id, site_id, capture_id)
        if not capture.judging:
            raise BrokerError('refused')
        self._drop_capture(capture_id, declined=True)
        return {}

    async def forget_site(self, site_id, operator_ref):
        for capture_id, capture in tuple(self._captures.items()):
            if capture.site_id == site_id:
                self._drop_capture(capture_id)
        site = self._registry.get(site_id)
        if site is not None:
            # Forget login removes only the item link: the site stays registered with its login
            # origins, so its next login asks the 1Password question again (slice 5).
            self._registry[site_id] = {k: v for k, v in site.items()
                                       if k in ('login_origins', 'operator_ref', 'title')}
            self._registry[site_id]['item_id'] = None
        self._persist_registry()
        self.journal.record_control('forget_site', site_id, operator_ref)
        return {}

    async def pending_notices(self, after):
        notices = []
        for notice in self.journal.pending_notices(after):
            try:
                encode({'notices': [*notices, notice]})
            except BrokerError:
                break
            notices.append(notice)
        return {'notices': notices}

# What JavaScript's String.prototype.trim removes (WhiteSpace and LineTerminator).
_JS_SPACE = ('\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007'
             '\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff')


def _shown(text):
    """A title or username as the driver compares it with the prompt's trimmed text (5v)."""
    return text.strip(_JS_SPACE) if isinstance(text, str) else ''


def _attempt(raw):
    """The driver's attempt record, or None: string values within the capture's bounds and
    the two flags, a flag that is not exactly False read as set (fail closed)."""
    if not isinstance(raw, dict):
        return None
    username, password = raw.get('username'), raw.get('password')
    # A password, or (slice 5n, O2) no password and an email or username: never neither.
    if not (isinstance(password, str) and len(password.encode('utf-8')) <= 1024
            and isinstance(username, str) and len(username.encode('utf-8')) <= 1024
            and (password or username)):
        return None
    if not password:
        if not is_email_identifier(username):
            return None          # r2: only an email address is an identifier on its own
    return {'username': username, 'password': password,
            'failed': raw.get('failed') is not False,
            'typed_after': raw.get('typed_after') is not False}


def _hosts(websites):
    """The hosts of an item's saved websites; an address without a scheme read as https."""
    from urllib.parse import urlsplit
    out = []
    for website in websites or ():
        url = website.url or ''
        try:
            host = urlsplit(url if '://' in url else 'https://' + url).hostname
        except ValueError:
            host = None
        if host and host not in out:
            out.append(host)
    return out


@dataclass
class DriverClient:
    path: Path = Path('/run/casa-browser/driver/driver.sock')
    timeout: float = 30.0

    async def call(self, op, **args):
        # A guest op holds the extension's install wait and a two-step sign-in (slice 5v).
        timeout = GUEST_OP_S if op in GUEST_OPS else self.timeout
        return await rpc_call(self.path, timeout, op, **args)


@dataclass
class BrokerServer(UnixServer):
    broker: Broker
    path: Path
    allowed_uid: int = 1103
    socket_gid: int | None = None

    async def start(self):
        await super().start()
        if self.socket_gid is not None:
            os.chown(self.path, -1, self.socket_gid)

    async def handle(self, reader, writer):
        await self.serve(reader, writer, self.broker.dispatch)


async def run():
    options = BrokerOptions(**json.loads(Path('/run/casa-browser/broker/broker.json').read_text()))
    journal = Journal(Path('/data/broker/journal.db'))
    broker = Broker(Vault(None, options.vault, options.op_token), DriverClient(), journal,
                    Path('/data/broker/registry.json'), options)
    server = BrokerServer(broker, Path('/run/casa-browser/broker/rpc.sock'), socket_gid=1103)
    try:
        await broker.start()
        await server.start()
        await asyncio.Event().wait()
    finally:
        if server.path.exists():
            await server.close()
        journal.close()


def main():
    # Library diagnostics can contain native SDK payloads; no broker diagnostic
    # includes exception text or credential-bearing objects.
    import logging
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit('broker unavailable') from None


if __name__ == '__main__':
    main()
