"""N2's production-export tooling (spec §4.5). Lives under
casa/rootfs/opt/casa/ — the ONE directory tests/conftest.py already
puts on sys.path — no new sys.path plumbing, no casa/scripts/
directory. This module reads the image tree and writes a component-repo
bundle; it never mutates the image itself (transitional image content is
removed via ordinary file deletion + git, not through this module).

#427: `export_mtg_component` is gone with the shipped judge persona pack it
read — the MTG component was exported once and now lives (with its persona,
corpus and plugin) in its own component repository; the image defaults carry
no MTG content."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ExportBundle:
    component_id: str
    version: str
    slug: str
    files: Mapping[str, bytes]


def _persona_bundle_files(pack_dir: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for name in ("persona.yaml", "persona.md"):
        out[f"persona/pack/{name}"] = (pack_dir / name).read_bytes()
    if (pack_dir / "examples.yaml").is_file():
        out["persona/pack/examples.yaml"] = (pack_dir / "examples.yaml").read_bytes()
    return out


def _build_manifest(
    *, component_id: str, version: str, role_files: dict[str, bytes], config_schema: bytes,
    default_persona_ref: str, default_persona_checksum: str, dependencies: list[dict],
) -> bytes:
    from specialist_component import compute_component_checksum

    files = {
        "role/role.yaml": role_files["role/role.yaml"], "role/doctrine.md": role_files["role/doctrine.md"],
        "config-schema.json": config_schema,
    }
    checksum = compute_component_checksum(files)
    manifest = {
        "api_version": "casa.specialist-component/v1", "component_id": component_id,
        "version": version,
        "default_persona": {"ref": default_persona_ref, "checksum": default_persona_checksum},
        "dependencies": dependencies, "checksum": checksum,
    }
    return json.dumps(manifest, indent=2, sort_keys=False).encode("utf-8") + b"\n"


def export_finance_component(
    *, defaults_root: Path = Path("casa/rootfs/opt/casa/defaults"),
) -> ExportBundle:
    from persona_pack import load_persona_pack

    role_dir = defaults_root / "roles" / "specialist" / "finance"
    role_files = {
        "role/role.yaml": (role_dir / "role.yaml").read_bytes(),
        "role/doctrine.md": (role_dir / "doctrine.md").read_bytes(),
    }
    persona_dir = defaults_root / "personas" / "casa" / "alex" / "0.1.0"
    pack = load_persona_pack(persona_dir / "pack", persona_dir / "manifest.json")
    config_schema = json.dumps({"required": [], "secret_names": []}).encode("utf-8")
    persona_ref = f"{pack.persona_id}@{pack.version}"
    manifest = _build_manifest(
        component_id="casa/finance", version="0.1.0", role_files=role_files,
        config_schema=config_schema, default_persona_ref=persona_ref,
        default_persona_checksum=pack.checksum,
        dependencies=[{"kind": "persona", "identifier": persona_ref, "digest": pack.checksum}],
    )
    files: dict[str, bytes] = {
        "manifest.json": manifest, **role_files, "config-schema.json": config_schema,
        **_persona_bundle_files(persona_dir / "pack"),
        "persona/manifest.json": (persona_dir / "manifest.json").read_bytes(),
    }
    return ExportBundle(component_id="casa/finance", version="0.1.0", slug="finance", files=files)


def write_export_bundle(bundle: ExportBundle, output_dir: Path) -> None:
    """A general, reusable, Mapping[str, bytes]-keyed writer — it must not
    implicitly trust its own future callers even though every caller in this
    module today only ever passes fixed internal keys. Two invariants,
    enforced BEFORE any byte is written: (1) a clean-output policy —
    `output_dir` must not already exist with content, so a stale file from a
    PREVIOUS export run can never silently survive into a new one; (2) path
    containment — every `rel_path` must resolve to a descendant of
    `output_dir`, matching the same no-escape invariant
    `plugin_store.safe_extract_tar` already enforces for untrusted archive
    members."""
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(
            f"{output_dir}: already exists and is not empty — export requires "
            "a clean output directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_root = output_dir.resolve()
    for rel_path, content in bundle.files.items():
        candidate = Path(rel_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"export bundle key {rel_path!r} is not a safe relative path")
        target = output_dir / candidate
        if resolved_root not in target.resolve().parents and target.resolve() != resolved_root:
            raise ValueError(f"export bundle key {rel_path!r} escapes {output_dir}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def validate_export_bundle_self_consistency(bundle: ExportBundle) -> None:
    """Spec §4.5's 'pre-cutover validation' step: proves the bundle is
    internally coherent (loads, compiles, checksums match) WITHOUT touching
    the image-collision check at all — deliberately independent of whether
    the image still ships a same-slug bundled specialist (that check is
    check_slug_uniqueness, exercised separately via inspect_specialist_repo
    in the clean-image-install test)."""
    import tempfile

    from specialist_component import load_specialist_component
    from prompt_compiler import compile_prompt_bundle
    from role_slot import materialize_role
    from role_artifact import load_role_artifact
    from persona_pack import load_persona_pack
    from personality_binding import materialize_component_default_binding

    with tempfile.TemporaryDirectory() as td:
        component_dir = Path(td)
        write_export_bundle(bundle, component_dir)
        component = load_specialist_component(component_dir, component_dir / "manifest.json")
        role = materialize_role(source=load_role_artifact(component_dir / "role"), options={})
        persona = load_persona_pack(
            component_dir / "persona" / "pack", component_dir / "persona" / "manifest.json")
        binding = materialize_component_default_binding(
            role=role, persona=persona,
            component_root=f"{component.component_id}@{component.version}#{component.checksum}",
        )
        compile_prompt_bundle(
            role=role, persona=persona, binding=binding,
            platform_frame=(Path(__file__).parent / "defaults" / "personality"
                             / "platform-frame.md").read_text(encoding="utf-8"),
            safety_kernel=(Path(__file__).parent / "defaults" / "personality"
                           / "safety-kernel.md").read_text(encoding="utf-8"),
        )  # raises ValueError on any incompatibility/ceiling violation
