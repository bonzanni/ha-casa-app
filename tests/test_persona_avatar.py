"""A persona pack may carry one avatar image, ``avatar.jpg``.

The image is binary: it is read as raw bytes, never decoded or canonicalized,
and its exact bytes are checksummed into the manifest, so they are part of the
persona's digest. It must be a JPEG (by its magic bytes), at most
``MAX_AVATAR_BYTES``, and admitted like the pack's other files (no symlink,
hard link or executable bit). A pack without one is unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from canonical_bytes import canonical_json_bytes, canonical_text, checksum_bytes
from persona_pack import PersonaPackError, load_persona_pack
from test_persona_pack import write_pack

MAX_AVATAR_BYTES = 1024 * 1024   # the cap the loader enforces

# Not valid UTF-8 (0xFF), and carrying bytes a text canonicalizer would
# rewrite (CRLF, trailing blanks): the digest must cover them verbatim.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\r\n  \r\n\x80\x81\xfe\xff\xd9"


def _manifest(pack: Path) -> dict:
    rows = []
    for name in sorted(os.listdir(pack)):
        raw = (pack / name).read_bytes()
        if name != "avatar.jpg":
            raw = canonical_text(raw.decode("utf-8")).encode("utf-8")
        rows.append({"path": name, "type": "file", "executable": False,
                     "checksum": checksum_bytes(raw)})
    payload = {"api_version": "casa.persona.manifest/v1", "files": rows}
    payload["checksum"] = checksum_bytes(canonical_json_bytes(payload))
    return payload


def _pack(tmp_path: Path, avatar: bytes | None = JPEG) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    pack = write_pack(tmp_path)
    if avatar is not None:
        (pack / "avatar.jpg").write_bytes(avatar)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(pack)), encoding="utf-8")
    return pack, manifest


def test_a_pack_with_a_jpeg_avatar_loads(tmp_path: Path) -> None:
    pack, manifest = _pack(tmp_path)
    loaded = load_persona_pack(pack, manifest)
    assert loaded.avatar_path == pack / "avatar.jpg"
    assert loaded.avatar_path.read_bytes() == JPEG
    rows = {row["path"]: row for row in loaded.manifest.files}
    assert rows["avatar.jpg"]["checksum"] == checksum_bytes(JPEG)


def test_the_digest_covers_the_avatars_exact_bytes(tmp_path: Path) -> None:
    pack, manifest = _pack(tmp_path / "a")
    flipped = bytearray(JPEG)
    flipped[-3] ^= 0x01
    other_pack, other_manifest = _pack(tmp_path / "b", bytes(flipped))
    without_pack, without_manifest = _pack(tmp_path / "c", None)
    digests = {load_persona_pack(pack, manifest).checksum,
               load_persona_pack(other_pack, other_manifest).checksum,
               load_persona_pack(without_pack, without_manifest).checksum}
    assert len(digests) == 3


def test_a_changed_avatar_no_longer_matches_its_manifest(tmp_path: Path) -> None:
    pack, manifest = _pack(tmp_path)
    (pack / "avatar.jpg").write_bytes(JPEG.replace(b"\r\n", b"\n"))
    with pytest.raises(PersonaPackError, match="does not match"):
        load_persona_pack(pack, manifest)


def test_a_pack_without_an_avatar_is_unchanged(tmp_path: Path) -> None:
    from test_persona_pack import build_manifest

    pack, manifest = _pack(tmp_path, None)
    assert json.loads(manifest.read_text()) == build_manifest(pack)
    loaded = load_persona_pack(pack, manifest)
    assert loaded.avatar_path is None
    assert [row["path"] for row in loaded.manifest.files] == ["persona.md", "persona.yaml"]


@pytest.mark.parametrize("data", [
    b"\x89PNG\r\n\x1a\n\x00\x00",
    b"GIF89a",
    b"",
    b"\xff\xd8",
])
def test_a_non_jpeg_avatar_is_refused(tmp_path: Path, data: bytes) -> None:
    pack, manifest = _pack(tmp_path, data)
    with pytest.raises(PersonaPackError, match="not a JPEG"):
        load_persona_pack(pack, manifest)


def test_an_oversize_avatar_is_refused(tmp_path: Path) -> None:
    at_cap = JPEG + b"\x00" * (MAX_AVATAR_BYTES - len(JPEG))
    pack, manifest = _pack(tmp_path / "ok", at_cap)
    assert load_persona_pack(pack, manifest).avatar_path is not None
    pack, manifest = _pack(tmp_path / "over", at_cap + b"\x00")
    with pytest.raises(PersonaPackError, match="too large"):
        load_persona_pack(pack, manifest)


def test_a_symlinked_avatar_is_refused(tmp_path: Path) -> None:
    pack, manifest = _pack(tmp_path, None)
    real = tmp_path / "elsewhere.jpg"
    real.write_bytes(JPEG)
    (pack / "avatar.jpg").symlink_to(real)
    with pytest.raises(PersonaPackError, match="invalid persona file: avatar.jpg"):
        load_persona_pack(pack, manifest)


def test_an_executable_avatar_is_refused(tmp_path: Path) -> None:
    pack, manifest = _pack(tmp_path)
    (pack / "avatar.jpg").chmod(0o755)
    with pytest.raises(PersonaPackError, match="executable persona file"):
        load_persona_pack(pack, manifest)


def test_an_install_carries_the_avatar_to_the_installed_pack(tmp_path: Path) -> None:
    from persona_install import (
        PersonaInspectionResult, PersonaInstallAckStore, commit_persona_install,
        persona_install_consent_identity,
    )

    staged = tmp_path / "staged"
    pack, manifest = _pack(staged)
    inspected = load_persona_pack(pack, manifest)
    inspection = PersonaInspectionResult(
        persona_id=inspected.persona_id, version=inspected.version,
        checksum=inspected.checksum, display_name="Test", staged_dir=staged)
    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    acks.record(
        identity=persona_install_consent_identity(
            persona_id=inspection.persona_id, version=inspection.version,
            checksum=inspection.checksum),
        persona_id=inspection.persona_id, version=inspection.version,
        checksum=inspection.checksum)
    personas_root = tmp_path / "personas"

    installed = commit_persona_install(
        inspection=inspection, acks=acks, personas_root=personas_root)

    dest = personas_root / inspected.persona_id / inspected.version / "pack"
    assert installed.checksum == inspected.checksum
    assert installed.avatar_path == dest / "avatar.jpg"
    assert installed.avatar_path.read_bytes() == JPEG
