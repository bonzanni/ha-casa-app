"""Controller-side private broker RPC interface."""
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime

from casa_browser.broker import BrokerError
from casa_browser.credential_rpc import call as rpc_call
from casa_browser.protocol import SafeNotice
from collections.abc import Awaitable, Callable

@dataclass
class BrokerClient:
    path: Path = Path('/run/casa-browser/broker/rpc.sock')
    timeout: float = 10.0

    store: object | None = field(default=None, repr=False)

    async def call(self, op, **args):
        try:
            # The broker's 120 s sign-in bound; its guest flow's 105 s (slice 5v).
            long = op in ('login', 'fill_code', 'unlock_guest')
            # The look's read: its settle and up to three reads, within the broker's 25 s.
            bound = (max(self.timeout, 125) if long else
                     max(self.timeout, 30) if op == 'read_sign_in' else
                     # Above the broker's transport to the driver (30 s) and the driver's own
                     # bound on opening a capture (20 s), so the driver always answers first.
                     max(self.timeout, 35) if op == 'begin_capture' else self.timeout)
            reply = await rpc_call(self.path, bound, op, **args)
        except BrokerError as error:
            return {'error': error.code}
        return reply

    async def start(self):
        if self.store is not None:
            await self.import_notices()

    async def import_notices(self, store=None):
        """Refresh the durable delivery copy at startup and before notice delivery.

        Replaying from zero also repairs a controller restored from an older backup.
        Store deduplication makes an interrupted or repeated import harmless.
        """
        store = self.store if store is None else store
        if store is None:
            raise BrokerError('unsupported')
        after = 0
        while True:
            reply = await self.pending_notices(after)
            if 'error' in reply:
                raise BrokerError(reply['error'])
            notices = reply['notices']
            if not notices:
                return
            for notice in notices:
                site = store.get_site(notice['site_id'])
                name = site.display_name if site is not None else 'site'
                text = notice['text_masked'].replace('{display_name}', name)
                store.add_notice(SafeNotice(0, notice['kind'], notice['run_id'], notice['site_id'],
                    text, datetime.fromisoformat(notice['at']), 'broker:' + str(notice['notice_key'])))
                after = notice['notice_key']

    async def status(self):
        return await self.call('status')

    async def begin_run(self, run_id, account_site_ids):
        return await self.call('begin_run', run_id=run_id, account_site_ids=account_site_ids)

    async def end_run(self, run_id):
        return await self.call('end_run', run_id=run_id)

    async def register_item(self, site_id, item_id, login_origins, operator_ref):
        return await self.call('register_item', site_id=site_id, item_id=item_id, login_origins=login_origins, operator_ref=operator_ref)

    async def register_capture(self, site_id, login_origins, operator_ref, title):
        return await self.call('register_capture', site_id=site_id, login_origins=login_origins,
                               operator_ref=operator_ref, title=title)

    async def login(self, run_id, site_id, driver_session, guest_ask=False, *, auto=False,
                    password_sent=False, fields=None):
        extra = {'auto': True, 'password_sent': password_sent} if auto else (
            {'password_sent': True} if password_sent else {})
        if auto and fields:
            extra['fields'] = dict(fields)
        return await self.call('login', run_id=run_id, site_id=site_id, driver_session=driver_session,
                               guest_ask=guest_ask, **extra)

    async def read_sign_in(self, run_id, site_id, driver_session):
        """The look's read of the page (signing in off the start page): facts, never values."""
        return await self.call('read_sign_in', run_id=run_id, site_id=site_id,
                               driver_session=driver_session)

    async def fill_code(self, run_id, site_id, driver_session, page_generation, code):
        """Slice 6 (S6-3): the code goes in this one call's line and nowhere else."""
        return await self.call('fill_code', run_id=run_id, site_id=site_id,
                               driver_session=driver_session, page_generation=page_generation,
                               code=code)

    async def unlock_guest(self, run_id, site_id, driver_session):
        return await self.call('unlock_guest', run_id=run_id, site_id=site_id, driver_session=driver_session)

    async def begin_capture(self, run_id, site_id, driver_session, purpose):
        return await self.call('begin_capture', run_id=run_id, site_id=site_id, driver_session=driver_session, purpose=purpose)

    async def end_capture(self, run_id, site_id, capture_id):
        return await self.call('end_capture', run_id=run_id, site_id=site_id, capture_id=capture_id)

    async def forget_site(self, site_id, operator_ref):
        return await self.call('forget_site', site_id=site_id, operator_ref=operator_ref)

    async def pending_notices(self, after):
        return await self.call('pending_notices', after=after)

    # -- slice 5: vault listing and the live-view sign-in -------------------------------------

    async def list_login_items(self, run_id, site_id):
        return await self.call('list_login_items', run_id=run_id, site_id=site_id)

    async def list_vault_logins(self, run_id):
        return await self.call('list_vault_logins', run_id=run_id)

    async def take_attempt(self, run_id, site_id, capture_id):
        return await self.call('take_attempt', run_id=run_id, site_id=site_id, capture_id=capture_id)

    async def save_attempt(self, run_id, site_id, capture_id, title):
        return await self.call('save_attempt', run_id=run_id, site_id=site_id,
                               capture_id=capture_id, title=title)

    async def drop_attempt(self, run_id, site_id, capture_id):
        return await self.call('drop_attempt', run_id=run_id, site_id=site_id, capture_id=capture_id)


async def arm_capture_then(client, *, run_id, site_id, driver_session, purpose,
                           expose: Callable[[], Awaitable[None]]):
    """Await begin_capture acknowledgement before awaiting expose(); never expose on failure.

    Return the capture reply (including fixed-code refusals); propagate transport failures.
    The controller uses this seam before granting live-view input.
    """
    reply = await client.begin_capture(run_id, site_id, driver_session, purpose)
    if 'error' not in reply:
        await expose()
    return reply
