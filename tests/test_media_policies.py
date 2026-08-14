"""MEDIA_POLICIES magic predicates — per-kind acceptance + totality (v0.73.0)."""
from __future__ import annotations

import pytest

from media_policies import MEDIA_POLICIES, MediaPolicy

pytestmark = pytest.mark.unit

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
ID3_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00rest"
# Valid MPEG-1 Layer III frame: FF FB 90 64 (sync, layer III, bitrate ok, 44.1k)
LAYER3_MP3 = b"\xff\xfb\x90\x64" + b"\x00" * 60
ADTS_AAC = b"\xff\xf1\x50\x80\x00\x1f\xfc"          # FF F1 — reserved layer (00)
LAYER2_MP3 = b"\xff\xfd\x90\x64" + b"\x00" * 60      # FF FD — Layer II
OGG_OPUS = b"OggS\x00\x02" + b"\x00" * 22 + b"\x01OpusHead\x01\x02"
OGG_VORBIS = b"OggS\x00\x02" + b"\x00" * 22 + b"\x01vorbis\x00\x00"


def test_table_shape():
    assert list(MEDIA_POLICIES) == ["document", "photo", "audio", "voice",
                                    "zip", "text"]
    for k, p in MEDIA_POLICIES.items():
        assert isinstance(p, MediaPolicy)
        assert p.ptb_method == {"document": "send_document", "photo": "send_photo",
                                "audio": "send_audio", "voice": "send_voice",
                                "zip": "send_document", "text": "send_document"}[k]
        assert p.size_cap >= 5 * 1024 * 1024
        assert all(e == e.lower() and e.startswith(".") for e in p.extensions)


@pytest.mark.parametrize("head", [b"", b"%", b"\xff", b"\xff\xd8", b"O", b"ID",
                                  b"P", b"PK", b"PK\x03", b"\x00"])
def test_predicates_are_total_on_short_input(head):
    # No predicate may raise on empty/short input; all must return a bool.
    for p in MEDIA_POLICIES.values():
        assert p.accepts(head) in (True, False)


def test_document_accepts_pdf_only():
    assert MEDIA_POLICIES["document"].accepts(PDF) is True
    assert MEDIA_POLICIES["document"].accepts(PNG) is False
    assert MEDIA_POLICIES["document"].accepts(b"%PD") is False


def test_photo_accepts_jpeg_and_png():
    ph = MEDIA_POLICIES["photo"]
    assert ph.accepts(JPEG) is True
    assert ph.accepts(PNG) is True
    assert ph.accepts(PDF) is False
    assert ph.accepts(b"\xff\xd8") is False  # too short for the 3-byte JPEG SOI


def test_audio_accepts_id3_and_layer3_rejects_aac_and_layer2():
    au = MEDIA_POLICIES["audio"]
    assert au.accepts(ID3_MP3) is True
    assert au.accepts(LAYER3_MP3) is True                        # FF FB (MPEG1 L3)
    assert au.accepts(b"\xff\xfa\x90\x64" + b"\x00" * 60) is True  # FF FA (MPEG1 L3)
    assert au.accepts(b"\xff\xf3\x40\x00" + b"\x00" * 60) is True  # FF F3 (MPEG2 L3)
    assert au.accepts(ADTS_AAC) is False      # FF F1 — reserved layer, must reject
    assert au.accepts(b"\xff\xf9\x50\x80") is False  # FF F9 — AAC ADTS variant
    assert au.accepts(LAYER2_MP3) is False    # Layer II — must reject
    assert au.accepts(b"\xff\xfb\x00\x64") is False  # bitrate index 0 (free) — reject
    assert au.accepts(b"\xff\xfb\xf0\x64") is False  # bitrate index 15 (bad) — reject
    assert au.accepts(b"\xff\xfb\x9c\x64") is False  # sample-rate index 3 (reserved) — reject
    assert au.accepts(b"\xff\xfb") is False    # sync only, indexes past -> must not raise


def test_voice_accepts_opus_rejects_vorbis():
    vo = MEDIA_POLICIES["voice"]
    assert vo.accepts(OGG_OPUS) is True
    assert vo.accepts(OGG_VORBIS) is False
    assert vo.accepts(b"OggS") is False       # OggS but no OpusHead


ZIP_LOCAL = b"PK\x03\x04" + b"\x00" * 26            # exactly the 30-byte floor
ZIP_EMPTY = b"PK\x05\x06" + b"\x00" * 18            # exactly the 22-byte EOCD


def test_zip_accepts_local_header_and_empty_archive():
    z = MEDIA_POLICIES["zip"]
    assert z.accepts(ZIP_LOCAL) is True
    assert z.accepts(ZIP_EMPTY) is True
    assert z.accepts(ZIP_LOCAL + b"payload") is True


def test_zip_refuses_truncated_signature_stubs():
    """#482 red case: a bare signature is not an archive. Both floors are the
    format's own fixed record sizes — one byte short must refuse."""
    z = MEDIA_POLICIES["zip"]
    assert z.accepts(b"PK\x03\x04") is False           # 4 bytes, no header
    assert z.accepts(ZIP_LOCAL[:-1]) is False          # 29 — one short of 30
    assert z.accepts(ZIP_EMPTY[:-1]) is False          # 21 — one short of 22


def test_zip_refuses_spanned_and_non_archive():
    z = MEDIA_POLICIES["zip"]
    assert z.accepts(b"PK\x07\x08" + b"\x00" * 40) is False   # spanned marker
    assert z.accepts(b"MZ" + b"\x00" * 40) is False           # SFX stub
    assert z.accepts(b"#!/bin/sh\n" + ZIP_LOCAL) is False     # shebang prefix
    assert z.accepts(PDF) is False


def test_text_accepts_plain_utf8():
    t = MEDIA_POLICIES["text"]
    assert t.accepts(b"hello") is True
    assert t.accepts(b"a\tb\r\nc\n") is True                  # TAB, CR, LF pass
    assert t.accepts("ciao àè \U0001f600".encode()) is True  # non-ASCII
    assert t.accepts(b"\xef\xbb\xbfwith bom") is True         # BOM is Cf, passes
    assert t.accepts(b"{not valid json") is True              # ENCODING, not structure


def test_text_refuses_nul_bad_utf8_and_empty():
    """#565 red case, verbatim from the issue: a NUL byte and invalid UTF-8 must
    both be refused, and an empty file must never be delivered as a zero-byte
    document."""
    t = MEDIA_POLICIES["text"]
    assert t.accepts(b"before\x00after") is False             # NUL
    assert t.accepts(b"\xff\xfe\x00") is False                # invalid UTF-8
    assert t.accepts(b"") is False                            # empty
    assert t.accepts(b"\xc3") is False                        # truncated sequence
    assert t.accepts(b"\xed\xa0\x80") is False                # lone surrogate


def test_text_refuses_the_other_c0_controls_and_del_and_c1():
    t = MEDIA_POLICIES["text"]
    assert t.accepts(b"colour: \x1b[31mred\x1b[0m") is False  # ESC — deliberate
    assert t.accepts(b"page\x0cbreak") is False               # form feed
    assert t.accepts(b"vert\x0btab") is False                 # vertical tab
    assert t.accepts(b"del\x7fhere") is False                 # DEL
    assert t.accepts("c1\u0085next".encode()) is False       # C1 (NEL)


def test_text_refuses_every_binary_kind_fixture():
    """The other kinds' own fixtures are binary; none may pass as text."""
    t = MEDIA_POLICIES["text"]
    for blob in (PDF, JPEG, PNG, ID3_MP3, LAYER3_MP3, OGG_OPUS, ZIP_LOCAL):
        assert t.accepts(blob) is False


def test_exact_caps_and_extensions():
    assert MEDIA_POLICIES["document"].size_cap == 20 * 1024 * 1024
    assert MEDIA_POLICIES["photo"].size_cap == 10 * 1024 * 1024
    assert MEDIA_POLICIES["audio"].size_cap == 20 * 1024 * 1024
    assert MEDIA_POLICIES["voice"].size_cap == 20 * 1024 * 1024
    assert MEDIA_POLICIES["document"].extensions == frozenset({".pdf"})
    assert MEDIA_POLICIES["photo"].extensions == frozenset({".jpg", ".jpeg", ".png"})
    assert MEDIA_POLICIES["audio"].extensions == frozenset({".mp3"})
    assert MEDIA_POLICIES["voice"].extensions == frozenset({".ogg", ".oga"})
    assert MEDIA_POLICIES["zip"].size_cap == 20 * 1024 * 1024
    assert MEDIA_POLICIES["zip"].extensions == frozenset({".zip"})
    # #565: text is capped BELOW the other send_document kinds, deliberately.
    assert MEDIA_POLICIES["text"].size_cap == 5 * 1024 * 1024
    assert MEDIA_POLICIES["text"].extensions == frozenset(
        {".txt", ".md", ".csv", ".log", ".json", ".yaml", ".yml"})


def test_zip_extension_list_is_what_confines_the_kind():
    """#482: `PK\\x03\\x04` is also every OOXML/ODF file, `.jar` and `.apk`, so
    the predicate cannot confine this kind — the extension allowlist does. A
    docx buffer passes the PREDICATE and must still be refused by the name."""
    assert MEDIA_POLICIES["zip"].accepts(ZIP_LOCAL) is True
    for ext in (".docx", ".xlsx", ".odt", ".jar", ".apk", ".epub"):
        assert ext not in MEDIA_POLICIES["zip"].extensions


# Deliberately NOT tested: that the kinds partition their extensions. They do
# today, but nothing requires it — `_validate_delivery_filename` checks the name
# against the CALLER-SELECTED kind (`tools.py:291`), so an extension claimed by
# two kinds is still unambiguous about which gate ran. Asserting the partition
# would pin a coincidence and fire on a legitimate future kind that shares an
# extension behind a different content gate.
