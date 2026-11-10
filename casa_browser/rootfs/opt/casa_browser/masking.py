"""Masking of observation text (Plan 1 Task 7; design §5.1, the second layer behind the
page-view gate).

``mask_text`` replaces, mechanically and without judging meaning:

* strings in a known credential format (cloud and service keys, tokens, JWTs, private-key
  blocks, ``Bearer`` values) and any token of 24 or more characters that mixes letters and
  digits with a Shannon entropy above 3.5 bits per character, with ``[masked]``;
* every address (``scheme://…``, and for the special schemes every form the URL standard
  accepts: any run of slashes or backslashes after the colon, or none; up to whitespace) with
  its origin and an opaque reference (``⟨r1⟩``) — or ``link`` when no host is certain (fewer
  than two separators, or ``file:``) or it is not plainly a host — so no path, query, fragment
  or user info — where a bearer token can sit — reaches a model. The same address in one text
  keeps one reference. A value the page splits (across layout boxes, or by whitespace inside
  an address) is judged piece by piece: a documented residual.

With ``keep_addresses`` (a plain fetch's answer, which carries no cookie: a reader has no other way
to follow a link) an address stays whole, the known formats are masked in place, and a token is
judged between an address's separators, so a path with a word and a number stays an address. A site
that recognises the requester some other way (its source address, a token in the address) can still
show a private link here: a documented residual.

It is a property of the string, not a classifier: a short secret such as an 8-character
recovery code is not caught (``test_short_recovery_code_residual``); only pages that belong
to the session are observed at all, which is the first layer.
"""
from __future__ import annotations

import hashlib
import math
import re
import sys
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit

MASK = "[masked]"
MAX_TEXT = 20_000      # output
MAX_INPUT = 400_000    # read, whole words, before masking; the output is cut after
MIN_RANDOM = 24
ENTROPY_BITS = 3.5

_KNOWN = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),       # JWT
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),                                       # AWS
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),                         # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),                                       # Slack
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}\b"),                      # Stripe
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),                                             # Google
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),                               # OpenAI, Anthropic
    re.compile(r"\bops_[A-Za-z0-9_-]{20,}"),                                             # 1Password SA
]
# An address, wherever it starts, up to whitespace, recognised as the URL standard (WHATWG)
# parses one: a special scheme (http, https, ws, wss, ftp, file) followed by any run of `/` and
# `\` — or none (`https:site.example`); any other scheme (RFC 3986 grammar, its length bounded
# so matching stays linear in the text) followed by `//`.
_SPECIAL = r"(?:https?|wss?|ftp|file)"
_URL = re.compile(r"%s:[\\/]*[^\s\\/]\S*|[a-z][a-z0-9+.-]{0,31}://\S+" % _SPECIAL, re.I)
_SPECIAL_PREFIX = re.compile(r"(%s):[\\/]*" % _SPECIAL, re.I)
_HOST = re.compile(r"[a-z0-9.-]+|[0-9a-f:.]+")  # what an origin may show of a host
_TOKEN = re.compile(r"[A-Za-z0-9_+/=.~-]{%d,}" % MIN_RANDOM)
_TOKEN_IN_ADDRESS = re.compile(r"[A-Za-z0-9+]{%d,}" % MIN_RANDOM)


@dataclass(frozen=True)
class MaskedText:
    text: str
    spans: tuple[tuple[int, int], ...]  # masked regions, in the original text
    replacements: tuple[str, ...] = ()  # what took each region's place, in step with ``spans``


def looks_random(token: str) -> bool:
    if len(token) < MIN_RANDOM:
        return False
    has_alpha = any(c.isalpha() for c in token)
    has_digit = any(c.isdigit() for c in token)
    if not (has_alpha and has_digit):
        return False
    counts = Counter(token)
    entropy = -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())
    return entropy > ENTROPY_BITS


def _origin(url: str) -> str:
    """Scheme, host and port only — and only when the host is plainly a host name or address:
    anything else (a backslash path a browser would read as a path, an encoded or unusual
    host) shows as ``link``, never as part of the address."""
    special = _SPECIAL_PREFIX.match(url)
    if special:
        # The standard's leniency: any run of separators, or none, after the scheme. Only a
        # run of two or more names a host for certain (with fewer, or under file:, the first
        # segment can be a path of the page itself), so only then is a host shown.
        if special.group(1).lower() == "file" or special.end() - len(special.group(1)) - 1 < 2:
            return "link"
        url = special.group(1) + "://" + url[special.end():]
    try:
        parts = urlsplit(url.replace("\\", "/"))
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "link"
    if not _HOST.fullmatch(host):
        return "link"
    return f"{parts.scheme.lower()}://{f'[{host}]' if ':' in host else host}{port}"


def mask_text(text: str, *, limit: int = MAX_TEXT, keep_addresses: bool = False) -> MaskedText:
    """Masks the whole text first and cuts the result to ``limit`` afterwards, so a cut never
    leaves part of a value that would have been recognised whole."""
    if not isinstance(text, str):
        text = ""
    if len(text) > MAX_INPUT:
        j = MAX_INPUT
        while j and not text[j - 1].isspace():
            j -= 1                                   # drop the word the cut would split
        text = text[:j]
    regions: list[tuple[int, int, str]] = []
    refs: dict[str, str] = {}

    def claim(start: int, end: int, replacement: str) -> None:
        i = bisect_left(regions, (start,))    # regions stay sorted and apart: two neighbours decide
        if (i and regions[i - 1][1] > start) or (i < len(regions) and regions[i][0] < end):
            return
        regions.insert(i, (start, end, replacement))

    for m in () if keep_addresses else _URL.finditer(text):
        url = m.group(0).rstrip(".,;:!?)]}'\"`>")
        if text.startswith(" ⟨r", m.start() + len(url)) and url == _origin(url):
            continue  # already an origin and its reference
        if url not in refs:
            refs[url] = f"⟨r{len(refs) + 1}⟩"
        claim(m.start(), m.start() + len(url), f"{_origin(url)} {refs[url]}")
    # Addresses first: a credential inside an address goes with the whole address (reduced to
    # its origin), never instead of it. Known formats and random tokens are masked outside.
    for pattern in _KNOWN:
        for m in pattern.finditer(text):
            claim(m.start(), m.end(), MASK)
    for m in (_TOKEN_IN_ADDRESS if keep_addresses else _TOKEN).finditer(text):
        if looks_random(m.group(0)):
            claim(m.start(), m.end(), MASK)
    out, cursor = [], 0
    for start, end, replacement in regions:
        out.append(text[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(text[cursor:])
    return MaskedText("".join(out)[:limit], tuple((s, e) for s, e, _ in regions),
                      tuple(r for _s, _e, r in regions))


def mask_whole(text: str, *, keep_addresses: bool = False) -> tuple[str, bool, bool]:
    """The whole text masked with no output limit (masking can lengthen it): the masked text,
    whether the input limit left it whole, and whether anything was replaced."""
    got = mask_text(text, limit=sys.maxsize, keep_addresses=keep_addresses)
    return got.text, len(text) <= MAX_INPUT, bool(got.spans)


def reference(url: str) -> str:
    """A stable opaque reference for an address (no part of it recoverable)."""
    return "r" + hashlib.sha256(url.encode("utf-8", "replace")).hexdigest()[:10]
