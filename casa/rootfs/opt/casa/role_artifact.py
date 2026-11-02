from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import jsonschema
import yaml

from authored_markers import contains_forbidden_marker, reject_markers_in_parsed
from canonical_bytes import assert_json_safe, canonical_text, deep_freeze
from markdown_sections import validate_markdown
from yaml_safety import load_yaml_no_aliases

# Byte-size caps checked BEFORE read_text, so a hostile artifact can't force
# an unbounded allocation. role.yaml is a small structured config file;
# doctrine.md is free-form prose but still bounded generously.
MAX_ROLE_YAML_BYTES = 65536
MAX_DOCTRINE_BYTES = 262144
_EXPECTED_FILES: dict[str, int] = {
    "role.yaml": MAX_ROLE_YAML_BYTES,
    "doctrine.md": MAX_DOCTRINE_BYTES,
}


@dataclass(frozen=True, slots=True)
class RoleArtifactSource:
    role: Mapping[str, object]
    doctrine: str
    role_path: Path
    doctrine_path: Path


def _reject_markers(text: str) -> None:
    if contains_forbidden_marker(text):
        raise ValueError("template, include, HTML, or delimiter detected")


# role.yaml's marker check runs on PARSED string leaves (dict keys AND
# values, via authored_markers.reject_markers_in_parsed), not raw source
# text. role.v1.json intentionally leaves some fields open
# (``disclosure.overrides``, ``tts.error_phrases`` values) as free-form
# strings — that authored-content surface is exactly what this check must
# cover. Raw-text scanning is deliberately NOT used here (unlike
# doctrine.md/persona content) because role.yaml's own flow-style YAML
# syntax legitimately produces adjacent closing braces (e.g.
# ``disclosure: {policy: standard, overrides: {}}``), which collides
# byte-for-byte with the forbidden ``}}`` template-close marker in every
# shipped role artifact. Scanning only the parsed string leaves catches
# every real injection surface (any string value the schema allows)
# without that false positive, and — per foundation review r3, F-A — is
# also immune to a marker hidden behind a YAML string escape, since it
# only ever sees the DECODED value.


def _admit_files(role_dir: Path) -> dict[str, Path]:
    """Admit exactly {role.yaml, doctrine.md} from *role_dir* as an
    adversarial trust gate — mirrors persona_pack.py's _admit_files.
    Uses lstat (never is_file(), which follows symlinks) so a hostile
    artifact directory can't substitute a symlink, hard link, or
    executable file for either expected entry, and rejects any hidden or
    unexpected entry outright rather than silently ignoring it."""
    # J3 (foundation review r6): directory listing can raise a raw OSError
    # (missing dir, permission error, an admission/read race) — scoped to
    # the iteration only, so a genuine downstream logic bug still surfaces
    # on its own terms.
    try:
        entries = sorted(role_dir.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ValueError(f"role directory unreadable: {exc}") from exc
    names = {entry.name for entry in entries}
    if names != set(_EXPECTED_FILES):
        raise ValueError(
            f"role artifact must contain exactly {sorted(_EXPECTED_FILES)}"
        )
    admitted: dict[str, Path] = {}
    for path in entries:
        try:
            info = path.lstat()
        except OSError as exc:
            raise ValueError(f"role directory unreadable: {exc}") from exc
        if path.name.startswith(".") or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"invalid role artifact file: {path.name}")
        if info.st_nlink != 1:
            raise ValueError(f"hard-linked role artifact file: {path.name}")
        if info.st_mode & 0o111:
            raise ValueError(f"executable role artifact file: {path.name}")
        cap = _EXPECTED_FILES[path.name]
        if info.st_size > cap:
            raise ValueError(f"role artifact file too large: {path.name}")
        admitted[path.name] = path
    return admitted


def load_role_artifact(role_dir: Path) -> RoleArtifactSource:
    admitted = _admit_files(role_dir)
    role_path = admitted["role.yaml"]
    doctrine_path = admitted["doctrine.md"]

    # H2 (foundation review r5, role-side consistency): a raw
    # UnicodeDecodeError already stays in the ValueError family (it
    # subclasses ValueError), so this loader's "raises ValueError" contract
    # technically held even before this wrap — but fold it into the same
    # clean, generic message as the parse-step rejections below rather than
    # leak the raw codec error text.
    try:
        role_text = canonical_text(role_path.read_text(encoding="utf-8"))
    except OSError as exc:
        # J3 (foundation review r6): the read itself (not just the admitted
        # lstat above) can still race and raise a raw OSError (e.g. the
        # file vanishing between admission and read).
        raise ValueError("role.yaml unreadable") from exc
    except UnicodeDecodeError as exc:
        raise ValueError("role.yaml has invalid encoding (not UTF-8)") from exc
    try:
        # F-C (foundation review r3, P0 DoS): load_yaml_no_aliases forbids
        # YAML aliases outright (yaml_safety._NoAliasSafeLoader) — a
        # fetched role.yaml could otherwise use aliases to build an
        # exponentially-expanding DAG (tiny on disk, shallow, yet ~2^30
        # leaves once walked) that turns assert_json_safe/deep_freeze
        # below into a CPU + memory DoS. Anchors with no alias are inert
        # and remain permitted.
        raw = load_yaml_no_aliases(role_text)
    except (yaml.YAMLError, RecursionError, ValueError, UnicodeError) as exc:
        # F-B (foundation review r3, P0): a hostile deeply-nested flow
        # scalar (e.g. "[" * 2000 + "0" + "]" * 2000), well under
        # MAX_ROLE_YAML_BYTES, makes yaml's own PARSER recurse — this
        # happens INSIDE the parse call, before assert_json_safe below
        # (which only bounds depth in the ALREADY-PARSED tree) ever runs.
        # Fold both a genuine YAML syntax error, a forbidden-alias
        # rejection, and a parser RecursionError into the same generic,
        # fail-closed ValueError. G2 (foundation review r4, P1): PyYAML's
        # own scanner also raises a plain ValueError (e.g. "chr() arg not
        # in range(0x110000)") for an invalid Unicode escape such as
        # "\U00110000" — neither yaml.YAMLError nor RecursionError, so it
        # previously escaped this boundary and leaked the raw
        # parser-internal message. Catch ValueError/UnicodeError here too
        # (scoped to this parse call only) so ANY parse failure folds into
        # the same generic, clean error.
        raise ValueError("role.yaml contains invalid or unparseable YAML") from exc
    # R1 (foundation review r2): yaml.safe_load can yield non-JSON-native
    # types (set/bytes/datetime/cyclic) via YAML tags/aliases that
    # role.v1.json's schema-open fields admit. Assert the parsed tree is
    # JSON-native BEFORE the marker walk / schema validation / deep_freeze
    # below, so none of them can crash on a cycle or be bypassed by a
    # marker hidden inside a non-dict/list/str container.
    assert_json_safe(raw)
    reject_markers_in_parsed(raw)
    schema_path = Path(__file__).parent / "defaults/schema/role.v1.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    # J2 (foundation review r6): jsonschema.validate raises
    # jsonschema.exceptions.ValidationError on its own, which inherits from
    # Exception, NOT ValueError — this loader's contract is that every
    # rejection surfaces as a ValueError, so wrap it here rather than
    # leaking the jsonschema exception type to callers. Mirrors
    # persona_pack.py's _validate_schema.
    try:
        jsonschema.validate(raw, schema)
    except jsonschema.exceptions.ValidationError as exc:
        raise ValueError(f"role schema validation failed: {exc.message}") from exc

    # H2 (foundation review r5, role-side consistency): same read/decode
    # boundary wrap as role.yaml above.
    try:
        doctrine = canonical_text(doctrine_path.read_text(encoding="utf-8"))
    except OSError as exc:
        # J3 (foundation review r6): same admission/read-race OSError
        # boundary as role.yaml above.
        raise ValueError("doctrine.md unreadable") from exc
    except UnicodeDecodeError as exc:
        raise ValueError("doctrine.md has invalid encoding (not UTF-8)") from exc
    if not doctrine.strip():
        raise ValueError("role doctrine is empty")
    _reject_markers(doctrine)
    try:
        validate_markdown(doctrine)
    except ValueError as exc:
        raise ValueError(f"role doctrine failed Markdown validation: {exc}") from exc
    # The prompt compiler carries only a doctrine's "# Core doctrine" section and
    # each surface's own projection section: a doctrine with neither (one titled
    # "# Fido doctrine") compiled to an empty <role_doctrine> on every surface and
    # reached no model, silently. Judged with the compiler's own selection.
    from prompt_compiler import _SURFACE_SECTIONS, doctrine_for_surface
    if not any(doctrine_for_surface(doctrine, s).strip() for s in _SURFACE_SECTIONS):
        raise ValueError("role doctrine has no '# Core doctrine' or projection section")
    if raw["doctrine_file"] != doctrine_path.name:
        raise ValueError("doctrine_file must resolve to doctrine.md")
    return RoleArtifactSource(
        role=deep_freeze(raw), doctrine=doctrine,
        role_path=role_path, doctrine_path=doctrine_path,
    )
