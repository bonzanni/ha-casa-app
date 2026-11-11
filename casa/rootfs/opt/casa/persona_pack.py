from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import jsonschema
import yaml

from authored_markers import reject_markers_in_parsed
from canonical_bytes import (
    assert_json_safe,
    canonical_json_bytes,
    canonical_text,
    checksum_bytes,
    deep_freeze,
    reject_forbidden_markers,
)
from markdown_sections import sections
from yaml_safety import load_yaml_no_aliases

_REQUIRED = {"persona.yaml", "persona.md"}
AVATAR_FILE = "avatar.jpg"
_OPTIONAL = {"examples.yaml", AVATAR_FILE}
# The avatar is the one binary file a pack may carry: read as raw bytes,
# never decoded or canonicalized, and checksummed as they are.
_BINARY = {AVATAR_FILE}
_JPEG_MAGIC = b"\xff\xd8\xff"

# Byte-size caps checked BEFORE read_text, so a hostile pack can't force an
# unbounded allocation — mirrors role_artifact.py's MAX_ROLE_YAML_BYTES /
# MAX_DOCTRINE_BYTES pattern (foundation review r4, G1). persona.yaml and
# examples.yaml are small structured files; persona.md is free-form prose
# but still bounded generously, at the same scale as doctrine.md.
MAX_PERSONA_YAML_BYTES = 65536
MAX_PERSONA_MD_BYTES = 262144
MAX_EXAMPLES_BYTES = 65536
MAX_AVATAR_BYTES = 1048576
# J1 (foundation review r6): checked via stat() BEFORE read/parse, so a
# fetched manifest.json can't force an unbounded read/parse allocation —
# mirrors the pack-file size caps above.
MAX_MANIFEST_BYTES = 65536
_FILE_SIZE_CAPS: dict[str, int] = {
    "persona.yaml": MAX_PERSONA_YAML_BYTES,
    "persona.md": MAX_PERSONA_MD_BYTES,
    "examples.yaml": MAX_EXAMPLES_BYTES,
    AVATAR_FILE: MAX_AVATAR_BYTES,
}
_AXES = {
    "warmth", "formality", "candor", "attunement",
    "curiosity", "levity", "social_energy", "optimism",
}


class PersonaPackError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PersonaManifest:
    files: tuple[Mapping[str, object], ...]
    checksum: str


@dataclass(frozen=True, slots=True)
class PersonaPack:
    persona_id: str
    version: str
    trait_schema_version: int
    identity: Mapping[str, object]
    relationship_posture: str
    archetype: str
    traits: Mapping[str, int]
    quirks: tuple[Mapping[str, str], ...]
    markdown: str
    examples: tuple[Mapping[str, str], ...]
    manifest: PersonaManifest
    checksum: str
    # The pack's avatar image (a JPEG, in the pack directory it was loaded
    # from), or None when the pack carries none. Its bytes are in the
    # manifest, so they are part of ``checksum``.
    avatar_path: Path | None = None


def _admit_files(pack_dir: Path) -> tuple[Path, ...]:
    # J3 (foundation review r6): directory listing can raise a raw OSError
    # (missing dir, permission error, an admission/read race) — scoped to
    # the iteration only, so a genuine downstream logic bug still surfaces
    # on its own terms.
    try:
        entries = sorted(pack_dir.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise PersonaPackError(f"persona directory unreadable: {exc}") from exc
    names = {entry.name for entry in entries}
    if not _REQUIRED <= names or names - _REQUIRED - _OPTIONAL:
        raise PersonaPackError("persona pack file set is invalid")
    admitted = []
    for path in entries:
        try:
            info = path.lstat()
        except OSError as exc:
            raise PersonaPackError(f"persona directory unreadable: {exc}") from exc
        if path.name.startswith(".") or not stat.S_ISREG(info.st_mode):
            raise PersonaPackError(f"invalid persona file: {path.name}")
        if info.st_nlink != 1:
            raise PersonaPackError(f"hard-linked persona file: {path.name}")
        if info.st_mode & 0o111:
            raise PersonaPackError(f"executable persona file: {path.name}")
        if info.st_size > _FILE_SIZE_CAPS[path.name]:
            raise PersonaPackError(f"persona file too large: {path.name}")
        admitted.append(path)
    return tuple(admitted)


def _reject_markers(text: str) -> None:
    try:
        reject_forbidden_markers(text)
    except ValueError as exc:
        raise PersonaPackError(str(exc)) from exc


def _validate_schema(payload: object, schema_path: Path) -> None:
    # jsonschema.validate raises jsonschema.exceptions.ValidationError on
    # its own — the loader's contract is that every rejection surfaces as
    # PersonaPackError, so wrap it here rather than leaking the jsonschema
    # exception type to callers.
    try:
        jsonschema.validate(payload, json.loads(schema_path.read_text()))
    except jsonschema.exceptions.ValidationError as exc:
        raise PersonaPackError(f"schema validation failed: {exc.message}") from exc


def load_persona_pack(pack_dir: Path, manifest_path: Path) -> PersonaPack:
    files = _admit_files(pack_dir)
    canonical_files = {}
    manifest_rows = []
    avatar_path = None
    for path in files:
        if path.name in _BINARY:
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise PersonaPackError(f"persona file unreadable: {path.name}") from exc
            # The admitted size was checked before the read; re-check what was
            # actually read, since the file can change in between.
            if len(data) > _FILE_SIZE_CAPS[path.name]:
                raise PersonaPackError(f"persona file too large: {path.name}")
            if not data.startswith(_JPEG_MAGIC):
                raise PersonaPackError(f"persona avatar is not a JPEG image: {path.name}")
            avatar_path = path
            manifest_rows.append({
                "path": path.name,
                "type": "file",
                "executable": False,
                "checksum": checksum_bytes(data),
            })
            continue
        # H2 (foundation review r5): the read/decode itself sat OUTSIDE any
        # exception boundary, so an admitted file containing an invalid
        # UTF-8 byte raised a raw UnicodeDecodeError instead of folding
        # into this loader's PersonaPackError contract like every other
        # rejection path here. Scoped to the read/decode only — later
        # validation (_reject_markers etc.) still raises on its own terms.
        try:
            text = canonical_text(path.read_text(encoding="utf-8"))
        except OSError as exc:
            # J3 (foundation review r6): the read itself (not just the
            # admitted lstat above) can still race and raise a raw OSError
            # (e.g. the file vanishing between admission and read).
            raise PersonaPackError(f"persona file unreadable: {path.name}") from exc
        except UnicodeDecodeError as exc:
            raise PersonaPackError(
                f"persona file is not valid UTF-8: {path.name}"
            ) from exc
        _reject_markers(text)
        canonical_files[path.name] = text
        manifest_rows.append({
            "path": path.name,
            "type": "file",
            "executable": False,
            "checksum": checksum_bytes(text.encode("utf-8")),
        })

    try:
        # F-C (foundation review r3, P0 DoS): load_yaml_no_aliases forbids
        # YAML aliases outright — see role_artifact.py's matching comment
        # for the DAG-amplification-DoS rationale. Anchors with no alias
        # remain permitted.
        persona = load_yaml_no_aliases(canonical_files["persona.yaml"])
    except (yaml.YAMLError, RecursionError, ValueError, UnicodeError) as exc:
        # F-B (foundation review r3, P0): a hostile deeply-nested flow
        # scalar makes yaml's own PARSER recurse, well under any size cap
        # here — this happens INSIDE the parse call, before
        # assert_json_safe below (which only bounds depth in the
        # ALREADY-PARSED tree) ever runs. Fold a genuine YAML syntax
        # error, a forbidden-alias rejection, and a parser RecursionError
        # into the same generic, fail-closed PersonaPackError.
        # G2 (foundation review r4, P1): PyYAML's own scanner also raises
        # a plain ValueError (e.g. "chr() arg not in range(0x110000)") for
        # an invalid Unicode escape such as "\U00110000" — neither
        # yaml.YAMLError nor RecursionError, so it previously escaped this
        # boundary and leaked the raw parser-internal message. Catch
        # ValueError/UnicodeError here too (scoped to this parse call
        # only) so ANY parse failure folds into the same generic error.
        raise PersonaPackError(
            "persona.yaml contains invalid or unparseable YAML"
        ) from exc
    # R1 (foundation review r2): guarantees freeze-soundness (deep_freeze
    # below) and rejects any non-JSON type (set/bytes/datetime/cyclic) a
    # YAML tag/alias could otherwise smuggle through yaml.safe_load.
    try:
        assert_json_safe(persona)
    except ValueError as exc:
        raise PersonaPackError(
            f"persona.yaml contains non-JSON-native content: {exc}"
        ) from exc
    # F-A (foundation review r3): persona.yaml is already raw-text
    # marker-scanned above via _reject_markers (kept as defense in depth),
    # but a YAML string escape (e.g. "\x24\x7bOVERRIDE\x7d") has no
    # literal marker bytes in the raw source — yaml.safe_load DECODES it
    # into the live marker. Scan the PARSED string leaves too, which sees
    # the value only after decoding and so cannot be bypassed this way.
    try:
        reject_markers_in_parsed(persona)
    except ValueError as exc:
        raise PersonaPackError(str(exc)) from exc
    schema_path = (
        Path(__file__).parent / "defaults/schema/persona.v1.json"
    )
    _validate_schema(persona, schema_path)
    if set(persona["traits"]) != _AXES:
        raise PersonaPackError("traits must contain exactly the eight v1 axes")

    try:
        parsed_sections = sections(canonical_files["persona.md"])
    except ValueError as exc:
        raise PersonaPackError(f"persona.md failed Markdown validation: {exc}") from exc
    core_bodies = [body for level, name, body in parsed_sections
                   if level == 1 and name == "Core"]
    if len(core_bodies) != 1:
        raise PersonaPackError("exactly one # Core section is required")
    core_length = len(core_bodies[0].strip())
    if not 300 <= core_length <= 500:
        raise PersonaPackError("Core body must contain 300–500 characters")
    if not any(level == 2 and name == "Negative space"
               for level, name, _ in parsed_sections):
        raise PersonaPackError("## Negative space is required")

    examples = ()
    if "examples.yaml" in canonical_files:
        try:
            # F-C (foundation review r3, P0 DoS): same alias-forbidding
            # parse as persona.yaml above.
            raw_examples = load_yaml_no_aliases(canonical_files["examples.yaml"])
        except (yaml.YAMLError, RecursionError, ValueError, UnicodeError) as exc:
            # F-B (foundation review r3, P0): same parser-recursion hazard
            # as persona.yaml above. G2 (foundation review r4, P1): same
            # bare-ValueError-from-invalid-Unicode-escape hazard too.
            raise PersonaPackError(
                "examples.yaml contains invalid or unparseable YAML"
            ) from exc
        try:
            assert_json_safe(raw_examples)
        except ValueError as exc:
            raise PersonaPackError(
                f"examples.yaml contains non-JSON-native content: {exc}"
            ) from exc
        # F-A (foundation review r3): same parsed-leaf marker scan as
        # persona.yaml above — examples.yaml's raw text is already
        # marker-scanned via _reject_markers, but a YAML string escape is
        # only visible after yaml.safe_load decodes it.
        try:
            reject_markers_in_parsed(raw_examples)
        except ValueError as exc:
            raise PersonaPackError(str(exc)) from exc
        example_schema = (
            Path(__file__).parent / "defaults/schema/persona-examples.v1.json"
        )
        _validate_schema(raw_examples, example_schema)
        examples = tuple(
            deep_freeze(value)
            for value in raw_examples.get("examples", [])
        )

    manifest_payload = {
        "api_version": "casa.persona.manifest/v1",
        "files": manifest_rows,
    }
    manifest_checksum = checksum_bytes(canonical_json_bytes(manifest_payload))
    manifest_payload["checksum"] = manifest_checksum
    # J1 (foundation review r6, P0): a manifest.json well under any prior
    # cap could still be a hostile deeply-nested JSON document (e.g.
    # "[" * N + "0" + "]" * N) — json.loads's own parser recurses on it,
    # raising a raw RecursionError that escaped this boundary entirely.
    # Cap the file size (checked via stat(), before any read) the same way
    # the admitted pack files are capped, and fold RecursionError into the
    # same generic PersonaPackError as every other manifest-read/parse
    # rejection below.
    try:
        manifest_size = manifest_path.stat().st_size
    except OSError as exc:
        raise PersonaPackError(f"persona manifest could not be read: {exc}") from exc
    if manifest_size > MAX_MANIFEST_BYTES:
        raise PersonaPackError("persona manifest file too large")
    try:
        # H2 (foundation review r5): UnicodeDecodeError was not caught
        # here alongside OSError/json.JSONDecodeError, so an invalid UTF-8
        # byte in manifest.json escaped this boundary as a raw
        # UnicodeDecodeError instead of PersonaPackError.
        # r6 close-out: catch bare ValueError too — json.loads raises a
        # plain ValueError (not JSONDecodeError) for an integer literal
        # over CPython's int-string-digit limit, which is reachable under
        # the size cap. ValueError subsumes json.JSONDecodeError and
        # UnicodeDecodeError, making the manifest parse boundary total.
        expected_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise PersonaPackError(f"persona manifest could not be read: {exc}") from exc
    if expected_manifest != manifest_payload:
        raise PersonaPackError("persona manifest does not match admitted files")

    return PersonaPack(
        persona_id=persona["id"],
        version=persona["version"],
        trait_schema_version=persona["trait_schema_version"],
        identity=deep_freeze(persona["identity"]),
        relationship_posture=persona["relationship_posture"],
        archetype=persona["archetype"],
        traits=deep_freeze(persona["traits"]),
        quirks=tuple(deep_freeze(q) for q in persona.get("quirks", [])),
        markdown=canonical_files["persona.md"],
        examples=examples,
        manifest=PersonaManifest(
            files=tuple(deep_freeze(row) for row in manifest_rows),
            checksum=manifest_checksum,
        ),
        checksum=manifest_checksum,
        avatar_path=avatar_path,
    )
