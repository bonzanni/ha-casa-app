"""The operator pages' text in the effective locale's language (Plan 1 Task 14).

Every string the ingress pages and the live view show the operator lives in one catalog per
language, ``locales/<language>.json`` beside this module: English (``en``) is complete by
definition, and every other catalog must hold exactly the same keys with the same
placeholders (``test_i18n``). The language is the effective locale's (Home Assistant's
configured language, or the ``language`` option; Task 2), reduced to its primary subtag; a
language without a catalog is shown in English, and ``lang`` then says ``en``.

What is translated is the app's own copy. What a page shows from elsewhere stays as it came:
a site's name, an item's description (the model's words, E-C3-2 = A), the step log's and
the notices' text (also the assistant's data, so one language), an event's kind and outcome.
"""
from __future__ import annotations

import html
import json
import re
import string
from functools import lru_cache
from pathlib import Path

LOCALES = Path(__file__).resolve().parent / "locales"
BASE = "en"
_TAG = re.compile(r"[A-Za-z]{2,3}")


@lru_cache(maxsize=None)
def catalog(language: str) -> dict[str, str]:
    """One language's catalog (``{}`` when there is none)."""
    if not _TAG.fullmatch(language or ""):
        return {}
    try:
        data = json.loads((LOCALES / f"{language}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def available() -> tuple[str, ...]:
    return tuple(sorted(p.stem for p in LOCALES.glob("*.json") if _TAG.fullmatch(p.stem)))


def language_for(tag: str | None) -> str:
    """The catalog language for a locale tag such as ``it-IT`` or ``nl``; else English."""
    primary = (tag or "").replace("_", "-").split("-")[0].lower()
    return primary if primary != BASE and catalog(primary) else BASE


def placeholders(template: str) -> frozenset[str]:
    return frozenset(name for _, name, _, _ in string.Formatter().parse(template) if name)


class Text:
    """``t(key, **values)`` is plain text (the caller escapes it); ``t.html(key, **values)`` is
    the escaped template with each value inserted as given (the caller escapes the values)."""

    def __init__(self, tag: str | None):
        self.language = language_for(tag)
        self._own = catalog(self.language)
        self._base = catalog(BASE)

    def _template(self, key: str) -> str:
        return self._own.get(key) or self._base[key]

    def __call__(self, key: str, **values) -> str:
        return self._template(key).format(**values)

    def html(self, key: str, **values_html) -> str:
        return html.escape(self._template(key), quote=True).format(**values_html)

    def table(self, keys) -> dict[str, str]:
        """Plain-text strings for a page's script (``live.js``)."""
        return {k: self._template(k) for k in keys}
