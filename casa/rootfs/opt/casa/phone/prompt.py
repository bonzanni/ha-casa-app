"""What the three models of a call are told, and the fixed spoken lines.

The texts are files: shipped defaults under ``defaults/phone/prompts/``, which
the config reconciler keeps in step with ``/config/phone/prompts/``, where the
operator may edit them. An edited file wins; one that cannot be used (missing
a section, unreadable) is logged once and the shipped default is used instead.

``agent.md`` is the voice model's template followed by headed sections
(``## who: <class>``, ``## transfer: <level>``); code picks one of each for
the call. ``helper.md`` and ``decision.md`` are templates that use the same
two paragraphs, so a rule is worded in one place.

What a call learns about its caller is :class:`contacts.lookup.CallerInfo`'s
name, first name and language, and only for a known caller. Filling replaces
the known ``{placeholders}`` in one pass, so whatever a value contains (a
name with braces in it) is inserted as written and never filled in turn.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from contacts.lookup import CallerInfo
from phone.settings import Options

logger = logging.getLogger(__name__)

WHO = ("priority", "known", "unknown", "returned")
TRANSFER = ("off", "emergency", "urgent", "open")
LINE_KEYS = ("silence", "too_long", "transfer_failed", "check_in")
LANGUAGE_NAMES = {"nl": "Dutch", "it": "Italian", "en": "English"}
DEFAULT_LANGUAGE = "en"

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_SECTION = re.compile(r"^## (who|transfer): *([a-z_]+) *$")


class PromptError(Exception):
    """A prompt file is missing, unreadable or lacks a section."""


@dataclass(frozen=True)
class Context:
    assistant_name: str
    owner_name: str
    info: CallerInfo
    caller_class: str
    language: str
    transfer_level: str
    returned: bool


def _config_dir() -> Path:
    return Path(os.environ.get("CASA_CONFIG_DIR", "/config"))


def _defaults_dir() -> Path:
    found = os.environ.get("CASA_DEFAULTS_DIR")
    if found:
        return Path(found)
    return Path(__file__).resolve().parents[1] / "defaults"


def _override_path(name: str) -> Path:
    return _config_dir() / "phone" / "prompts" / name


def _default_path(name: str) -> Path:
    return _defaults_dir() / "phone" / "prompts" / name


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(f"{path.name}: {type(exc).__name__}") from None


def load(name: str) -> str:
    """The operator's copy of *name* when it exists, else the shipped one."""
    override = _override_path(name)
    if override.is_file():
        return _read(override)
    return _read(_default_path(name))


def parse(text: str) -> tuple[str, dict[str, str]]:
    """The template before the first section, and the sections keyed
    ``"who: known"``, ``"transfer: off"`` and so on. Raises
    :class:`PromptError` when any of them is missing or empty."""
    body: list[str] = []
    sections: dict[str, list[str]] = {}
    current = body
    for line in text.splitlines():
        match = _SECTION.match(line.strip())
        if match:
            current = sections.setdefault(f"{match[1]}: {match[2]}", [])
            continue
        current.append(line)
    found = {key: "\n".join(lines).strip() for key, lines in sections.items()}
    for kind, names in (("who", WHO), ("transfer", TRANSFER)):
        for name in names:
            if not found.get(f"{kind}: {name}"):
                raise PromptError(f"missing section '{kind}: {name}'")
    template = "\n".join(body).strip()
    if not template:
        raise PromptError("missing template")
    return template + "\n", found


#: The operator's copy of each file, parsed, keyed by its path, modification
#: time and size: a file is parsed, and a broken one warned about, once per
#: version, however many texts a call builds from it.
_parsed_overrides: dict[str, tuple[tuple, object, PromptError | None]] = {}


def _parsed(name: str, parser):
    """``parser(text)`` of the operator's copy of *name*; when that copy
    cannot be used, one warning per version of it and ``parser`` of the
    shipped default, whose own failure is raised."""
    override = _override_path(name)
    try:
        stat = override.stat()
    except OSError:
        stat = None
    if stat is not None:
        key = (str(override), stat.st_mtime_ns, stat.st_size)
        hit = _parsed_overrides.get(name)
        if hit is None or hit[0] != key:
            try:
                hit = (key, parser(_read(override)), None)
            except PromptError as exc:
                logger.warning("phone: the edited %s cannot be used (%s); "
                               "using the shipped one", name, exc)
                hit = (key, None, exc)
            _parsed_overrides[name] = hit
        if hit[2] is None:
            return hit[1]
    return parser(_read(_default_path(name)))


def fill(template: str, values: dict[str, str]) -> str:
    """Replace each ``{key}`` of *values* in one pass; any other brace text
    is left as it is."""
    return _PLACEHOLDER.sub(
        lambda m: values.get(m[1], m[0]), template)


def language_name(language: str) -> str:
    return LANGUAGE_NAMES.get(language, LANGUAGE_NAMES[DEFAULT_LANGUAGE])


def _who_key(ctx: Context) -> str:
    if ctx.returned:
        return "returned"
    if ctx.caller_class in ("priority", "known") and _names(ctx)[0]:
        return ctx.caller_class
    return "unknown"


def _names(ctx: Context) -> tuple[str, str]:
    """``(name, first_name)`` for a known caller; empty for anyone else."""
    if ctx.returned or ctx.caller_class not in ("priority", "known") \
            or not ctx.info.known:
        return "", ""
    name = ctx.info.name or ctx.info.first_name
    return name, ctx.info.first_name or name


def _values(ctx: Context, sections: dict[str, str]) -> dict[str, str]:
    name, first_name = _names(ctx)
    base = {"assistant_name": ctx.assistant_name,
            "owner_name": ctx.owner_name,
            "language_name": language_name(ctx.language)}
    level = "off" if ctx.returned else ctx.transfer_level
    if level not in TRANSFER:
        level = "off"
    who = fill(sections[f"who: {_who_key(ctx)}"],
               {**base, "name": name, "first_name": first_name})
    rule = fill(sections[f"transfer: {level}"], base)
    return {**base, "who": who, "transfer_rule": rule}


def instructions(ctx: Context) -> str:
    """The voice model's instructions for this call."""
    template, sections = _parsed("agent.md", parse)
    return fill(template, _values(ctx, sections))


def _plain(name: str, ctx: Context) -> str:
    values = _values(ctx, _parsed("agent.md", parse)[1])
    return fill(_parsed(name, lambda text: _nonempty(text, name)), values)


def _nonempty(text: str, name: str) -> str:
    if not text.strip():
        raise PromptError(f"{name} is empty")
    return text


def helper_instructions(ctx: Context) -> str:
    """The in-call helper's instructions (the model the session hands work
    to, which holds the abilities as tools)."""
    return _plain("helper.md", ctx)


def decision_instructions(ctx: Context) -> str:
    """The decision step's instructions."""
    return _plain("decision.md", ctx)


def _lines_in(text: str) -> dict[str, dict[str, str]]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise PromptError("lines.yaml is not YAML") from None
    if not isinstance(data, dict):
        raise PromptError("lines.yaml is not a mapping")
    found: dict[str, dict[str, str]] = {}
    for language, lines in data.items():
        if not isinstance(lines, dict):
            raise PromptError(f"lines.yaml: {language} is not a mapping")
        for key in LINE_KEYS:
            if not isinstance(lines.get(key), str) or not lines[key].strip():
                raise PromptError(f"lines.yaml: {language} lacks {key}")
        found[str(language)] = {key: lines[key].strip() for key in LINE_KEYS}
    if DEFAULT_LANGUAGE not in found:
        raise PromptError(f"lines.yaml lacks {DEFAULT_LANGUAGE}")
    return found


def spoken_lines(language: str, owner_name: str) -> dict[str, str]:
    """The fixed lines in *language*, or in English when it has none, with
    ``{owner_name}`` filled in."""
    found = _parsed("lines.yaml", _lines_in)
    lines = found.get(language) or found[DEFAULT_LANGUAGE]
    return {key: fill(text, {"owner_name": owner_name})
            for key, text in lines.items()}


def assistant_name() -> str:
    """The assistant's name from its character file, else a plain word."""
    path = _config_dir() / "agents" / "assistant" / "character.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return "the assistant"
    name = data.get("name") if isinstance(data, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() \
        else "the assistant"


def owner_name(options: Options) -> str:
    return options.owner_name or "the person you called"
