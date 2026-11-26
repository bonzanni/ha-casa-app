"""Site logins kept in the app's own store, for an install with no 1Password account.

`BuiltinVault` answers the broker the way the 1Password vault class does: the same
methods, and results that carry what the broker reads from an item or a listing. A
login is one item of the store, category `LOGIN`, with these fields:

    username             the sign-in name
    password             only when the login has one
    websites             the sign-in origins, one per line
    one-time password    an authenticator seed, when somebody stored one
    version              a whole number, one higher after every change

Nothing here takes an item away, and no failure carries the store's message text:
every error leaves as one of the broker's fixed codes.

The store calls are made on the broker's event loop, with no thread and no lock of
their own. No method awaits between its store calls, so a version check and the
write that follows it cannot be split by another write. A write holds the loop for
a fraction of a second; one run is active at a time and a save happens when a
person closes a live view, so nothing else is waiting on it.
"""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import hmac
import struct
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from casa_browser.broker import BrokerError
from casa_browser.vault_store import Ambiguous, Field, Item, ItemNotFound, Store, StoreError

VAULT_ID = "builtin"
OPEN_FAILED = "casa-browser broker: the saved logins could not be opened"

_CATEGORY = "LOGIN"
_WRITER = "live_view"
_USERNAME = "username"
_PASSWORD = "password"
_WEBSITES = "websites"
_SEED = "one-time password"
_VERSION = "version"
_STORED_TYPE = {_USERNAME: "STRING", _PASSWORD: "CONCEALED"}
_SHOWN_TYPE = {_USERNAME: "Text", _PASSWORD: "Concealed"}

_DIGESTS = {"SHA1": "sha1", "SHA256": "sha256", "SHA512": "sha512"}


@dataclass(frozen=True)
class Website:
    url: str


@dataclass(frozen=True)
class LoginField:
    id: str
    title: str
    field_type: str  # 'Text', 'Concealed' or 'Totp'
    value: str = field(repr=False)
    section_id: str | None = None


@dataclass(frozen=True)
class LoginOverview:
    """A login as a listing shows it: no field, so no value."""
    id: str
    vault_id: str
    category: str
    title: str
    websites: tuple[Website, ...]


@dataclass(frozen=True)
class Login(LoginOverview):
    version: int
    fields: tuple[LoginField, ...]


class BuiltinVault:
    """The logins in one directory's store.

    The store is opened once, here. If that fails the object stays without a store
    and every operation answers `unavailable`; nothing is tried again, because the
    way out is a person correcting the key and restarting the app.
    """

    def __init__(self, directory: Path, env_key: str | None,
                 clock: Callable[[], float] = time.time):
        self._clock = clock
        self._store = _opened(Path(directory), env_key)
        if self._store is None:
            # One fixed line: the reason may name a path or quote a key.
            print(OPEN_FAILED, file=sys.stderr, flush=True)

    @property
    def available(self) -> bool:
        return self._store is not None

    async def resolve_vault_id(self) -> str:
        with self._calls():
            return VAULT_ID

    async def get(self, item_id: str) -> Login:
        with self._calls() as store:
            return _login(_exact(store, item_id))

    async def list_logins(self) -> list[LoginOverview]:
        with self._calls() as store:
            return [_overview(item) for item in store.list()]

    async def create_login(self, title, username, password, website_origins) -> Login:
        with self._calls(writing=True) as store:
            fields = [_stored(_USERNAME, username)]
            if password:
                fields.append(_stored(_PASSWORD, password))
            if website_origins:
                fields.append(Field(id="", label=_WEBSITES, type="URL",
                                    value="\n".join(website_origins)))
            fields.append(Field(id="", label=_VERSION, type="STRING", value="1"))
            return _login(store.create(title=title, category=_CATEGORY, tags=(),
                                       fields=fields, writer=_WRITER))

    async def update_fields(self, item_id, expected_version,
                            username=None, password=None) -> Login:
        with self._calls() as store:
            item = _exact(store, item_id)
            version = _version(item)
        if version != expected_version:
            raise BrokerError("conflict")
        values = {label: value
                  for label, value in ((_USERNAME, username), (_PASSWORD, password))
                  if value is not None}
        fields = []
        for stored in item.fields:
            if stored.label == _VERSION:
                stored = dataclasses.replace(stored, value=str(version + 1))
            elif stored.label in values:
                stored = dataclasses.replace(stored, value=values.pop(stored.label))
            fields.append(stored)
        fields.extend(_stored(label, value) for label, value in values.items())
        # If this write fails, the store forgets its copy in memory and reads the
        # file again, so the next read shows the version and values from before.
        with self._calls(writing=True) as store:
            return _login(store.replace(item.id, title=None, category=None, tags=None,
                                        fields=fields, writer=_WRITER))

    async def totp_code(self, item_id: str) -> str:
        with self._calls() as store:
            seed = _value(_exact(store, item_id), _SEED)
            if not seed:
                raise BrokerError("not_found")
            return one_time_code(seed, self._clock())

    async def vault_accessors(self) -> None:
        """Nobody but this app has access to its store, so there is nobody to list."""
        return None

    @contextlib.contextmanager
    def _calls(self, writing: bool = False) -> Iterator[Store]:
        """The store for one operation; whatever fails leaves as a fixed code."""
        if self._store is None:
            raise BrokerError("unavailable")
        try:
            yield self._store
        except BrokerError:
            raise
        except (ItemNotFound, Ambiguous):
            raise BrokerError("not_found") from None
        except StoreError:
            raise BrokerError("refused" if writing else "unavailable") from None
        except Exception:
            raise BrokerError("unavailable") from None


def _opened(directory: Path, env_key: str | None) -> Store | None:
    """The store in `directory`, or None when it cannot be opened."""
    # A key that was supplied but holds only whitespace is not passed on. The store
    # would trim it to nothing and then use the key file, or make a new key and a
    # new file: another key in place of the one somebody meant to supply.
    if env_key and not env_key.strip():
        return None
    try:
        return Store.open(directory, env_key)
    except Exception:
        return None


def _exact(store: Store, item_id: str) -> Item:
    """The item whose id is `item_id`.

    The store also finds an item by its title. An id the broker holds must never
    reach a login that merely happens to be called that.
    """
    item = store.get(item_id)
    if item.id != item_id:
        raise BrokerError("not_found")
    return item


def _stored(label: str, value: str) -> Field:
    return Field(id="", label=label, type=_STORED_TYPE[label], value=value)


def _value(item: Item, label: str) -> str:
    return next((stored.value for stored in item.fields if stored.label == label), "")


def _version(item: Item) -> int:
    return int(_value(item, _VERSION))


def _overview(item: Item) -> LoginOverview:
    return LoginOverview(id=item.id, vault_id=VAULT_ID, category="Login", title=item.title,
                         websites=_websites(item))


def _websites(item: Item) -> tuple[Website, ...]:
    return tuple(Website(url) for url in _value(item, _WEBSITES).splitlines() if url)


def _login(item: Item) -> Login:
    fields = []
    for stored in item.fields:
        if stored.label in _SHOWN_TYPE:
            fields.append(LoginField(id=stored.label, title=stored.label,
                                     field_type=_SHOWN_TYPE[stored.label], value=stored.value))
        elif stored.label == _SEED and stored.value:
            # The broker learns that a code can be made; the seed stays in the store.
            fields.append(LoginField(id=_SEED, title=_SEED, field_type="Totp", value=""))
    return Login(id=item.id, vault_id=VAULT_ID, category="Login", title=item.title,
                 websites=_websites(item), version=_version(item), fields=tuple(fields))


def one_time_code(seed: str, at: float) -> str:
    """The time-based code (RFC 6238) that `seed` gives at `at`, seconds since 1970.

    `seed` is a base-32 secret, where spaces and lower case are accepted and the
    padding may be missing, or an `otpauth://totp/...` address, whose `secret`,
    `digits` (6 to 8), `period` and `algorithm` (SHA1, SHA256, SHA512) are used.
    Without them a code has 6 digits, lasts 30 seconds and is made with SHA-1.
    A seed that cannot be read is `not_found`: there is no code to give.
    """
    try:
        secret, digits, period, digest = _seed_parts(seed)
        compact = "".join(secret.split()).upper().rstrip("=")
        key = base64.b32decode(compact + "=" * (-len(compact) % 8))
        if not key:
            raise ValueError
        mac = hmac.digest(key, struct.pack(">Q", int(at // period)), digest)
    except (ValueError, LookupError):
        raise BrokerError("not_found") from None
    # RFC 4226, 5.3: the last half-byte says where the 31-bit number starts.
    number = struct.unpack_from(">I", mac, mac[-1] & 0x0F)[0] & 0x7FFFFFFF
    return str(number % 10 ** digits).zfill(digits)


def _seed_parts(seed: str) -> tuple[str, int, int, str]:
    """The secret, the number of digits, the seconds a code lasts and the hash."""
    text = seed.strip()
    if not text.lower().startswith("otpauth:"):
        return text, 6, 30, _DIGESTS["SHA1"]
    address = urlsplit(text)
    if address.netloc.lower() != "totp":
        raise ValueError
    given = parse_qs(address.query)
    digits = int(given.get("digits", ["6"])[0])
    period = int(given.get("period", ["30"])[0])
    if not 6 <= digits <= 8 or period < 1:
        raise ValueError
    return (given["secret"][0], digits, period,
            _DIGESTS[given.get("algorithm", ["SHA1"])[0].upper()])
