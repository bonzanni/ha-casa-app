"""Vault-scoped reads and versioned field writes, without destructive operations."""
from ast import literal_eval
import json
import re
from dataclasses import dataclass, field
from onepassword import Client
from onepassword.types import (ItemCreateParams, ItemField, ItemListFilterByState,
                               ItemListFilterByStateInner, VaultGetParams, Website)

from casa_browser.broker import BrokerError

EXTRA_FIELDS_WARNING = "The extension can see the whole browsing vault."

# The SDK bridge wraps native errors in plain Exception. Its context still holds
# the JSON envelope; update failures may identify the subtype only in the message.
_SDK_CODES = {
    'ItemNotFound': 'not_found', 'itemNotFound': 'not_found',
    'PermissionDenied': 'refused',
    'ItemStatusPermissionError': 'refused', 'itemStatusPermissionError': 'refused',
    'ItemStatusIncorrectItemVersion': 'conflict', 'itemStatusIncorrectItemVersion': 'conflict',
}
_SDK_TOKEN = r'\b(' + '|'.join(_SDK_CODES) + r')\b'


def _sdk_code(error):
    if isinstance(error, TimeoutError):
        return 'timeout'
    payload = None
    context = error.__context__
    # Native Error stores its envelope as msg=<Python string repr> in args.
    # Read that fixed payload without inspecting arbitrary SDK attributes.
    if context is not None and len(context.args) == 1:
        native = context.args[0]
        if isinstance(native, str) and native.startswith('msg='):
            try:
                payload = json.loads(literal_eval(native[4:]))
            except (SyntaxError, TypeError, ValueError):
                pass
    if isinstance(payload, dict):
        name = payload.get('name')
        if isinstance(name, str) and name in _SDK_CODES:
            return _SDK_CODES[name]
    # Never classify human prose such as "permission denied" or "not found".
    message = error.args[0] if len(error.args) == 1 else None
    if isinstance(message, str):
        tokens = re.findall(_SDK_TOKEN, message)
        codes = {_SDK_CODES[token] for token in tokens}
        if len(codes) == 1:
            return codes.pop()
    return 'unavailable'

@dataclass
class Vault:
    _client: object = field(repr=False)
    vault_name: str
    _auth_token: str = field(default='', repr=False)
    _vault_id: str | None = field(default=None, init=False, repr=False)

    async def resolve_vault_id(self):
        if self._vault_id is None:
            try:
                if self._client is None:
                    self._client = await Client.authenticate(
                        auth=self._auth_token, integration_name='Casa Browser', integration_version='1.0')
                matches = [v.id for v in await self._client.vaults.list()
                           if v.title == self.vault_name]
            except Exception as error:
                raise BrokerError(_sdk_code(error)) from None
            if len(matches) != 1:
                raise BrokerError('refused')
            self._vault_id = matches[0]
        return self._vault_id

    async def get(self, item_id):
        vault_id = await self.resolve_vault_id()
        try:
            item = await self._client.items.get(vault_id, item_id)
        except Exception as error:
            raise BrokerError(_sdk_code(error)) from None
        if item.vault_id != vault_id or item.id != item_id:
            raise BrokerError('refused')
        return item

    async def list_logins(self):
        """The vault's active Login and Password items as SDK overviews (id, title, websites;
        an overview holds no field value). Archived items are left out by the SDK's filter."""
        vault_id = await self.resolve_vault_id()
        try:
            items = await self._client.items.list(vault_id, ItemListFilterByState(
                content=ItemListFilterByStateInner(active=True, archived=False)))
        except Exception as error:
            raise BrokerError(_sdk_code(error)) from None
        return [i for i in items if i.vault_id == vault_id and i.category in ('Login', 'Password')]

    async def create_login(self, title, username, password, website_origins):
        vault_id = await self.resolve_vault_id()
        try:
            params = ItemCreateParams(
                vault_id=vault_id, category='Login', title=title,
                # Slice 5n (O2): an identifier-only login holds no password field at all.
                fields=[ItemField(id='username', title='username', field_type='Text', value=username),
                        *([ItemField(id='password', title='password', field_type='Concealed',
                                     value=password)] if password else [])],
                websites=[Website(url=origin, label='', autofill_behavior='ExactDomain')
                          for origin in website_origins])
            return await self._client.items.create(params)
        except Exception as error:
            raise BrokerError(_sdk_code(error)) from None

    async def update_fields(self, item_id, expected_version, username=None, password=None):
        item = await self.get(item_id)
        if item.version != expected_version:
            item = await self.get(item_id)
            if item.version != expected_version:
                raise BrokerError('conflict')
        for role, value, field_type in (('username', username, 'Text'),
                                        ('password', password, 'Concealed')):
            if value is None:
                continue
            target = next((f for f in item.fields if f.id == role and not f.section_id), None)
            if target is None:
                item.fields.append(ItemField(id=role, title=role, field_type=field_type, value=value))
            else:
                target.value = value
        try:
            return await self._client.items.put(item)
        except Exception as error:
            raise BrokerError(_sdk_code(error)) from None

    async def totp_code(self, item_id):
        item = await self.get(item_id)
        for f in item.fields:
            if f.field_type == 'Totp' and f.details is not None and f.details.content is not None:
                return f.details.content.code
        raise BrokerError('not_found')

    async def vault_accessors(self):
        vault_id = await self.resolve_vault_id()
        try:
            vault = await self._client.vaults.get(vault_id, VaultGetParams(accessors=True))
            return vault.access
        except Exception:
            return None
