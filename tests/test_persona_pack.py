"""Strict persona-pack loader coverage.

`persona_pack.load_persona_pack` reads an authored persona pack
(persona.yaml + persona.md + optional examples.yaml) plus a loader-owned
manifest.json envelope, validates it hard against the v1 schemas and the
Core/quirks/trait invariants, and produces an immutable `PersonaPack`. It
must reject anything outside the exact admitted file set (hidden files,
undeclared files, subdirectories, executable files, symlinks, hard links,
devices, FIFOs), any role-owned/forbidden YAML key, any template/include/
HTML/Casa-structural-delimiter marker in persona prose, and any manifest
that does not exactly match the admitted, checksummed file set.

`markdown_sections` underlies the persona.md parsing: it does ACCEPTED-
CommonMark validation (rejecting unsupported tokens and raw HTML) and
BYTE-PRESERVING source-slice extraction (extracted section bodies must be
exact substrings of the canonical source, never re-rendered, so checksums
stay stable).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from canonical_bytes import canonical_json_bytes, canonical_text, checksum_bytes
from markdown_sections import (
    MarkdownSectionError,
    root_sections,
    select_markdown_sections,
    sections,
    validate_markdown,
)
from persona_pack import PersonaPackError, load_persona_pack


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def valid_yaml() -> dict:
    return {
        "api_version": "casa.persona/v1",
        "id": "example.personas/test",
        "version": "1.0.0",
        "trait_schema_version": 1,
        "identity": {
            "display_name": "Test",
            "pronouns": {
                "subject": "they",
                "object": "them",
                "possessive_adjective": "their",
                "possessive_pronoun": "theirs",
                "reflexive": "themselves",
            },
        },
        "relationship_posture": "professional",
        "archetype": "household-assistant",
        "traits": {
            "warmth": 3, "formality": 3, "candor": 3, "attunement": 3,
            "curiosity": 3, "levity": 3, "social_energy": 3, "optimism": 3,
        },
        "quirks": [],
    }


def core() -> str:
    body = (
        "Test is a calm and attentive household presence who responds with "
        "measured social ease. They notice practical details, distinguish "
        "evidence from assumption, and maintain a consistent interpersonal "
        "manner without claiming memories or authority that the role and "
        "available evidence do not support. Their identity remains stable "
        "across text and voice while operational behaviour remains role-owned."
    )
    return f"# Core\n\n{body}\n\n## Negative space\n\nNo fake intimacy or authority.\n"


def core_with_fill_length(n: int) -> str:
    """`## Negative space` is a level-2 heading nested under `# Core`, so
    `markdown_sections.sections()` (next-heading-of-same-or-shallower-level
    boundary) folds the Negative-space heading + body into the Core
    section's own extracted body — it never terminates it. `n` is the
    length of the literal filler text, not the measured Core-section
    length; see `core_with_measured_core_length` for the latter."""
    body = "A" * n
    return f"# Core\n\n{body}\n\n## Negative space\n\nNo fake intimacy or authority.\n"


def _measured_core_length(fill_len: int) -> int:
    canonical = canonical_text(core_with_fill_length(fill_len))
    core_bodies = [body for level, name, body in sections(canonical)
                   if level == 1 and name == "Core"]
    return len(core_bodies[0].strip())


def core_with_measured_core_length(target: int) -> str:
    """Build a document whose *measured* Core-section length (exactly what
    `persona_pack.load_persona_pack` checks against the 300-500 bound) is
    `target` — found by binary search over the fill length against the real
    `markdown_sections.sections()` (measured length is monotonic non-
    decreasing in fill length) rather than assuming a fixed linear offset,
    so this stays correct even given small-fill edge effects (an empty
    filler body collapses adjacent blank lines differently) and if the
    fixed Negative-space wording ever changes."""
    lo, hi = 0, target
    while lo < hi:
        mid = (lo + hi) // 2
        if _measured_core_length(mid) < target:
            lo = mid + 1
        else:
            hi = mid
    assert _measured_core_length(lo) == target, (
        f"no fill length produces an exact measured Core length of {target}"
    )
    return core_with_fill_length(lo)


def write_pack(path: Path) -> Path:
    pack = path / "pack"
    pack.mkdir()
    (pack / "persona.yaml").write_text(
        yaml.safe_dump(valid_yaml(), sort_keys=False),
        encoding="utf-8",
    )
    (pack / "persona.md").write_text(core(), encoding="utf-8")
    return pack


def build_manifest(pack: Path) -> dict:
    """Replicate the loader's manifest-construction algorithm using only
    the already-shipped `canonical_bytes` primitives (Task 1/2), so tests
    can hand `load_persona_pack` a manifest.json that matches whatever the
    loader will independently recompute from the pack's admitted files."""
    rows = []
    for name in sorted(os.listdir(pack)):
        path = pack / name
        text = canonical_text(path.read_text(encoding="utf-8"))
        rows.append({
            "path": name,
            "type": "file",
            "executable": False,
            "checksum": checksum_bytes(text.encode("utf-8")),
        })
    payload = {"api_version": "casa.persona.manifest/v1", "files": rows}
    payload["checksum"] = checksum_bytes(canonical_json_bytes(payload))
    return payload


def write_manifest(pack: Path, manifest_path: Path) -> dict:
    payload = build_manifest(pack)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def write_valid_manifest(pack: Path, manifest_path: Path) -> None:
    write_manifest(pack, manifest_path)


# ---------------------------------------------------------------------------
# Step 1 (brief, verbatim): forbidden/invalid YAML, templates/HTML/
# structural delimiters, hidden files.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update({"tools": {}}),
        lambda data: data.update({"model": "haiku"}),
        lambda data: data.update({"channels": ["voice"]}),
        lambda data: data["traits"].pop("warmth"),
        lambda data: data["traits"].update({"warmth": 6}),
    ],
)
def test_forbidden_or_invalid_yaml_fails(tmp_path: Path, mutation) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    mutation(data)
    (pack / "persona.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


@pytest.mark.parametrize("text", ["${SECRET}", "{{ value }}", "{% include x %}",
                                  "<platform_frame>", "<b>html</b>", "!include x"])
def test_templates_html_and_structural_delimiters_fail(
    tmp_path: Path, text: str,
) -> None:
    pack = write_pack(tmp_path)
    (pack / "persona.md").write_text(core() + text + "\n", encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_template_marker_in_persona_yaml_fails(tmp_path: Path) -> None:
    # Mirrors test_templates_html_and_structural_delimiters_fail above, but
    # for persona.yaml rather than persona.md — confirms `_reject_markers`
    # is applied to every admitted file's raw text, not just persona.md.
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["identity"]["display_name"] = "Test ${SECRET}"
    (pack / "persona.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_hidden_executable_symlink_hardlink_and_subdirectory_fail(
    tmp_path: Path,
) -> None:
    pack = write_pack(tmp_path)
    (pack / ".hidden").write_text("x", encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


# ---------------------------------------------------------------------------
# Additional pack-boundary rejection coverage (mandated by the "reject
# hidden files, undeclared files, subdirectories, executable files,
# symlinks, hard links, devices, FIFOs" constraint) — each isolates ONE
# admission-boundary category so a regression in `_admit_files` is
# attributable.
# ---------------------------------------------------------------------------


def test_undeclared_extra_file_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    (pack / "readme.txt").write_text("not part of the pack", encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_missing_required_file_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    (pack / "persona.md").unlink()
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_executable_persona_file_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    path = pack / "persona.yaml"
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_symlinked_persona_file_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    real = tmp_path / "outside_examples.yaml"
    real.write_text("api_version: casa.persona.examples/v1\nexamples: []\n", encoding="utf-8")
    (pack / "examples.yaml").symlink_to(real)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_hardlinked_persona_file_fails(tmp_path: Path) -> None:
    # The hard link must occupy one of the DECLARED filenames (here
    # persona.md) so this exercises `_admit_files`'s `st_nlink != 1` guard
    # specifically, rather than the earlier undeclared-file-set check that
    # an extra/undeclared hard-linked name would trip instead.
    pack = write_pack(tmp_path)
    external = tmp_path / "external_persona.md"
    external.write_text(core(), encoding="utf-8")
    (pack / "persona.md").unlink()
    os.link(external, pack / "persona.md")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_subdirectory_in_pack_fails(tmp_path: Path) -> None:
    # The directory must occupy a DECLARED filename (here the optional
    # examples.yaml) so this exercises `_admit_files`'s `S_ISREG` guard
    # specifically, rather than the earlier undeclared-file-set check that
    # an extra/undeclared subdirectory name would trip instead.
    pack = write_pack(tmp_path)
    (pack / "examples.yaml").mkdir()
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_fifo_in_pack_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    fifo_path = pack / "examples.yaml"
    os.mkfifo(fifo_path)
    try:
        with pytest.raises(PersonaPackError):
            load_persona_pack(pack, tmp_path / "manifest.json")
    finally:
        fifo_path.unlink()


# Device-file rejection (mknod for a character/block device) requires root
# in this environment and cannot be exercised without it; `_admit_files`
# rejects non-regular files via `stat.S_ISREG`, which covers devices by the
# same code path already exercised by the FIFO/symlink/subdirectory cases
# above. See task-3-report.md for this environment limitation.


# ---------------------------------------------------------------------------
# G1 (foundation review r4, P0): role_artifact.py caps role.yaml/doctrine.md
# by st_size BEFORE read_text; persona_pack.py's _admit_files had no such
# cap, so a huge persona.yaml/persona.md/examples.yaml would be read fully
# into memory (and then normalized/scanned/checksummed) before schema
# rejection ever ran — an OOM vector for an untrusted pack. Mirrors
# test_role_artifact.py's TestAdversarialTrustGate oversized-file tests.
# ---------------------------------------------------------------------------


def test_oversized_persona_yaml_fails(tmp_path: Path) -> None:
    from persona_pack import MAX_PERSONA_YAML_BYTES

    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["identity"]["display_name"] = "A" * MAX_PERSONA_YAML_BYTES
    (pack / "persona.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    assert (pack / "persona.yaml").stat().st_size > MAX_PERSONA_YAML_BYTES
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_oversized_persona_md_fails(tmp_path: Path) -> None:
    from persona_pack import MAX_PERSONA_MD_BYTES

    pack = write_pack(tmp_path)
    (pack / "persona.md").write_text(
        core() + "A" * MAX_PERSONA_MD_BYTES, encoding="utf-8"
    )
    assert (pack / "persona.md").stat().st_size > MAX_PERSONA_MD_BYTES
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_oversized_examples_yaml_fails(tmp_path: Path) -> None:
    from persona_pack import MAX_EXAMPLES_BYTES

    pack = write_pack(tmp_path)
    examples_payload = {
        "api_version": "casa.persona.examples/v1",
        "examples": [
            {"surface": "text", "user": "hi", "good": "A" * MAX_EXAMPLES_BYTES, "bad": "x"},
        ],
    }
    (pack / "examples.yaml").write_text(
        yaml.safe_dump(examples_payload, sort_keys=False), encoding="utf-8"
    )
    assert (pack / "examples.yaml").stat().st_size > MAX_EXAMPLES_BYTES
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


# ---------------------------------------------------------------------------
# Manifest verification
# ---------------------------------------------------------------------------


def test_valid_pack_with_matching_manifest_loads(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    result = load_persona_pack(pack, manifest_path)
    assert result.persona_id == "example.personas/test"
    assert result.version == "1.0.0"
    assert result.trait_schema_version == 1
    assert result.traits["warmth"] == 3
    assert result.checksum.startswith("sha256:")
    assert result.manifest.checksum == result.checksum


# ---------------------------------------------------------------------------
# R1 (foundation review r2): every shipped canonical persona pack must
# still load after assert_json_safe is wired in as defense in depth —
# their content is ordinary JSON-shaped YAML, so this is a pure
# no-regression check.
#
# The set under test is DERIVED from IMAGE_DEFAULT_PERSONA_BY_SLOT rather
# than hand-listed, because after #544 those are the same set by
# construction and a hand-listed copy is what let an orphan pack sit in the
# image unnoticed. #427: "judge" left with the MTG component. #544: "alex"
# left with the finance specialist — the image ships no specialist content,
# so it ships no persona belonging to no resident slot.
# ---------------------------------------------------------------------------

_REAL_PERSONAS_DIR = Path(__file__).resolve().parent.parent / (
    "casa/rootfs/opt/casa/defaults/personas"
)


def _slot_default_persona_refs_by_slot() -> dict[str, tuple[str, str, str]]:
    """IMAGE_DEFAULT_PERSONA_BY_SLOT re-expressed in the on-disk layout's own
    terms: slot -> (namespace, slug, version)."""
    from personality_binding import IMAGE_DEFAULT_PERSONA_BY_SLOT

    by_slot = {}
    for slot, ref in IMAGE_DEFAULT_PERSONA_BY_SLOT.items():
        persona_id, _, version = ref.partition("@")
        namespace, _, slug = persona_id.partition("/")
        by_slot[slot] = (namespace, slug, version)
    return by_slot


def _slot_default_persona_refs() -> frozenset[tuple[str, str, str]]:
    """The (namespace, slug, version) triples IMAGE_DEFAULT_PERSONA_BY_SLOT
    names, in the on-disk layout's own terms."""
    return frozenset(_slot_default_persona_refs_by_slot().values())


def _shipped_persona_packs() -> frozenset[tuple[str, str, str]]:
    """Every persona pack actually present under defaults/personas/, as
    (namespace, slug, version). Walks the whole tree — not just the "casa"
    namespace — so an orphan pack parked under a new namespace is caught.

    #544 (Sol r2, S2): found by RECURSIVE search for manifest.json and then
    asserting the depth, rather than globbing the expected depth directly. A
    glob of `*/*/*` silently ignores a pack at any other depth, so a valid
    pack dropped at `personas/<ns>/<slug>/` with no version directory was
    invisible to every one of these tests — dead bundled bytes could return
    unnoticed, which is the exact thing INV-PERS-005 exists to prevent. Such
    a pack is not loadable (every loader wants <ns>/<slug>/<version>), so
    misplacement is itself the defect worth failing on, not a shape to
    tolerate."""
    packs = set()
    for manifest in _REAL_PERSONAS_DIR.rglob("manifest.json"):
        parts = manifest.relative_to(_REAL_PERSONAS_DIR).parts
        assert len(parts) == 4, (
            f"persona pack manifest at unexpected depth: "
            f"{manifest.relative_to(_REAL_PERSONAS_DIR)} — expected "
            f"<namespace>/<slug>/<version>/manifest.json"
        )
        packs.add(parts[:3])
    return frozenset(packs)


@pytest.mark.parametrize(
    ("namespace", "slug", "version"), sorted(_slot_default_persona_refs()))
def test_real_shipped_persona_pack_loads(namespace: str, slug: str, version: str) -> None:
    version_dir = _REAL_PERSONAS_DIR / namespace / slug / version
    result = load_persona_pack(
        version_dir / "pack", version_dir / "manifest.json"
    )
    assert result.persona_id == f"{namespace}/{slug}"
    assert result.version == version


def test_image_ships_exactly_the_persona_packs_its_resident_slots_default_to() -> None:
    """#544: the image is not a distribution channel for anything but the
    fixed resident slots' own defaults. A pack in the tree that no slot
    defaults to is dead weight the image cannot serve — nothing resolves it
    for a resident (no resident role's persona compatibility admits a
    foreign slug) and nothing resolves it for a specialist (a specialist's
    component-default persona is read from its CAS store, not from the
    image defaults). Set equality in BOTH directions: an extra pack is an
    orphan, a missing one breaks its slot's boot reconciliation."""
    assert _shipped_persona_packs() == _slot_default_persona_refs()


def test_every_fixed_resident_slot_has_an_image_default_persona() -> None:
    """#544 (Sol/Terra r1, both S2): the set-equality test above compares two
    views DERIVED FROM THE SAME MAP, so it is blind to the map itself losing a
    slot. Dropping "assistant" from IMAGE_DEFAULT_PERSONA_BY_SLOT and deleting
    Ellen's pack in the same edit keeps both derived sets equal and every
    parametrized load passing, while a fresh assistant boot raises KeyError at
    `IMAGE_DEFAULT_PERSONA_BY_SLOT[role.slot]` in reconcile_resident_binding.
    FIXED_RESIDENT_SLOTS is the independent anchor that closes that hole."""
    from personality_binding import IMAGE_DEFAULT_PERSONA_BY_SLOT
    from role_slot import FIXED_RESIDENT_SLOTS

    assert set(IMAGE_DEFAULT_PERSONA_BY_SLOT) == set(FIXED_RESIDENT_SLOTS)
    # #544 (Terra r2, S2): the slots must not COLLAPSE onto a shared persona
    # either. Pointing all three slots at Gary, deleting Ellen and Tina, and
    # relaxing each role's compatibility to match satisfies every other
    # assertion here while "no more, no fewer" is plainly false — the refs
    # are distinct per slot, so their count is the cheap thing to pin.
    assert len(set(IMAGE_DEFAULT_PERSONA_BY_SLOT.values())) == len(FIXED_RESIDENT_SLOTS)


@pytest.mark.parametrize("slot", sorted(_slot_default_persona_refs_by_slot()))
def test_each_slots_image_default_persona_satisfies_that_slots_real_role(slot: str) -> None:
    """#544 (Sol r1 #7): pin each mapped default against the SHIPPED role
    artifact's own persona compatibility — the one oracle here that is not
    derived from the default map. Without it, repointing a slot at another
    slot's persona (assistant -> casa/tina) and deleting the orphaned pack
    satisfies both set-equality and the fixed-slot keyset, yet every boot of
    that resident then fails check_persona_requirements.

    The RAW role artifact is used deliberately, not a materialized role: only
    the persona block is read, and materializing would drag in the ha_option
    model resolution this assertion has no business depending on."""
    from personality_binding import check_persona_requirements
    from role_artifact import load_role_artifact

    namespace, slug, version = _slot_default_persona_refs_by_slot()[slot]
    version_dir = _REAL_PERSONAS_DIR / namespace / slug / version
    pack = load_persona_pack(version_dir / "pack", version_dir / "manifest.json")
    role_dir = (
        Path(__file__).resolve().parent.parent
        / "casa/rootfs/opt/casa/defaults/roles/resident" / slot
    )
    check_persona_requirements(load_role_artifact(role_dir).role, pack)  # raises on mismatch


# ---------------------------------------------------------------------------
# FIX 5 (foundation review, P1): artifacts must be DEEPLY frozen, not just
# top-level MappingProxyType — nested dicts/lists inside identity, quirks,
# examples, and manifest rows must also reject mutation.
# ---------------------------------------------------------------------------


def test_identity_pronouns_are_deeply_frozen(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    result = load_persona_pack(pack, manifest_path)

    with pytest.raises(TypeError):
        result.identity["pronouns"]["subject"] = "x"


def test_quirk_mapping_rejects_mutation(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["quirks"] = [_quirk(0)]
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    result = load_persona_pack(pack, manifest_path)

    with pytest.raises(TypeError):
        result.quirks[0]["context"] = "mutated"


def test_examples_mapping_rejects_mutation(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    examples_payload = {
        "api_version": "casa.persona.examples/v1",
        "examples": [
            {"surface": "text", "user": "hi", "good": "Hello.", "bad": "yo"},
        ],
    }
    (pack / "examples.yaml").write_text(
        yaml.safe_dump(examples_payload, sort_keys=False), encoding="utf-8"
    )
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    result = load_persona_pack(pack, manifest_path)

    with pytest.raises(TypeError):
        result.examples[0]["good"] = "mutated"


def test_manifest_mismatch_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    payload = build_manifest(pack)
    payload["files"][0]["checksum"] = "sha256:" + "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


def test_missing_manifest_file_fails(tmp_path: Path) -> None:
    # J3 (foundation review r6): the loader's error boundary is now TOTAL —
    # a missing manifest file must raise ONLY PersonaPackError, never a raw
    # OSError/FileNotFoundError escaping the boundary.
    pack = write_pack(tmp_path)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "does-not-exist.json")


def test_malformed_manifest_json_fails(tmp_path: Path) -> None:
    # Otherwise-valid pack, but manifest.json is syntactically invalid JSON —
    # exercises the json.JSONDecodeError branch of the loader's
    # `except (OSError, json.JSONDecodeError)` guard, distinct from the
    # missing-file/OSError path covered above.
    pack = write_pack(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


def test_invalid_utf8_byte_in_persona_yaml_fails_with_persona_pack_error(
    tmp_path: Path,
) -> None:
    # H2 (foundation review r5): persona_pack.py read/decoded admitted
    # files OUTSIDE any exception boundary, so an invalid UTF-8 byte in an
    # admitted file raised a raw UnicodeDecodeError instead of folding into
    # the loader's PersonaPackError contract (every other rejection path
    # here raises PersonaPackError).
    pack = write_pack(tmp_path)
    (pack / "persona.yaml").write_bytes(b"not: \xffvalid utf8\n")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_invalid_utf8_byte_in_manifest_fails_with_persona_pack_error(
    tmp_path: Path,
) -> None:
    # H2 (foundation review r5): same boundary gap for the manifest read —
    # its except clause already caught OSError/json.JSONDecodeError but not
    # UnicodeDecodeError, so an invalid UTF-8 byte in manifest.json escaped
    # as a raw UnicodeDecodeError instead of PersonaPackError.
    pack = write_pack(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(b'{"api_version": "casa.persona.manifest/v1", \xff}')
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


def test_missing_pack_directory_fails(tmp_path: Path) -> None:
    # J3 (foundation review r6): a nonexistent pack directory is the
    # simplest OSError trigger for _admit_files' iterdir — must fold into
    # PersonaPackError, not escape as a raw FileNotFoundError/OSError.
    missing = tmp_path / "does_not_exist"
    manifest_path = tmp_path / "manifest.json"
    with pytest.raises(PersonaPackError):
        load_persona_pack(missing, manifest_path)


def test_manifest_deeply_nested_json_raises_persona_pack_error_not_recursion_error(
    tmp_path: Path,
) -> None:
    # J1 (foundation review r6, P0): a manifest.json containing a deeply
    # nested JSON array is valid JSON well under MAX_MANIFEST_BYTES, but
    # json.loads's own parser recurses on it — the loader must fold that
    # into PersonaPackError rather than let a raw RecursionError escape.
    # Depth 20000 (~40KB, still under the 65536-byte manifest cap) reliably
    # triggers CPython's json decoder RecursionError on this build; a
    # shallower depth (e.g. 2000) does not.
    pack = write_pack(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    depth = 20000
    manifest_path.write_text("[" * depth + "0" + "]" * depth, encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


def test_manifest_oversized_integer_literal_raises_persona_pack_error(
    tmp_path: Path,
) -> None:
    # r6 close-out: a manifest.json holding an integer literal longer than
    # CPython's int-string-digit limit (default 4300) is well under
    # MAX_MANIFEST_BYTES, yet json.loads raises a plain ValueError (NOT a
    # json.JSONDecodeError) while converting it. The manifest parse boundary
    # must fold that into PersonaPackError like every other rejection.
    pack = write_pack(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("5" * 5000, encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


def test_oversized_manifest_file_fails(tmp_path: Path) -> None:
    # J1 (foundation review r6): a size cap on manifest.json, checked via
    # stat() BEFORE read/parse, so an oversized fetched manifest can't force
    # an unbounded read/parse allocation.
    pack = write_pack(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    payload = {
        "api_version": "casa.persona.manifest/v1",
        "files": [],
        "pad": "a" * 70_000,
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PersonaPackError, match="too large"):
        load_persona_pack(pack, manifest_path)


def test_manifest_stale_after_pack_content_changes(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    (pack / "persona.md").write_text(core() + "\nExtra unreviewed sentence.\n", encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


# ---------------------------------------------------------------------------
# Core length boundaries: 300-500 Unicode characters required (299/501 fail,
# 300/500 pass).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("length", [299, 501])
def test_core_body_length_outside_boundaries_fails(tmp_path: Path, length: int) -> None:
    pack = write_pack(tmp_path)
    (pack / "persona.md").write_text(core_with_measured_core_length(length), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


@pytest.mark.parametrize("length", [300, 500])
def test_core_body_length_at_boundaries_passes(tmp_path: Path, length: int) -> None:
    pack = write_pack(tmp_path)
    (pack / "persona.md").write_text(core_with_measured_core_length(length), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    result = load_persona_pack(pack, manifest_path)
    assert result.persona_id == "example.personas/test"


def test_missing_core_section_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    (pack / "persona.md").write_text(
        "# Not Core\n\nSomething else entirely that is not the required "
        "section heading at all.\n\n## Negative space\n\nNone.\n",
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


def test_duplicate_core_section_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    doc = core() + "\n" + core_with_fill_length(320)
    (pack / "persona.md").write_text(doc, encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


def test_missing_negative_space_section_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    body = "A" * 320
    (pack / "persona.md").write_text(f"# Core\n\n{body}\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


# ---------------------------------------------------------------------------
# Quirks: at most three entries, author order preserved, context/tendency
# capped at 240 chars each.
# ---------------------------------------------------------------------------


def _quirk(i: int) -> dict:
    return {
        "frequency": "occasional",
        "context": f"context-{i}",
        "tendency": f"tendency-{i}",
    }


@pytest.mark.parametrize("count", [0, 3])
def test_quirks_count_within_limit_passes(tmp_path: Path, count: int) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["quirks"] = [_quirk(i) for i in range(count)]
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    result = load_persona_pack(pack, manifest_path)
    assert [q["context"] for q in result.quirks] == [f"context-{i}" for i in range(count)]


def test_quirks_count_over_limit_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["quirks"] = [_quirk(i) for i in range(4)]
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


@pytest.mark.parametrize("field", ["context", "tendency"])
def test_quirk_field_over_240_chars_fails(tmp_path: Path, field: str) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    quirk = _quirk(0)
    quirk[field] = "x" * 241
    data["quirks"] = [quirk]
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_quirks_preserve_author_order(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["quirks"] = [_quirk(2), _quirk(0), _quirk(1)]
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    result = load_persona_pack(pack, manifest_path)
    assert [q["context"] for q in result.quirks] == ["context-2", "context-0", "context-1"]


# ---------------------------------------------------------------------------
# Every forbidden/role-owned top-level YAML key.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value",
    [
        ("tools", {}),
        ("model", "haiku"),
        ("channels", ["voice"]),
        ("system_prompt", "override"),
        ("mcp_servers", []),
        ("hooks", {}),
        ("permissions", {}),
    ],
)
def test_every_role_owned_key_is_forbidden(tmp_path: Path, key: str, value: object) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data[key] = value
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


# ---------------------------------------------------------------------------
# Trait schema: exactly the eight v1 axes, integers 1-5.
# ---------------------------------------------------------------------------


def test_trait_missing_axis_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    del data["traits"]["curiosity"]
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_trait_extra_axis_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["traits"]["extra_axis"] = 3
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


@pytest.mark.parametrize("value", [0, 6, 3.5])
def test_trait_value_out_of_range_or_non_integer_fails(tmp_path: Path, value) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["traits"]["warmth"] = value
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


# ---------------------------------------------------------------------------
# Pronoun field length.
# ---------------------------------------------------------------------------


def test_invalid_pronoun_length_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["identity"]["pronouns"]["subject"] = "x" * 41
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


def test_empty_pronoun_field_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["identity"]["pronouns"]["object"] = ""
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


# ---------------------------------------------------------------------------
# examples.yaml (optional file, schema-validated when present).
# ---------------------------------------------------------------------------


def test_valid_examples_file_loads(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    examples_payload = {
        "api_version": "casa.persona.examples/v1",
        "examples": [
            {"surface": "text", "user": "hi", "good": "Hello.", "bad": "yo"},
        ],
    }
    (pack / "examples.yaml").write_text(
        yaml.safe_dump(examples_payload, sort_keys=False), encoding="utf-8"
    )
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    result = load_persona_pack(pack, manifest_path)
    assert len(result.examples) == 1
    assert result.examples[0]["good"] == "Hello."


def test_examples_file_wrong_api_version_fails(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    examples_payload = {
        "api_version": "casa.persona.examples/v0",
        "examples": [],
    }
    (pack / "examples.yaml").write_text(
        yaml.safe_dump(examples_payload, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, tmp_path / "manifest.json")


# ---------------------------------------------------------------------------
# FIX 6 (foundation review, P1): general HTML rejection outside persona.md.
# persona.md is caught by Markdown validation, but persona.yaml/
# examples.yaml only went through `_reject_markers`, which (pre-fix) only
# blocked the literal substring "<html" — `<script>`, `<img>`, etc. slipped
# through in those two files.
# ---------------------------------------------------------------------------


def test_script_tag_in_persona_yaml_quirk_fails(tmp_path: Path) -> None:
    # A valid manifest is written FIRST so that — pre-fix, when the marker
    # check does not yet catch this — the load would otherwise SUCCEED
    # rather than fail for an unrelated reason (an absent manifest.json
    # also raises PersonaPackError, which would make this test a false
    # positive that passes regardless of whether the HTML is rejected).
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["quirks"] = [{
        "frequency": "occasional",
        "context": "<script>alert(1)</script>",
        "tendency": "tendency-0",
    }]
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError, match="template, include, HTML, or delimiter"):
        load_persona_pack(pack, manifest_path)


def test_img_tag_in_examples_yaml_fails(tmp_path: Path) -> None:
    # See comment in test_script_tag_in_persona_yaml_quirk_fails above for
    # why the manifest must match the ACTUAL (malicious) file content —
    # written from the state that already contains the injected tag, so
    # the manifest check itself can never be what raises here — only the
    # marker/HTML rejection can (asserted via the specific message).
    pack = write_pack(tmp_path)
    examples_payload = {
        "api_version": "casa.persona.examples/v1",
        "examples": [
            {"surface": "text", "user": "hi", "good": "Hello.", "bad": "<img src=x>"},
        ],
    }
    (pack / "examples.yaml").write_text(
        yaml.safe_dump(examples_payload, sort_keys=False), encoding="utf-8"
    )
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError, match="template, include, HTML, or delimiter"):
        load_persona_pack(pack, manifest_path)


def test_benign_angle_bracket_comparison_in_persona_yaml_still_loads(tmp_path: Path) -> None:
    # NUMERIC right-hand side: HTML_TAG_OPEN_RE requires a LETTER after the
    # (optionally whitespace-separated) '<', so "2 < 3" never matches it —
    # this is the one prose form of "<" that is guaranteed to load.
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["quirks"] = [{
        "frequency": "occasional",
        "context": "Notices when 2 < 3 in a casual aside.",
        "tendency": "tendency-0",
    }]
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    result = load_persona_pack(pack, manifest_path)
    assert result.quirks[0]["context"] == "Notices when 2 < 3 in a casual aside."


# ---------------------------------------------------------------------------
# F-A (foundation review r3, P0): the raw-text marker scan runs BEFORE
# yaml.safe_load, so a YAML string escape (e.g. \x24\x7b, which decodes to
# the literal characters "${") is invisible to the raw scan but is
# DECODED by the YAML parser into the live forbidden marker. The parsed
# persona/examples content must be marker-scanned on its PARSED string
# leaves (post-decode), not just its raw source text, to close this.
# ---------------------------------------------------------------------------


def test_escaped_template_marker_in_persona_yaml_quirk_is_rejected(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["quirks"] = [{
        "frequency": "occasional",
        "context": "PLACEHOLDER",
        "tendency": "tendency-0",
    }]
    yaml_text = yaml.safe_dump(data, sort_keys=False)
    assert "context: PLACEHOLDER\n" in yaml_text
    # Raw source contains the literal backslash-x escape sequence, NOT the
    # characters "${" — a raw-text scan sees no marker here. The YAML
    # parser decodes \x24\x7b -> "${" and \x7d -> "}", producing the live
    # string "${OVERRIDE}" only after yaml.safe_load runs.
    yaml_text = yaml_text.replace(
        "context: PLACEHOLDER\n", 'context: "\\x24\\x7bOVERRIDE\\x7d"\n'
    )
    assert "${" not in yaml_text  # confirms this is genuinely a raw-scan bypass
    (pack / "persona.yaml").write_text(yaml_text, encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


def test_escaped_html_tag_in_persona_yaml_quirk_is_rejected(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["quirks"] = [{
        "frequency": "occasional",
        "context": "PLACEHOLDER",
        "tendency": "tendency-0",
    }]
    yaml_text = yaml.safe_dump(data, sort_keys=False)
    assert "context: PLACEHOLDER\n" in yaml_text
    # \x3c -> "<", \x3e -> ">": decodes to the live string "<script>" only
    # after yaml.safe_load runs; the raw source has no bare "<" at all.
    yaml_text = yaml_text.replace(
        "context: PLACEHOLDER\n", 'context: "\\x3cscript\\x3e"\n'
    )
    assert "<" not in yaml_text  # confirms this is genuinely a raw-scan bypass
    (pack / "persona.yaml").write_text(yaml_text, encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


def test_escaped_marker_in_examples_yaml_is_rejected(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    examples_payload = {
        "api_version": "casa.persona.examples/v1",
        "examples": [
            {"surface": "text", "user": "hi", "good": "Hello.", "bad": "PLACEHOLDER"},
        ],
    }
    yaml_text = yaml.safe_dump(examples_payload, sort_keys=False)
    assert 'bad: PLACEHOLDER\n' in yaml_text
    yaml_text = yaml_text.replace(
        "bad: PLACEHOLDER\n", 'bad: "\\x24\\x7bOVERRIDE\\x7d"\n'
    )
    assert "${" not in yaml_text  # confirms this is genuinely a raw-scan bypass
    (pack / "examples.yaml").write_text(yaml_text, encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


# ---------------------------------------------------------------------------
# F-B (foundation review r3, P0): yaml.safe_load's own PARSER recurses on a
# deeply-nested flow scalar well under any of the loader's size caps, so an
# uncaught RecursionError crashed the loader before assert_json_safe (which
# only runs AFTER parsing succeeds) ever got a chance to bound the depth.
# ---------------------------------------------------------------------------


def test_deeply_nested_flow_sequence_in_persona_yaml_raises_not_recursion_error(
    tmp_path: Path,
) -> None:
    pack = write_pack(tmp_path)
    (pack / "persona.yaml").write_text(
        "[" * 2000 + "0" + "]" * 2000, encoding="utf-8"
    )
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


def test_deeply_nested_flow_sequence_in_examples_yaml_raises_not_recursion_error(
    tmp_path: Path,
) -> None:
    pack = write_pack(tmp_path)
    (pack / "examples.yaml").write_text(
        "[" * 2000 + "0" + "]" * 2000, encoding="utf-8"
    )
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


# ---------------------------------------------------------------------------
# G2 (foundation review r4, P1): PyYAML raises a plain ValueError (e.g.
# "chr() arg not in range(0x110000)") for an invalid Unicode escape like
# "\U00110000" — neither yaml.YAMLError nor RecursionError, so it escaped
# the loader's `except (yaml.YAMLError, RecursionError)` parse boundary and
# leaked the raw parser-internal message as a bare ValueError instead of a
# PersonaPackError.
# ---------------------------------------------------------------------------


def test_invalid_unicode_escape_in_persona_yaml_raises_persona_pack_error(
    tmp_path: Path,
) -> None:
    pack = write_pack(tmp_path)
    (pack / "persona.yaml").write_text('x: "\\U00110000"\n', encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError) as exc_info:
        load_persona_pack(pack, manifest_path)
    assert "chr() arg" not in str(exc_info.value)


def test_invalid_unicode_escape_in_examples_yaml_raises_persona_pack_error(
    tmp_path: Path,
) -> None:
    pack = write_pack(tmp_path)
    (pack / "examples.yaml").write_text('x: "\\U00110000"\n', encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError) as exc_info:
        load_persona_pack(pack, manifest_path)
    assert "chr() arg" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# F-C (foundation review r3, P0 DoS): YAML aliases let a tiny, shallow
# authored document expand into an exponentially large DAG once walked
# (assert_json_safe, deep_freeze) — forbid aliases outright at parse time.
# Both persona.v1.json and persona-examples.v1.json are fully closed
# schemas (additionalProperties: false throughout, no schema-open array/
# object field) — unlike role.v1.json's `delegates` (see
# test_role_artifact.py's TestNoAliasesPermitted, which covers the
# DAG-amplification bomb shape directly) — so here the alias is a plain
# SCALAR reuse: schema-valid whether or not aliases are permitted, which is
# exactly what makes this a genuine pre-fix "loads successfully" case
# rather than one that would fail for an unrelated (schema) reason either
# way. Anchors with no alias are harmless and must still load (see the
# existing test_real_shipped_persona_pack_loads no-regression coverage).
# ---------------------------------------------------------------------------


def test_simple_yaml_alias_reference_in_persona_yaml_quirk_is_rejected(
    tmp_path: Path,
) -> None:
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["quirks"] = [{
        "frequency": "occasional",
        "context": "shared text",
        "tendency": "shared text",
    }]
    yaml_text = yaml.safe_dump(data, sort_keys=False)
    assert "context: shared text\n" in yaml_text
    assert "tendency: shared text\n" in yaml_text
    yaml_text = yaml_text.replace("context: shared text\n", "context: &c0 shared text\n")
    yaml_text = yaml_text.replace("tendency: shared text\n", "tendency: *c0\n")
    (pack / "persona.yaml").write_text(yaml_text, encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


def test_simple_yaml_alias_reference_in_examples_yaml_is_rejected(tmp_path: Path) -> None:
    pack = write_pack(tmp_path)
    examples_payload = {
        "api_version": "casa.persona.examples/v1",
        "examples": [
            {"surface": "text", "user": "hi", "good": "shared text", "bad": "shared text"},
        ],
    }
    yaml_text = yaml.safe_dump(examples_payload, sort_keys=False)
    assert "good: shared text\n" in yaml_text
    assert "bad: shared text\n" in yaml_text
    yaml_text = yaml_text.replace("good: shared text\n", "good: &g0 shared text\n")
    yaml_text = yaml_text.replace("bad: shared text\n", "bad: *g0\n")
    (pack / "examples.yaml").write_text(yaml_text, encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError):
        load_persona_pack(pack, manifest_path)


def test_letter_angle_bracket_comparison_in_persona_yaml_fails(tmp_path: Path) -> None:
    # R3 (foundation review r2): HTML_TAG_OPEN_RE is deliberately
    # CONSERVATIVE for a trust boundary — it rejects "<" followed (even
    # across whitespace) by a LETTER, so "a < b" is rejected, unlike the
    # numeric "2 < 3" case above. This pins that contrast explicitly so
    # the module's documented behavior and this test never drift apart
    # again (a prior version of this comment/test only proved the numeric
    # case, leaving the letter case's rejection undocumented).
    pack = write_pack(tmp_path)
    data = valid_yaml()
    data["quirks"] = [{
        "frequency": "occasional",
        "context": "Notices when a < b in a casual aside.",
        "tendency": "tendency-0",
    }]
    (pack / "persona.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)
    with pytest.raises(PersonaPackError, match="template, include, HTML, or delimiter"):
        load_persona_pack(pack, manifest_path)


# ===========================================================================
# markdown_sections — ACCEPTED-CommonMark validation + byte-preserving
# source-slice extraction, tested directly.
# ===========================================================================


@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        ((1, 2, 3), [(1, "H0")]),
        ((2, 1, 2), [(2, "H0"), (1, "H1")]),
        ((1, 2, 1, 2), [(1, "H0"), (1, "H2")]),
        ((3, 1), [(3, "H0"), (1, "H1")]),
    ],
)
def test_root_sections_returns_the_prefix_minimum_rows_in_document_order(
        levels: tuple[int, ...], expected: list[tuple[int, str]]) -> None:
    """#611. A span is a root exactly when no other span contains it, which
    ``_section_spans`` makes a pure function of the level sequence: a span ends
    at the next heading of the same-or-shallower level, so a span is a root iff
    its level is <= every level before it.

    Before ``root_sections`` existed this collected as
    ``ImportError: cannot import name 'root_sections'`` — a COLLECTION error,
    not an assertion failure, so it never read as a passing test.

    The parameters are NOT interchangeable, which is the point of having four.
    Measured, one mutation at a time: ``<=`` -> ``<`` reds ONLY (1,2,1,2);
    initialising ``shallowest = 1`` and dropping the ``is None`` disjunct reds
    (2,1,2) and (3,1); and both ``root_sections := sections`` and deleting the
    ``shallowest = level`` update red (1,2,3), (2,1,2) and (1,2,1,2) -- those
    two are the same mutation in effect, since without the update
    ``shallowest`` stays ``None`` and every span is kept. (3,1) is the only
    parameter that tells the ``shallowest = 1`` mutation apart from those."""
    doc = "".join("#" * level + f" H{index}\n\nbody{index}\n\n"
                  for index, level in enumerate(levels))
    assert len(sections(doc)) == len(levels), "premise: one section per heading"
    assert [(level, name) for level, name, _ in root_sections(doc)] == expected
    # Bodies are returned verbatim from `sections`, never re-rendered: without
    # this an implementation that truncated or reflowed a body would pass.
    flat = {(level, name): body for level, name, body in sections(doc)}
    assert all(flat[(level, name)] == body
               for level, name, body in root_sections(doc))


@pytest.mark.parametrize(
    "html_fragment",
    [
        "<b>html</b>",
        "<b>unterminated",
        "<!--comment-->",
        "<!DOCTYPE html>",
        "<?probe?>",
        "<script>x</script>",
        "<div>\nblock\n</div>\n",
    ],
)
def test_raw_html_fails_in_all_forms(html_fragment: str) -> None:
    source = core() + html_fragment + "\n"
    with pytest.raises(MarkdownSectionError):
        validate_markdown(source)


@pytest.mark.parametrize("text", ["2 < 3", "`<b>`"])
def test_non_html_angle_brackets_and_backticked_code_remain_valid(text: str) -> None:
    source = core() + text + "\n"
    assert validate_markdown(source)


@pytest.mark.parametrize(
    "unsupported_fragment",
    [
        "> a blockquote\n",
        "1. an ordered list item\n",
        "---\n",
        "![alt](img.png)\n",
        "[a link](http://example.com)\n",
    ],
)
def test_unsupported_markdown_tokens_fail(unsupported_fragment: str) -> None:
    # A blank line before the fragment is required so "---" is parsed as a
    # thematic break (hr) rather than a setext-heading underline for the
    # immediately preceding paragraph (CommonMark setext rule).
    source = core() + "\n" + unsupported_fragment
    with pytest.raises(MarkdownSectionError):
        validate_markdown(source)


def _heading_body_start(canonical: str, level: int, name: str) -> int:
    """The byte offset where `sections()` begins a section's extracted
    body: right after the heading line, past the blank-line separator."""
    heading_line = f"{'#' * level} {name}"
    idx = canonical.index(heading_line) + len(heading_line)
    while canonical[idx] == "\n":
        idx += 1
    return idx


def test_source_slice_extraction_is_byte_preserving() -> None:
    # A crafted document with distinctive inline markdown (*emphasis*,
    # inline `code`) in each section body. Mere substring containment
    # (`body in canonical`) would also pass for a body lifted from the
    # WRONG offset (e.g. swapped between sections, or re-rendered from an
    # AST) as long as matching text existed anywhere in the source — so
    # this instead anchors each body to ITS OWN heading's exact source
    # offset. Re-rendering emphasis/code from a parsed AST would also not
    # reproduce these literal `*`/backtick source bytes verbatim.
    source = (
        "# Core\n\n"
        "Core body with *emphasis* and inline `code` markers right here.\n\n"
        "## Negative space\n\n"
        "Negative space body with *other emphasis* and `other code`.\n"
    )
    canonical = canonical_text(source)
    parsed = sections(canonical)

    core_bodies = [body for level, name, body in parsed if level == 1 and name == "Core"]
    assert len(core_bodies) == 1
    core_body = core_bodies[0]
    core_body_no_trailing_nl = core_body[:-1] if core_body.endswith("\n") else core_body
    core_start = _heading_body_start(canonical, 1, "Core")
    # Exact-offset check: the extracted body must equal the source slice
    # immediately adjacent to its OWN heading, byte-for-byte.
    assert (
        canonical[core_start:core_start + len(core_body_no_trailing_nl)]
        == core_body_no_trailing_nl
    )
    assert "*emphasis*" in core_body
    assert "`code`" in core_body

    negative_space_bodies = [
        body for level, name, body in parsed if level == 2 and name == "Negative space"
    ]
    assert len(negative_space_bodies) == 1
    ns_body = negative_space_bodies[0]
    ns_body_no_trailing_nl = ns_body[:-1] if ns_body.endswith("\n") else ns_body
    ns_start = _heading_body_start(canonical, 2, "Negative space")
    assert (
        canonical[ns_start:ns_start + len(ns_body_no_trailing_nl)]
        == ns_body_no_trailing_nl
    )
    assert "*other emphasis*" in ns_body
    assert "`other code`" in ns_body


def _sibling_sections_source() -> str:
    # Sibling level-1 headings (unlike `## Negative space` under `# Core`,
    # which is a nested subsection swallowed into Core's own body) isolate
    # `select_markdown_sections`'s subset-selection behaviour from the
    # heading-nesting behaviour covered by `test_source_slice_...` above.
    return (
        "# First\n\nFirst section body text.\n\n"
        "# Second\n\nSecond section body text.\n\n"
        "# Third\n\nThird section body text.\n"
    )


def test_select_markdown_sections_returns_named_bodies_in_source_order() -> None:
    canonical = canonical_text(_sibling_sections_source())
    result = select_markdown_sections(canonical, ("Third", "First"))
    assert "First section body text." in result
    assert "Third section body text." in result
    first_pos = result.index("First section body text.")
    third_pos = result.index("Third section body text.")
    # Selection follows SOURCE order, not the order names were requested in.
    assert first_pos < third_pos


def test_select_markdown_sections_omits_unselected_sections() -> None:
    canonical = canonical_text(_sibling_sections_source())
    result = select_markdown_sections(canonical, ("First",))
    assert "First section body text." in result
    assert "Second section body text." not in result
    assert "Third section body text." not in result


def test_pin_inv_pers_002_capability_claims_are_not_semantically_validated(tmp_path):
    """Pins INV-PERS-002: persona validation is structural; a prose claim
    about capability is not checked against what the role can do.

    Red case demonstrated: adding a capability-claim rejection after the
    schema validation in load_persona_pack fails this test.
    """
    pack = write_pack(tmp_path)
    claim = "I can invoke the imaginary privileged tool."
    (pack / "persona.md").write_text(
        core().replace("No fake intimacy or authority.", claim),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    write_valid_manifest(pack, manifest_path)

    loaded = load_persona_pack(pack, manifest_path)
    assert claim in loaded.markdown


# ===========================================================================
# #611 — the compiled persona BODY of every shipped pack, pinned to exact
# bytes. This is the only assertion in the batch that proves the render-once
# change did not move the shipped VOICE bytes, which is the release's central
# safety claim.
# ===========================================================================

_PERSONA_BODY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "persona_body_v1.json"


def _shipped_packs():
    from persona_pack import load_persona_pack as _load
    packs = []
    for namespace, slug, version in sorted(_slot_default_persona_refs()):
        version_dir = _REAL_PERSONAS_DIR / namespace / slug / version
        packs.append(_load(version_dir / "pack", version_dir / "manifest.json"))
    return packs


@pytest.mark.parametrize("surface", ["text", "voice"])
def test_each_shipped_persona_body_is_exact_bytes(surface: str) -> None:
    """#611. The TEXT half is red on the pre-fix tree (tina/ellen/gary were
    1369/1372/1380 bytes, carrying every nested subsection twice). The VOICE
    half was already green and must STAY green — voice is byte-identical
    across this change for all three packs, which is what makes the change
    safe to ship without touching a single binding.

    The two surfaces are written out SEPARATELY even though all three packs
    happen to produce equal values for both. That equality is ACCIDENTAL —
    each pack ships two quirks and no examples, so the text-only extras all
    render empty and ``quirks[:2]`` happens to be all of them. Asserting
    ``text == voice`` would dress a coincidence as an invariant."""
    from prompt_compiler import _persona_body

    fixture = json.loads(_PERSONA_BODY_FIXTURE.read_text())
    packs = _shipped_packs()
    assert packs, "premise: the image ships at least one persona pack"
    for pack in packs:
        row = fixture[f"{pack.persona_id}@{pack.version}"]
        body = _persona_body(pack, surface).encode("utf-8")
        assert (len(body), checksum_bytes(body)) == (
            row[f"{surface}_bytes"], row[f"{surface}_sha256"]), pack.persona_id


def test_the_persona_body_fixture_names_the_packs_it_was_computed_from() -> None:
    """Binds each fixture row to the pack checksum it was computed from, so a
    persona edit is caught as a FIXTURE-drift failure and not only as an opaque
    byte diff that invites 'refreshing' the golden to whatever the tree now
    produces.

    It does not claim to fail FIRST: the exact-byte parametrization above runs
    before it and already names the pack in its assertion message. This is the
    separate binding — bytes alone cannot tell an intended persona edit from a
    renderer regression, and the checksum can. Mirrors the trait-renderer
    fixture's version pin."""
    fixture = json.loads(_PERSONA_BODY_FIXTURE.read_text())
    packs = _shipped_packs()
    assert set(fixture) == {f"{p.persona_id}@{p.version}" for p in packs}
    for pack in packs:
        assert fixture[f"{pack.persona_id}@{pack.version}"]["persona_checksum"] == pack.checksum
