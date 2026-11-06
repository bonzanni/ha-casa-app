"""Synthetic SDK and driver doubles; credentials are generated only during a test."""
from __future__ import annotations

import copy
import json
import platform
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from onepassword.errors import raise_typed_exception
from onepassword.types import (
    Item, ItemField, ItemOverview, ItemFieldDetailsOtp, OtpFieldDetails, VaultAccess,
    VaultAccessorType, VaultGetParams, VaultOverview,
)

if platform.machine().lower() in ('x86_64', 'amd64'):
    from onepassword.lib.x86_64.op_uniffi_core import Error as NativeError
else:
    from onepassword.lib.aarch64.op_uniffi_core import Error as NativeError

from casa_browser.broker.items import Vault
from casa_browser.broker.journal import Journal
from casa_browser.broker.main import Broker
from casa_browser.login import CapturedFields, CapturedValue, LoginFields
from casa_browser.options import BrokerOptions

LOGIN = ('https://login.example',)
ALL_SDK_CALLS = []
FORBIDDEN_SDK_METHODS = frozenset({
    'items.delete', 'items.delete_all', 'items.archive', 'vaults.delete', 'vaults.update',
    'vaults.grant_group_permissions', 'vaults.update_group_permissions',
    'vaults.revoke_group_permissions', 'inner_client.invoke',
})


class CallLog(list):
    def append(self, value):
        ALL_SDK_CALLS.append(value)
        super().append(value)


def canary():
    """A fixture secret. One scannable shape for every one of them (``itsecret`` + 32 hex), so
    the image lane's container-log scan (``run-integration.sh``) sees any that leaks."""
    return "itsecret" + secrets.token_hex(16)


def item_value(item, role):
    return next(f.value for f in item.fields if f.id == role)


def attempt_of(values, **flags):
    """The driver's sign-in attempt record (slice 5) for ``values`` (role, value) sent."""
    sent = dict(values)
    return {'username': sent.get('username', ''), 'password': sent.get('password', ''),
            'failed': False, 'typed_after': False, **flags}


def sdk_error(name, message=None):
    """Run the installed SDK's native error through its real Python bridge."""
    try:
        try:
            raise NativeError.Error(json.dumps({'name': name, 'message': message or canary()}))
        except Exception as native:
            raise_typed_exception(native)
    except Exception as error:
        return error


def make_item(item_id='item', vault_id='vault'):
    now = datetime.now(timezone.utc)
    return Item(id=item_id, title='Synthetic login', category='Login', vault_id=vault_id,
                fields=[ItemField(id='username', title='username', field_type='Text', value=canary()),
                        ItemField(id='password', title='password', field_type='Concealed', value=canary()),
                        ItemField(id='otp', title='one-time code', field_type='Totp', value=canary())],
                sections=[], notes='', tags=[], websites=[], version=1, files=[],
                created_at=now.strftime('%Y-%m-%dT%H:%M:%SZ'), updated_at=now.strftime('%Y-%m-%dT%H:%M:%SZ'))


class FakeSDK:
    """SDK 0.4 method signatures, detached reads, versioned writes and safe call records."""
    def __init__(self):
        self.calls = CallLog()
        self.forbidden_log_path = None
        self.saved = {('vault', 'item'): make_item()}
        self.history = []
        self.fail = None
        self.before_write = None
        self.read_failure = None
        self.vault_failure = None
        self.vault_count = 1
        self.returned_identity = None
        self.otp_reads = []
        self.archived = set()
        self.accessors = [VaultAccess(vault_uuid='vault', accessor_type=VaultAccessorType.USER,
                                     accessor_uuid='guest', permissions=1072)]
        self.inner_client = self.InnerClient(self)
        self.items = self.Items(self)
        self.vaults = self.Vaults(self)

    def forbid(self, method, *args):
        record = (method, *args)
        self.calls.append(record)
        # Image tests run the SDK in another process; retain evidence even if the
        # broker catches the exception or the harness terminates it with SIGTERM.
        if self.forbidden_log_path is not None:
            import json
            with self.forbidden_log_path.open('a') as stream:
                stream.write(json.dumps(record) + '\n')
        raise AssertionError('Forbidden SDK operation: ' + method)

    class InnerClient:
        def __init__(self, sdk):
            self.sdk = sdk

        async def invoke(self, invoke_config):
            # Retain only the operation name, never arbitrary (possibly secret) params.
            name = invoke_config.get('invocation', {}).get('parameters', {}).get('name')
            self.sdk.forbid('inner_client.invoke', name)

    class Items:
        def __init__(self, sdk):
            self.sdk = sdk
            self.inner_client = sdk.inner_client

        async def get(self, vault_id, item_id):
            s = self.sdk
            s.calls.append(('items.get', vault_id, item_id))
            if s.read_failure:
                raise s.read_failure
            if (vault_id, item_id) not in s.saved:
                raise sdk_error('ItemNotFound')
            item = copy.deepcopy(s.saved[(vault_id, item_id)])
            if s.returned_identity:
                item.vault_id, item.id = s.returned_identity
            for f in item.fields:
                if f.field_type == 'Totp':
                    code = str(100000 + len(s.otp_reads))
                    s.otp_reads.append(code)
                    f.details = ItemFieldDetailsOtp(content=OtpFieldDetails.model_construct(code=code))
            return item

        async def create(self, params):
            s = self.sdk
            s.calls.append(('items.create', params.vault_id, params.category))
            return self._write(params, fresh=True)

        async def put(self, item):
            self.sdk.calls.append(('items.put', item.vault_id, item.id, item.version))
            return self._write(item, fresh=False)

        def _write(self, value, fresh):
            s = self.sdk
            if s.before_write:
                s.before_write()
            mode, s.fail = s.fail, None
            if mode == 'refuse':
                raise sdk_error('ItemUpdateFailure', 'itemStatusPermissionError: ' + canary())
            if isinstance(mode, Exception):
                raise mode
            if mode == 'raise_before':
                raise sdk_error('Transport')
            if fresh:
                item = make_item('created_' + str(len(s.saved)), value.vault_id)
                for name in ('title', 'category', 'fields', 'sections', 'notes', 'tags', 'websites'):
                    v = getattr(value, name)
                    if v is not None:
                        setattr(item, name, copy.deepcopy(v))
            else:
                old = s.saved[(value.vault_id, value.id)]
                if old.version != value.version:
                    raise sdk_error('ItemUpdateFailure', 'itemStatusIncorrectItemVersion: ' + canary())
                s.history.append(copy.deepcopy(old))
                item = copy.deepcopy(value)
                item.version += 1
            s.saved[(item.vault_id, item.id)] = copy.deepcopy(item)
            if mode == 'write_then_raise':
                raise sdk_error('Transport')
            return copy.deepcopy(item)

        async def delete(self, vault_id, item_id):
            self.sdk.forbid('items.delete', vault_id, item_id)

        async def delete_all(self, vault_id, item_ids):
            self.sdk.forbid('items.delete_all', vault_id, tuple(item_ids))

        async def archive(self, vault_id, item_id):
            self.sdk.forbid('items.archive', vault_id, item_id)

        async def list(self, vault_id, *filters):
            """As the SDK: every item of the vault, narrowed by a ``ByState`` filter (an item in
            ``archived`` is archived)."""
            self.sdk.calls.append(('items.list', vault_id, len(filters)))
            if self.sdk.read_failure:
                raise self.sdk.read_failure
            out = []
            for (v, item_id), i in self.sdk.saved.items():
                state = 'archived' if item_id in self.sdk.archived else 'active'
                if v != vault_id or any(not getattr(f.content, state) for f in filters):
                    continue
                out.append(ItemOverview.model_validate(
                    i.model_dump(mode='json', by_alias=True) | {'state': state}))
            return out


    class Vaults:
        def __init__(self, sdk):
            self.sdk = sdk
            self.inner_client = sdk.inner_client

        async def list(self, params=None):
            self.sdk.calls.append(('vaults.list',))
            if self.sdk.vault_failure:
                raise self.sdk.vault_failure
            return [VaultOverview(id='vault', title='Synthetic vault', description='', vault_type='userCreated', active_item_count=len(self.sdk.saved), content_version=1, attribute_version=1, created_at='2000-01-01T00:00:00Z', updated_at='2000-01-01T00:00:00Z') for _ in range(self.sdk.vault_count)]

        async def get(self, vault_id, vault_params: VaultGetParams):
            self.sdk.calls.append(('vaults.get', vault_id, vault_params.accessors))
            if self.sdk.accessors is None:
                raise sdk_error('PermissionDenied')
            return SimpleNamespace(id=vault_id, access=copy.deepcopy(self.sdk.accessors))

        async def delete(self, vault_id):
            self.sdk.forbid('vaults.delete', vault_id)

        async def update(self, vault_id, params):
            self.sdk.forbid('vaults.update', vault_id)

        async def grant_group_permissions(self, vault_id, group_permissions_list):
            self.sdk.forbid('vaults.grant_group_permissions', vault_id, len(group_permissions_list))

        async def update_group_permissions(self, group_permissions_list):
            self.sdk.forbid('vaults.update_group_permissions', len(group_permissions_list))

        async def revoke_group_permissions(self, vault_id, group_id):
            self.sdk.forbid('vaults.revoke_group_permissions', vault_id, group_id)


@dataclass
class Step:
    roles: tuple[str, ...] = ()
    origin_ok: bool = True
    field_error: bool = False
    submit: str | None = 'submit'
    on_site_origin: bool = True
    passkey_frame: bool = False   # 1Password's passkey prompt is shown (slice 5v)
    passkey_hint: bool = False    # a visible webauthn field on a login origin (lazy guest)
    submit_ready: bool = True     # the submit button can be pressed at the read
    empty_box: bool = False       # a shown, empty box of the password's form nothing fills


class FakeDriver:
    def __init__(self, steps=None):
        self.steps = steps or [Step(('username',)), Step(('password',)),
                               Step(('one_time_code',)), Step()]
        self.index = 0
        self.calls = []
        self.fills = []  # test-private secret sink, never a protocol reply
        self.values = []
        self.attempt = None   # the capture's last sign-in attempt (slice 5), or None
        self.incomplete = False
        self.failure = None
        self.before_take = None
        self.after_take = None
        self.guest = 'unlocked'
        self.passkey_ok = True
        self.passkeys = []    # test-private: what choose_passkey was asked to match
        self.marks = {}       # slice 6: step index -> the mark set in that page's document
        self.mark_fails = False
        # Signing in off the start page: the look's binding as the driver reads it. ``binding``
        # is what read_sign_in reports; ``bound`` (callable(op_count) -> (ok, binding) or None
        # for "unchanged") stands for the page moving between the look and a fill.
        self.binding = 'binding-1'
        self.bound = None
        self.bound_checks = 0
        self.labels = {'username': 'Username', 'password': 'Password',
                       'one_time_code': 'Code', 'submit': 'Login'}
        self.read_failure = None
        self.plain = ()           # R9: the read's plain fields (no role the page marks), labels
        self.usable = None        # async callable: submit_usable's reply (default: it can)
        self.submit_reply = None  # submit's reply instead of the click (nothing moves)

    async def call(self, op, **args):
        return await getattr(self, op)(**args)

    async def settle(self, driver_session, timeout_s=30):
        self.calls.append(('settle', driver_session))
        return dict(page_generation=self.index + 1, on_site_origin=self.steps[self.index].on_site_origin,
                    on_login_origin=self.steps[self.index].origin_ok)

    async def find_login_fields(self, driver_session, page_generation, sign_in_look=None,
                                sign_in_fields=None, login_origins=None):
        self.calls.append(('find_login_fields', driver_session, page_generation))
        s = self.steps[self.index]
        return LoginFields(page_generation, s.origin_ok, {r: r for r in s.roles}, s.submit,
                           s.field_error, s.passkey_frame, s.passkey_hint,
                           s.submit_ready, s.empty_box)

    async def submit_usable(self, driver_session, page_generation, submit_handle, login_origins=None):
        self.calls.append(('submit_usable', driver_session, page_generation, submit_handle))
        return await self.usable() if self.usable is not None else {'ok': True}

    async def choose_passkey(self, driver_session, page_generation, title, username):
        """As the real driver (5v): Sign in on a prompt that offers ``title``/``username`` (the
        page then moves on, like a submit), else ``{ok: false}``."""
        self.calls.append(('choose_passkey', driver_session, page_generation))
        self.passkeys.append((title, username))
        s = self.steps[self.index]
        if not (s.passkey_frame and self.passkey_ok):
            return {'ok': False}
        self.index = min(self.index + 1, len(self.steps) - 1)
        return {'ok': True}

    async def fill(self, driver_session, page_generation, field_handle, value):
        self.calls.append(('fill', driver_session, page_generation, field_handle))
        self.fills.append((self.index, field_handle, value))
        return {'ok': True}

    async def submit(self, driver_session, page_generation, submit_handle):
        self.calls.append(('submit', driver_session, page_generation, submit_handle))
        if self.submit_reply is not None:
            return self.submit_reply
        self.index = min(self.index + 1, len(self.steps) - 1)
        return {'ok': True}

    async def read_sign_in(self, driver_session, page_generation, mark, login_origins=None):
        self.calls.append(('read_sign_in', driver_session, page_generation))
        if self.read_failure:
            raise self.read_failure
        s = self.steps[self.index]
        fields = [{'id': f'f{n}', 'role': r, 'label': self.labels.get(r, ''),
                   'password': r == 'password', 'button': self.labels['submit'] if s.submit else None}
                  for n, r in enumerate(sorted(s.roles), 1)]
        fields += [{'id': f'f{n}', 'role': None, 'label': label, 'password': False,
                    'button': self.labels['submit']}
                   for n, label in enumerate(self.plain, len(fields) + 1)]
        return dict(page_generation=page_generation, origin_ok=s.origin_ok,
                    roles=sorted(s.roles), fields=fields,
                    passkey_frame=s.passkey_frame, passkey_hint=s.passkey_hint,
                    binding=self.binding)

    async def sign_in_bound(self, driver_session, page_generation, mark, login_origins=None):
        self.calls.append(('sign_in_bound', driver_session, page_generation))
        self.bound_checks += 1
        got = self.bound(self.bound_checks) if self.bound is not None else None
        if got is None:
            return {'ok': True, 'binding': self.binding}
        ok, binding = got
        return {'ok': ok, 'binding': binding}

    async def mark_document(self, driver_session, page_generation, mark):
        self.calls.append(('mark_document', driver_session, page_generation))
        if self.mark_fails:
            return {'ok': False}
        self.marks[self.index] = mark
        return {'ok': True}

    async def document_marked(self, driver_session, page_generation, mark):
        self.calls.append(('document_marked', driver_session, page_generation))
        return {'ok': self.marks.get(self.index) == mark}

    async def begin_live_capture(self, driver_session):
        self.calls.append(('begin_live_capture', driver_session))
        if self.failure:
            raise self.failure
        return {'capture_id': 'capture'}

    async def take_capture(self, driver_session, capture_id):
        self.calls.append(('take_capture', driver_session, capture_id))
        if self.before_take:
            self.before_take()
        if self.failure:
            raise self.failure
        result = CapturedFields(tuple(CapturedValue(r, v) for r, v in self.values), self.incomplete,
                                self.attempt)
        self.values.clear()
        self.attempt = None
        if self.after_take:
            import inspect
            result_hook = self.after_take()
            if inspect.isawaitable(result_hook):
                await result_hook
        return result

    async def guest_state(self, driver_session):
        self.calls.append(('guest_state', driver_session))
        return {'state': self.guest}

    async def guest_unlock(self, driver_session, password):
        self.calls.append(('guest_unlock', driver_session))
        return {'state': 'unlocked'}

    async def guest_sign_in(self, driver_session, region_host, email, secret_key, password):
        self.calls.append(('guest_sign_in', driver_session, region_host))
        return {'state': 'unlocked'}      # the real sign-in lands unlocked (5v, measured)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Rig:
    """Only orchestration and dependency construction; no broker behavior is faked."""
    def __init__(self, root):
        self.root = root
        self.sdk, self.driver, self.clock = FakeSDK(), FakeDriver(), Clock()
        self.journal = Journal(root / 'journal.db')
        self.options = BrokerOptions(canary(), vault='Synthetic vault',
                                     guest_sign_in_address='acct.1password.eu',
                                     guest_email=canary(), guest_password=canary(), guest_secret_key=canary())
        self.broker = self.new_broker()
        self.replies = []

    def new_broker(self):
        return Broker(Vault(self.sdk, 'Synthetic vault'), self.driver, self.journal,
                      self.root / 'registry.json', self.options, self.clock)

    async def call(self, op, **args):
        reply = await self.broker.dispatch({'op': op, **args})
        self.replies.append(reply)
        return reply

    async def setup(self, *, item=True):
        await self.broker.start()
        if item:
            await self.call('register_item', site_id='site', item_id='item',
                            login_origins=list(LOGIN), operator_ref='operator')
        else:
            await self.call('register_capture', site_id='site', login_origins=list(LOGIN), operator_ref='operator', title='Synthetic site')
        await self.call('begin_run', run_id='run', account_site_ids=['site'])
        self.sdk.calls.clear()
        return self

    async def capture(self, values, purpose='login', *, attempt=None):
        """A live view's capture with ``values`` typed (and ``attempt`` as its last sign-in
        attempt), taken into broker memory as Done takes it (``take_attempt`` and
        ``end_capture`` review first); returns the broker's capture record."""
        self.driver.values = list(values)
        self.driver.attempt = attempt
        begun = await self.call('begin_capture', run_id='run', site_id='site',
                                driver_session='driver', purpose=purpose)
        await self.broker._review_capture('run', 'site', begun['capture_id'])
        return self.broker._captures.get(begun['capture_id'])

    async def save(self, title='Synthetic site'):
        """The automatic save after a ``signed_in`` verdict (S5-5, S5-6): Done's take, then
        the write of the attempt's values."""
        await self.call('take_attempt', run_id='run', site_id='site', capture_id='capture')
        return await self.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                               title=title)

    def overwritable(self):
        """The linked item's own password was rejected in this run (as ``Broker._after_login``
        records it): the automatic save may then overwrite that item (design §5.3 step 4)."""
        self.broker._registry['site']['rejected'] = {'run_id': 'run', 'item_id': 'item'}

    async def restart(self):
        self.journal.close()
        self.journal = Journal(self.root / 'journal.db')
        self.broker = self.new_broker()
        await self.broker.start()

    def writes(self):
        return [c for c in self.sdk.calls if c[0] in ('items.put', 'items.create')]


class SocketDriver:
    """Harness-only broker-side transport to the real controller-hosted driver socket."""
    def __init__(self, path):
        self.path = path
        self.wire_replies = []

    async def call(self, op, **args):
        import asyncio
        import json
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        try:
            writer.write(json.dumps({'op': op, **args}).encode() + b'\n')
            await writer.drain()
            # A guest op waits as the production transport does (slice 5v).
            from casa_browser.broker.main import GUEST_OP_S, GUEST_OPS
            raw = await asyncio.wait_for(reader.readline(), GUEST_OP_S if op in GUEST_OPS else 10)
            self.wire_replies.append((op, raw))
            return json.loads(raw)
        finally:
            writer.close()
            await writer.wait_closed()

    def __getattr__(self, op):
        async def call(**args):
            return await self.call(op, **args)
        return call


class CaptureEndpoint:
    """Supply page-free capture payloads behind the real driver's RPC handler."""
    @staticmethod
    def make(manager, path, source):
        import os
        from casa_browser.login import DriverServer

        class Endpoint(DriverServer):
            async def begin_live_capture(self, driver_session):
                return await source.begin_live_capture(driver_session)

            async def take_capture(self, driver_session, capture_id):
                return await source.take_capture(driver_session, capture_id)

        return Endpoint(manager, path, allowed_uid=os.getuid())


class ControllerRig(Rig):
    """Real broker/client/driver Unix RPC; only page capture and the vault are doubles.

    The relay records exact broker reply bytes before BrokerClient can parse or filter them.
    Driver take_capture replies travel to broker memory, never to controller consumers.
    """
    async def __aenter__(self):
        import asyncio
        import os
        from casa_browser.broker.main import BrokerServer
        from casa_browser.broker_client import BrokerClient
        from fake_controller import FakeController
        self.controller = FakeController(self.root / 'controller.db')
        self.wire_replies = []
        self.exposure_order = []
        self.source = self.driver
        from casa_browser.browser import BrowserSession
        page = SimpleNamespace(is_closed=lambda: False)
        session = BrowserSession('account', 'site', SimpleNamespace(pages=[page]))
        self.endpoint = CaptureEndpoint.make(SimpleNamespace(session=session), self.root / 'driver.sock', self.source)
        self.browser_session = session
        self.driver = SocketDriver(self.endpoint.path)
        self.broker = self.new_broker()
        self.server = BrokerServer(self.broker, self.root / 'broker.sock', allowed_uid=os.getuid())
        self.client = BrokerClient(self.root / 'controller.sock')
        self.relay = None
        try:
            await self.endpoint.start()
            self.driver_session = await self.endpoint.open_session('capture', 'run', 'site', self.browser_session, SimpleNamespace(), LOGIN)
            await self.broker.start()
            await self.server.start()

            async def relay(reader, writer):
                upstream_reader, upstream_writer = await asyncio.open_unix_connection(str(self.server.path))
                async def pump(src, dst, record=False):
                    try:
                        while data := await src.read(65536):
                            if record:
                                self.wire_replies.append(data)
                            dst.write(data)
                            await dst.drain()
                    finally:
                        dst.close()
                        await dst.wait_closed()
                await asyncio.gather(pump(reader, upstream_writer), pump(upstream_reader, writer, True))
            self.relay = await asyncio.start_unix_server(relay, path=str(self.client.path))
            return self
        except BaseException:
            self.controller.store.close()
            raise

    async def __aexit__(self, *exc):
        if self.relay:
            self.relay.close()
            await self.relay.wait_closed()
        await self.server.close()
        await self.endpoint.close()
        self.controller.store.close()
        self.journal.close()

    async def expose_input(self):
        """The controller's grant callback, invoked only by arm_capture_then."""
        self.exposure_order.append('expose_input')

    async def call(self, op, **args):
        reply = await self.client.call(op, **args)
        self.replies.append(reply)
        return reply

    async def setup(self, *, item=True):
        if item:
            await self.call('register_item', site_id='site', item_id='item', login_origins=list(LOGIN), operator_ref='operator')
        else:
            await self.call('register_capture', site_id='site', login_origins=list(LOGIN), operator_ref='operator', title='Synthetic site')
        await self.call('begin_run', run_id='run', account_site_ids=['site'])
        self.sdk.calls.clear()
        return self

    async def capture(self, values, purpose='login', *, attempt=None):
        """Over the real RPC: the capture armed, then taken at Done (``take_attempt``, whose
        reply carries flags only); returns that reply."""
        self.source.values = list(values)
        self.source.attempt = attempt
        begun = await self.call('begin_capture', run_id='run', site_id='site', driver_session=self.driver_session, purpose=purpose)
        return await self.call('take_attempt', run_id='run', site_id='site', capture_id=begun['capture_id'])

    async def save(self, title='Synthetic site'):
        """``save_attempt`` over the real RPC, after ``capture`` took the attempt."""
        return await self.call('save_attempt', run_id='run', site_id='site', capture_id='capture',
                               title=title)


async def serve_image_broker(config):
    """Test entry point: inject the SDK at construction, never through production options."""
    import asyncio
    import json
    import os
    from casa_browser.broker import BrokerError
    from casa_browser.broker.main import BrokerServer
    assert os.getuid() == 1102
    root = Path(config['root'])
    sdk = FakeSDK()
    sdk.forbidden_log_path = root / 'forbidden-sdk-calls.jsonl'
    snapshot = root / 'fake-vault.json'
    sdk.saved = {(i['vaultId'], i['id']): Item.model_validate(i) for i in json.loads(snapshot.read_text())}
    original = sdk.items._write
    def persist(value, fresh):
        result = original(value, fresh)
        snapshot.write_text(json.dumps([i.model_dump(mode='json', by_alias=True) for i in sdk.saved.values()]))
        return result
    sdk.items._write = persist
    options = BrokerOptions(**config['options'])
    broker = Broker(Vault(sdk, options.vault), SocketDriver(config['driver_socket']),
                    Journal(root / 'journal.db'), root / 'registry.json', options)
    await broker.start()
    real_dispatch = broker.dispatch

    async def dispatch(request):
        # Test instrumentation, never a broker op: what a capture took into broker memory, each
        # value as its role and a hint (a username, or a password's length and last two
        # characters), so the image lane can check what real pages hand the capture.
        if isinstance(request, dict) and request.get('op') == 'test_review_capture':
            try:
                await broker._review_capture(request['run_id'], request['site_id'],
                                             request['capture_id'])
            except BrokerError as error:
                return {'error': error.code}
            capture = broker._captures.get(request['capture_id'])
            values = capture.candidates.values() if capture is not None else ()
            return {'candidates': [dict(role=role, hint=value if role == 'username'
                                        else f'password ({len(value)}) …{value[-2:]}')
                                   for role, value in values],
                    'capture_incomplete': bool(capture and capture.incomplete)}
        return await real_dispatch(request)
    broker.dispatch = dispatch
    server = BrokerServer(broker, root / 'rpc.sock', allowed_uid=1103)
    await server.start()
    os.chmod(root / 'rpc.sock', 0o666)
    print('READY', flush=True)
    if config.get('log_marker'):   # test instrumentation: a planted canary on the broker's stderr
        import sys
        print('planted ' + config['log_marker'], file=sys.stderr, flush=True)
        print('planted ' + config['log_marker'], flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.close()


if __name__ == '__main__':
    import asyncio
    import json
    import sys
    asyncio.run(serve_image_broker(json.loads(sys.stdin.readline())))
