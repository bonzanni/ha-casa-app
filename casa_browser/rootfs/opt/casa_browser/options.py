"""App options, the effective locale and root bootstrap.

Only root bootstrap (the ``init-browser-config`` oneshot, ``python -m
casa_browser.options``) reads ``/data/options.json``. It splits the options into
``PublicOptions`` (no secrets; written to controller-private state) and ``BrokerOptions``
(the 1Password values; written only to the broker's runtime directory, ``0600`` broker),
reads Home Assistant's configured language through the Core API proxy with
``SUPERVISOR_TOKEN``, writes the effective locale, and then removes the token from the s6
container environment so no service started afterwards can inherit it.

Nothing in this module puts a secret in a ``repr``, an exception message, a log line or an
argv: validation errors name the option key, never its value.
"""
from __future__ import annotations

import json
import os
import shutil
import re
import sys
import tempfile
import urllib.error
import urllib.request
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .protocol import EffectiveLocale

# Fixed paths (production). Tests pass their own Paths.
DATA_ROOT = Path("/data")
RUN_ROOT = Path("/run/casa-browser")
CONTAINER_ENV = Path("/run/s6/container_environment")

HA_CONFIG_URL = "http://supervisor/core/api/config"
TOKEN_ENV_NAMES = ("SUPERVISOR_TOKEN", "HASSIO_TOKEN")

# Service identities (created in the Dockerfile). Task 5's firewall matches these UIDs.
SERVICE_USERS = {
    "browser": "casa-browser",
    "proxy": "casa-proxy",
    "broker": "casa-broker",
    "controller": "casa-controller",
    "proxy": "casa-proxy",
}

LOG_LEVELS = ("debug", "info", "warning", "error")
DEFAULT_VAULT = "Casa Browser"
DEFAULT_RETENTION_DAYS = 30
_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8}){0,3}")
# The 1Password account's own sign-in host (``<team>.1password.<region>``): the proxy admits it
# for the extension and the broker. ``my.`` is the generic region address and names no account.
_ACCOUNT_HOST = re.compile(r"(?!my\.)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.1password\.(?:eu|com|ca)")
# Variables a service child process may inherit; everything else is dropped.
CHILD_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TZ")


class OptionsError(ValueError):
    """An invalid option. ``key`` names it; the value never appears."""

    def __init__(self, key: str):
        super().__init__(f"invalid option: {key}")
        self.key = key


@dataclass(frozen=True)
class Paths:
    data: Path = DATA_ROOT
    run: Path = RUN_ROOT
    container_env: Path = CONTAINER_ENV

    @property
    def options(self) -> Path:
        return self.data / "options.json"

    @property
    def state(self) -> Path:
        return self.data / "state"

    @property
    def profiles(self) -> Path:
        return self.data / "profiles"

    @property
    def artifacts(self) -> Path:
        return self.data / "artifacts"

    @property
    def downloads(self) -> Path:
        """Where Chrome puts a file it downloads (Task 10): written by the browser user, read
        and removed by the controller, emptied at every start, excluded from backups."""
        return self.data / "downloads"

    @property
    def broker_state(self) -> Path:
        return self.data / "broker"

    @property
    def proxy_state(self) -> Path:
        return self.data / "proxy"

    @property
    def broker_dir(self) -> Path:
        return self.run / "broker"

    @property
    def proxy_private(self) -> Path:
        """The proxy's own run directory (0700): its CA key and the account host."""
        return self.run / "proxy-private"

    @property
    def broker_config(self) -> Path:
        return self.broker_dir / "broker.json"

    @property
    def public_options(self) -> Path:
        return self.state / "public_options.json"

    @property
    def locale_record(self) -> Path:
        return self.state / "effective_locale.json"

    @property
    def database(self) -> Path:
        return self.state / "casa_browser.db"


@dataclass(frozen=True)
class PublicOptions:
    language: str | None = None
    timezone: str | None = None
    retention_days: int = DEFAULT_RETENTION_DAYS
    log_level: str = "info"


@dataclass(frozen=True)
class BrokerOptions:
    op_token: str = field(repr=False)
    vault: str = DEFAULT_VAULT
    guest_sign_in_address: str = field(default="", repr=False)
    guest_email: str = field(default="", repr=False)
    guest_password: str = field(default="", repr=False)
    guest_secret_key: str = field(default="", repr=False)

    @property
    def configured(self) -> bool:
        return all((self.op_token, self.vault, self.guest_sign_in_address, self.guest_email,
                    self.guest_password, self.guest_secret_key))

    def to_private_json(self) -> dict:
        """Only for the broker's own config file."""
        return {"op_token": self.op_token, "vault": self.vault,
                "guest_sign_in_address": self.guest_sign_in_address,
                "guest_email": self.guest_email, "guest_password": self.guest_password,
                "guest_secret_key": self.guest_secret_key}


def valid_language(value: object) -> bool:
    return isinstance(value, str) and _LANGUAGE.fullmatch(value) is not None


def valid_timezone(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 64 or value.startswith("/") \
            or ".." in value:
        return False
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _opt_str(raw: Mapping, key: str, default: str = "") -> str:
    value = raw.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > 4096:
        raise OptionsError(key)
    return value


def account_host(value: str) -> str:
    """The sign-in address as one exact account host (``https://`` and a trailing ``/``
    tolerated), ``""`` when unset; ``OptionsError`` for any other shape."""
    text = value.strip()
    if text.lower().startswith("https://"):
        text = text[len("https://"):]
    if text.endswith("/"):
        text = text[:-1]
    text = text.lower()
    if text and not _ACCOUNT_HOST.fullmatch(text):
        raise OptionsError("guest_sign_in_address")
    return text


def parse_options(raw: Mapping) -> tuple[PublicOptions, BrokerOptions]:
    if not isinstance(raw, Mapping):
        raise OptionsError("options")
    language = _opt_str(raw, "language") or None
    if language is not None and not valid_language(language):
        raise OptionsError("language")
    tz = _opt_str(raw, "timezone") or None
    if tz is not None and not valid_timezone(tz):
        raise OptionsError("timezone")
    days = raw.get("retention_days", DEFAULT_RETENTION_DAYS)
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 3650:
        raise OptionsError("retention_days")
    level = _opt_str(raw, "log_level", "info") or "info"
    if level not in LOG_LEVELS:
        raise OptionsError("log_level")
    public = PublicOptions(language=language, timezone=tz, retention_days=days, log_level=level)
    broker = BrokerOptions(
        op_token=_opt_str(raw, "onepassword_service_account_token"),
        vault=_opt_str(raw, "onepassword_vault", DEFAULT_VAULT) or DEFAULT_VAULT,
        guest_sign_in_address=account_host(_opt_str(raw, "guest_sign_in_address")),
        guest_email=_opt_str(raw, "guest_email"),
        guest_password=_opt_str(raw, "guest_password"),
        guest_secret_key=_opt_str(raw, "guest_secret_key"),
    )
    return public, broker


def load_options(path: Path) -> tuple[PublicOptions, BrokerOptions]:
    """Root bootstrap only."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise OptionsError("options") from None
    return parse_options(raw)


Fetch = Callable[[str], tuple[int, bytes]]


def read_ha_language(fetch: Fetch) -> str | None:
    """HA's configured language from ``GET /core/api/config`` (Core's ``config.as_dict()``
    carries ``language``). Any failure — transport error, non-200, non-JSON, a missing or
    malformed ``language`` — is ``None``: the caller holds runs rather than guess."""
    try:
        status, body = fetch(HA_CONFIG_URL)
    except Exception:  # noqa: BLE001 — any transport failure means "unknown"
        return None
    if status != 200:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    language = data.get("language") if isinstance(data, dict) else None
    return language if valid_language(language) else None


def supervisor_fetch(token: str, timeout: float = 10.0) -> Fetch:
    """The production fetch: Bearer token in a header (never argv), fixed timeout."""

    def fetch(url: str) -> tuple[int, bytes]:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return response.status, response.read(1_000_000)
        except urllib.error.HTTPError as err:
            return err.code, b""

    return fetch


def effective_locale(options: PublicOptions, ha_language: str | None,
                     tz_env: str | None) -> EffectiveLocale | None:
    """Option wins; otherwise HA's language and Supervisor's ``TZ``. ``None`` (the app
    reports ``needs_locale`` and holds runs) when no language or no valid time zone is
    known — never a silent ``en-US``."""
    if options.language:
        language, source = options.language, "option"
    elif ha_language and valid_language(ha_language):
        language, source = ha_language, "home_assistant"
    else:
        return None
    tz = options.timezone or (tz_env if valid_timezone(tz_env) else None)
    if not tz:
        return None
    return EffectiveLocale(language=language.replace("_", "-"), timezone=tz,
                           language_source=source)


def child_environment(base: Mapping[str, str], extra: Mapping[str, str] | None = None) -> dict:
    """The environment a service may pass to a child process: the allowlist only."""
    env = {k: base[k] for k in CHILD_ENV_ALLOWLIST if k in base}
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Root bootstrap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Owners:
    """uid/gid per role; ``None`` means "do not chown" (tests run unprivileged)."""

    browser: tuple[int, int] | None = None
    controller: tuple[int, int] | None = None
    broker: tuple[int, int] | None = None
    proxy: tuple[int, int] | None = None

    @classmethod
    def from_system(cls) -> "Owners":
        import pwd

        def ids(role: str) -> tuple[int, int]:
            entry = pwd.getpwnam(SERVICE_USERS[role])
            return entry.pw_uid, entry.pw_gid

        return cls(browser=ids("browser"), controller=ids("controller"), broker=ids("broker"),
                   proxy=ids("proxy"))


def _ensure_dir(path: Path, mode: int, owner: tuple[int, int] | None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OptionsError("data layout")
    if owner is not None:
        os.chown(path, *owner)
    os.chmod(path, mode)


def _empty_dir(path: Path) -> None:
    """Remove every entry of ``path`` (a symlink itself, never its target)."""
    for entry in path.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def _write_private(path: Path, payload: dict, owner: tuple[int, int] | None) -> None:
    """Atomic ``0600`` write: temp file in the same directory, chown, fsync, rename."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        if owner is not None:
            os.fchown(fd, *owner)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def scrub_container_environment(container_env: Path) -> list[str]:
    """Remove the Supervisor token from the s6 container environment, so ``with-contenv``
    can never hand it to a service. Returns the names removed."""
    removed = []
    for name in TOKEN_ENV_NAMES:
        target = container_env / name
        try:
            target.unlink()
            removed.append(name)
        except FileNotFoundError:
            pass
    return removed


@dataclass(frozen=True)
class BootstrapResult:
    configured: bool
    locale: EffectiveLocale | None
    options_error: str | None


def bootstrap(env: Mapping[str, str], paths: Paths, owners: Owners,
              fetch_factory: Callable[[str], Fetch] = supervisor_fetch,
              log: Callable[[str], None] = lambda line: print(line, file=sys.stderr)) -> BootstrapResult:
    """Run once as root at every app start, before any service. Idempotent."""
    token = env.get("SUPERVISOR_TOKEN") or env.get("HASSIO_TOKEN") or ""
    try:
        if paths.options.exists() and not paths.options.is_symlink():
            os.chmod(paths.options, 0o600)
        _ensure_dir(paths.state, 0o700, owners.controller)
        _ensure_dir(paths.artifacts, 0o700, owners.controller)
        _ensure_dir(paths.profiles, 0o700, owners.browser)
        # setgid: Chrome's files take the controller's group, so the controller can read and
        # remove them; nobody else can enter. A file left by an earlier start is removed.
        shared = (None if owners.browser is None or owners.controller is None
                  else (owners.browser[0], owners.controller[1]))
        _ensure_dir(paths.downloads, 0o2770, shared)
        _empty_dir(paths.downloads)
        _ensure_dir(paths.broker_state, 0o700, owners.broker)
        _ensure_dir(paths.proxy_state, 0o700, owners.proxy)
        _ensure_dir(paths.run, 0o755, None)
        _ensure_dir(paths.broker_dir, 0o700, owners.broker)
        _ensure_dir(paths.proxy_private, 0o700, owners.proxy)

        options_error = None
        try:
            if paths.options.is_symlink():
                raise OptionsError("options")
            public, broker = load_options(paths.options)
        except OptionsError as err:
            options_error = err.key
            public, broker = PublicOptions(), BrokerOptions(op_token="")

        ha_language = None
        if public.language is None and token:
            ha_language = read_ha_language(fetch_factory(token))
        locale = effective_locale(public, ha_language, env.get("TZ"))

        _write_private(paths.broker_config, broker.to_private_json(), owners.broker)
        _write_private(paths.proxy_private / "hosts.json",
                       {"onepassword_account_host": broker.guest_sign_in_address or None},
                       owners.proxy)
        _write_private(paths.public_options,
                       {"language": public.language, "timezone": public.timezone,
                        "retention_days": public.retention_days, "log_level": public.log_level,
                        "configured": broker.configured, "options_error": options_error},
                       owners.controller)
        if locale is None:
            record = {"needs_locale": True}
        else:
            record = {"needs_locale": False, "language": locale.language,
                      "timezone": locale.timezone, "language_source": locale.language_source}
        _write_private(paths.locale_record, record, owners.controller)
    finally:
        # Whatever happened above, no service may inherit the token.
        scrub_container_environment(paths.container_env)

    if options_error:
        log(f"casa-browser: option '{options_error}' is invalid; fix it in the app configuration")
    if locale is None:
        log("casa-browser: browser language unknown (Home Assistant's could not be read and no "
            "language option is set); runs are held until it is known")
    else:
        log(f"casa-browser: browser language {locale.language} ({locale.language_source}), "
            f"time zone {locale.timezone}")
    return BootstrapResult(configured=broker.configured, locale=locale, options_error=options_error)


def read_locale_record(paths: Paths) -> EffectiveLocale | None:
    """The controller's view of the effective locale (``None`` = needs_locale)."""
    try:
        data = json.loads(paths.locale_record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("needs_locale", True):
        return None
    return EffectiveLocale(data["language"], data["timezone"], data["language_source"])


def main() -> int:  # pragma: no cover — exercised by the image's init oneshot
    result = bootstrap(dict(os.environ), Paths(), Owners.from_system())
    os.environ.pop("SUPERVISOR_TOKEN", None)
    os.environ.pop("HASSIO_TOKEN", None)
    del result  # an invalid option is reported in the log and on the setup page
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
