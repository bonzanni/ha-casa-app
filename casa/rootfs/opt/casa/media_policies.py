"""Canonical per-kind media policy table (v0.73.0, spec §3.1).

The ONE type-specific surface for the ``send_media`` capability: the JSON-schema
``kind`` enum, argument validation, the magic gate, the extension allowlist, the
size cap, and the PTB send-method dispatch ALL derive from ``MEDIA_POLICIES``.

Dependency-neutral by design: this module imports only the stdlib so BOTH
``tools.py`` and ``channels/telegram.py`` can import it without a cycle
(``tools`` already imports ``channels``; putting the table in ``tools`` would
make a ``channels.telegram`` import of it cycle).

Every ``accepts(content)`` predicate is TOTAL OVER CONTENT: every admitted
``bytes`` value — empty, short, malformed, undecodable — yields a bool rather
than an exception. Slicing/``startswith`` are inherently safe; every index
access is length-guarded; every raising call is caught.

Totality is a claim about CONTENT, not about the machine. A process-level
failure (``MemoryError`` being the realistic one) is deliberately NOT caught
here and propagates: `capture`'s taxonomy separates a content verdict
(``magic_mismatch``) from an inability to reach one, and swallowing an
allocation failure into ``False`` would report "these bytes are not valid
<kind>" about bytes that may be perfectly valid — claiming more than the
evidence supports, the same error ``DeliveryOutcome`` exists to avoid. Such a
failure surfaces as ``internal_error`` with a logged traceback (``tools.py``),
and the claim is still cleaned up.

``accepts`` is handed the WHOLE captured buffer, not a head: ``plugin_outbox``
calls it after both the ``fstat`` check and ``_read_capped`` have enforced the
kind's ``size_cap``, so the cost of a whole-buffer predicate is bounded by that
cap and the bytes are already resident. Most kinds only look at the first few —
a magic signature is a head test — but ``text`` has no signature and could not
exist under a head-only contract (#565).
"""
from __future__ import annotations

import re
from typing import Callable, NamedTuple

_MB = 1024 * 1024


class MediaPolicy(NamedTuple):
    ptb_method: str                     # Bot.<method> to dispatch to
    accepts: Callable[[bytes], bool]    # TOTAL predicate over the WHOLE content
    extensions: frozenset[str]          # lower-cased, dot-prefixed allowlist
    size_cap: int                       # bytes


def _accepts_pdf(head: bytes) -> bool:
    return head.startswith(b"%PDF-")


def _accepts_photo(head: bytes) -> bool:
    # JPEG SOI (FF D8 FF) or the 8-byte PNG signature.
    return head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n"


def _accepts_mp3(head: bytes) -> bool:
    # ID3v2 tag, or a VALIDATED MPEG Layer-III frame header. The bare 11-bit
    # sync mask (FF Ex) is too broad — it also matches ADTS AAC (FF F1) and
    # MPEG Layer I/II, and would index past short input. Require, in order:
    if head[:3] == b"ID3":
        return True
    if len(head) < 4:
        return False
    if head[0] != 0xFF:
        return False
    b1, b2 = head[1], head[2]
    if (b1 & 0xE0) != 0xE0:        # 11-bit frame sync
        return False
    if (b1 & 0x18) == 0x08:        # MPEG version ID: reserved -> reject
        return False
    if (b1 & 0x06) != 0x02:        # Layer III only (rejects AAC's reserved 00 + Layer I/II)
        return False
    if (b2 & 0xF0) in (0x00, 0xF0):  # bitrate index: free/bad -> reject
        return False
    if (b2 & 0x0C) == 0x0C:        # sample-rate index: reserved -> reject
        return False
    return True


def _accepts_ogg_opus(head: bytes) -> bool:
    # Ogg container whose first page carries the Opus ID header (RFC 7845: the
    # OpusHead magic sits in the first packet, alone on the first Ogg page).
    return head[:4] == b"OggS" and b"OpusHead" in head[:64]


# ZIP fixed-size record headers (APPNOTE 4.3.7 / 4.3.16): a local file header
# is 30 bytes before its variable-length name, an end-of-central-directory
# record is 22 bytes before its variable-length comment. Requiring the floor
# turns a truncated 4-byte signature stub into a refusal instead of a delivered
# fragment — free, because the whole buffer is already resident. These are
# sanity floors, not structural validation: this table is a magic gate, never a
# parser (the full minima are 30 + name + extra, and 22 + comment).
_ZIP_LOCAL_HEADER_MIN = 30
_ZIP_EOCD_MIN = 22


def _accepts_zip(content: bytes) -> bool:
    # PK\x03\x04 = local file header (an archive with at least one member);
    # PK\x05\x06 = a bare EOCD, i.e. the legitimately empty archive.
    # DELIBERATELY refused, though a ZIP reader would open them: PK\x07\x08
    # (spanned — the delivered volume is incomplete without the others), an SFX
    # stub (starts `MZ`), a shebang-prefixed archive, and the ZIP64-form empty
    # archive (PK\x06\x06). This kind is conventional single-file archives; an
    # executable or script prefix is not that.
    if content.startswith(b"PK\x03\x04"):
        return len(content) >= _ZIP_LOCAL_HEADER_MIN
    if content.startswith(b"PK\x05\x06"):
        return len(content) >= _ZIP_EOCD_MIN
    return False


# Control characters refused by the `text` kind: all of C0 except TAB (09),
# LF (0A) and CR (0D), plus DEL (7F) and the C1 block (80-9F). Compiled once —
# a per-character `unicodedata.category` loop over a 5 MB artifact is seconds
# of Python; this is one C-level scan. A BOM (U+FEFF) is Cf, not Cc, so it
# passes and is delivered verbatim: `capture` returns exactly what it read.
#
# This refuses ESC, so an ANSI-coloured diagnostic dump does not qualify and
# its producer must strip the colour. That is the narrow reading, taken
# deliberately and for an asymmetric reason: ADMITTING a control byte later is
# backward-compatible with every producer, while WITHDRAWING one breaks
# whatever had come to rely on it. Start strict; relax on a real producer.
_TEXT_FORBIDDEN_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _accepts_text(content: bytes) -> bool:
    # #565: whole-buffer validation of ENCODING, never of STRUCTURE — see the
    # table entry. Empty is refused rather than delivered as a zero-byte
    # document. The strict decode also excludes lone surrogates, truncated
    # sequences and overlong forms. `UnicodeDecodeError` is the only
    # content-induced exception either call can raise: the codec name is a
    # fixed built-in (no `LookupError`), the pattern is a constant that
    # compiles at import (no `re.error`), and the subject is always `bytes`
    # from `_read_capped` (no `TypeError`).
    if not content:
        return False
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return _TEXT_FORBIDDEN_CONTROLS.search(text) is None


# Insertion order IS the JSON-schema `kind` enum order (`tools.py`), so new
# kinds are APPENDED and the existing four keep their positions.
MEDIA_POLICIES: dict[str, MediaPolicy] = {
    # `document` means PDF, and keeps that name deliberately. `zip` and `text`
    # dispatch to the same `send_document`, which makes the NAME a misnomer for
    # the KIND — but the name is the tool's public `kind` enum, so renaming it
    # breaks every caller. Do NOT "fix" this by widening `document` into a
    # generic catch-all: the `%PDF-` gate is the reason the kind exists, and a
    # catch-all dissolves it (#565, #482).
    "document": MediaPolicy("send_document", _accepts_pdf,
                            frozenset({".pdf"}), 20 * _MB),
    "photo": MediaPolicy("send_photo", _accepts_photo,
                         frozenset({".jpg", ".jpeg", ".png"}), 10 * _MB),
    "audio": MediaPolicy("send_audio", _accepts_mp3,
                         frozenset({".mp3"}), 20 * _MB),
    "voice": MediaPolicy("send_voice", _accepts_ogg_opus,
                         frozenset({".ogg", ".oga"}), 20 * _MB),
    # #482. The `.zip`-only extension list — NOT the predicate — is what
    # confines this kind: `PK\x03\x04` is equally the signature of every
    # OOXML/ODF document, `.jar`, `.apk` and `.epub`. That is a deliberate
    # choice; widening the extensions admits all of them.
    "zip": MediaPolicy("send_document", _accepts_zip,
                       frozenset({".zip"}), 20 * _MB),
    # #565. Validates ENCODING, never STRUCTURE — a malformed `.json` or
    # `.yaml` passes, and this kind must never be read as a parser. Those
    # extensions are included anyway because Casa's own producers emit them and
    # the alternative is renaming a JSON export to `.txt`. No new capability
    # boundary: text can already leave Casa as a chat message, so a format
    # constraint is strictly narrowing relative to what a message may carry.
    # Cap is 5 MB, not the 20 MB of the other `send_document` kinds — text is
    # decoded and scanned in full, and 5 MB is already an enormous text
    # artifact (~1200x a Telegram message).
    "text": MediaPolicy("send_document", _accepts_text,
                        frozenset({".txt", ".md", ".csv", ".log",
                                   ".json", ".yaml", ".yml"}), 5 * _MB),
}
