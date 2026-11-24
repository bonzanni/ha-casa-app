"""The phone options (read once from env, exported by the app's run script)
and the effective settings: what the store holds, else the seed option, else
the default.

An unavailable store gives seeds and defaults for models and voices but every
grant ``off``: Casa cannot know whether the operator narrowed a grant, so
nobody is put through until the store is back. Calls are still answered and
messages taken.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from contacts import numbers as phone_numbers
from contacts import service as contacts_service
from phone import store as pstore

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, str] = {
    "voice_model": "gpt-live-1.1",
    "helper_model": "gpt-5.6-terra",
    "decision_model": "gpt-5.6-terra",
    "record_model": "gpt-5.6-terra",
}
DEFAULT_GRANTS: dict[tuple[str, str], str] = {
    ("priority", "transfer"): "open",
    ("known", "transfer"): "urgent",
    ("unknown", "transfer"): "emergency",
}
#: Ascending: a wider grant has a higher index.
LEVELS = ("off", "emergency", "urgent", "open")


@dataclass(frozen=True)
class Options:
    enabled: bool
    transfer_number: str
    owner_name: str
    seed_voices: dict[str, str]
    api_host: str


@dataclass(frozen=True)
class Effective:
    models: dict[str, str]
    voices: dict[str, str]
    grants: dict[tuple[str, str], str]


def _seed_voices(raw: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in raw.split(","):
        language, sep, voice = part.partition("=")
        language, voice = language.strip(), voice.strip()
        if sep and language and voice:
            found[language] = voice
    return found


_options: Options | None = None


def reset() -> None:
    """Forget the options read, so the next :func:`options` reads env again."""
    global _options
    _options = None


def options() -> Options:
    """The options, read from env on first use and kept: a transfer number
    that does not read as a number becomes ``""`` with one warning (the value
    itself is not logged), once."""
    global _options
    if _options is None:
        _options = _read()
    return _options


def _read() -> Options:
    env = os.environ
    transfer = env.get("PHONE_TRANSFER_NUMBER", "").strip()
    if transfer:
        normal = phone_numbers.to_e164(
            transfer, contacts_service.default_region())
        if normal is None:
            logger.warning("phone: phone_transfer_number is not a usable "
                           "number; nobody will be put through")
        transfer = normal or ""
    return Options(
        enabled=env.get("PHONE_ENABLED", "").strip().lower() == "true",
        transfer_number=transfer,
        owner_name=env.get("PHONE_OWNER_NAME", "").strip(),
        seed_voices=_seed_voices(env.get("LIVE_VOICES", "")),
        api_host=env.get("LIVE_API_HOST", "").strip())


def voice_param(value: str) -> str | dict:
    """The API's form of a voice: a ``voice_`` id as an object, a stock name
    as itself."""
    return {"id": value} if value.startswith("voice_") else value


def effective(store: pstore.PhoneStore | None) -> Effective:
    """Blocking. Never raises."""
    seeds = options().seed_voices
    models = dict(DEFAULTS)
    voices = dict(seeds)
    grants = {key: "off" for key in DEFAULT_GRANTS}
    if store is None or not store.available:
        return Effective(models, voices, grants)
    try:
        with store.transaction() as conn:
            stored_models = {k: pstore.get_setting(conn, k) for k in DEFAULTS}
            stored_voices = pstore.voices(conn)
            stored_grants = pstore.grants(conn)
    except Exception:  # noqa: BLE001 — unreadable means unavailable
        logger.warning("phone: settings not read from the store",
                       exc_info=True)
        return Effective(models, voices, grants)
    models.update({k: v for k, v in stored_models.items() if v})
    voices.update(stored_voices)
    grants = {**DEFAULT_GRANTS, **stored_grants}
    return Effective(models, voices, grants)
