"""Tests for specialist_export.py — N2's production-export tooling (spec §4.5).

Post-cutover (Step 9): finance's role artifact no longer exists under the
image's defaults/roles/specialist/ tree, so these tests build a SYNTHETIC
defaults_root holding the exact finalized role.yaml/doctrine.md content the
export was validated against pre-cutover — the export tool + the real
role_artifact/specialist_component loaders it drives are still exercised
end-to-end. The alex persona pack is copied from the still-bundled real
image tree. (#427: export_mtg_component and the shipped judge persona pack
are gone — the MTG component lives in its own repository.)
"""
import json
import shutil

import pytest
from pathlib import Path

from specialist_export import (
    export_finance_component,
    validate_export_bundle_self_consistency,
    write_export_bundle,
)

_FINANCE_ROLE_YAML = """\
api_version: casa.role/v1
id: specialist:finance
kind: specialist
slot: finance
mission: Retrieve and explain household financial records using deterministic arithmetic.
enabled: false
model: {source: fixed, value: sonnet}
tools:
  allowed: [Read, Skill, mcp__casa-framework__get_schedule, mcp__casa-framework__send_media, mcp__casa-framework__ask_user]
  disallowed: [Bash, Write, Edit]
  permission_mode: acceptEdits
  max_turns: 10
  skills: all
  voice_guard: none
mcp_servers: [n8n-workflows, casa-framework]
channels: []
memory: {token_budget: 4000, read_strategy: per_turn}
session: {strategy: ephemeral, idle_timeout_seconds: 0}
disclosure: {policy: delegated, overrides: {}}
delegates: []
executors: []
triggers: []
hooks: {pre_tool_use: []}
tts: {tag_dialect: none, error_phrases: {}}
response:
  text: {register: precise, max_status_sentences: 3}
  voice: {register: spoken, max_status_sentences: 2}
  restricted_webhook: {register: plain, max_status_sentences: 2}
persona:
  policy: optional-but-bound
  compatibility: ["casa/alex@>=0.1.0 <1.0.0"]
requires: {plugins: [], tools: []}
doctrine_file: doctrine.md
"""

_FINANCE_DOCTRINE_MD = """\
# Core doctrine

Answer only finance-scoped delegations. Retrieve source records through assigned tools, route every arithmetic operation through the deterministic finance calculation path, distinguish source data from conclusions, and return a precise task-focused result. Treat recalled material as attributed prior evidence, not personal recollection.

## Text projection

Use concise prose and tables only when they make the figures easier to audit.

## Voice projection

Lead with the result, then give at most the essential supporting figures.

## Restricted webhook projection

Do not expose financial records or persona identity.
"""


def _build_synthetic_defaults_root(tmp_path: Path) -> Path:
    real_repo_root = Path(__file__).resolve().parents[1]
    real_defaults_root = real_repo_root / "casa" / "rootfs" / "opt" / "casa" / "defaults"

    root = tmp_path / "synthetic-defaults"
    finance_role_dir = root / "roles" / "specialist" / "finance"
    finance_role_dir.mkdir(parents=True)
    (finance_role_dir / "role.yaml").write_text(_FINANCE_ROLE_YAML, encoding="utf-8")
    (finance_role_dir / "doctrine.md").write_text(_FINANCE_DOCTRINE_MD, encoding="utf-8")

    src = real_defaults_root / "personas" / "casa" / "alex" / "0.1.0"
    dst = root / "personas" / "casa" / "alex" / "0.1.0"
    shutil.copytree(src, dst)

    return root


def test_export_finance_component_produces_a_self_consistent_bundle(tmp_path: Path) -> None:
    defaults_root = _build_synthetic_defaults_root(tmp_path)
    bundle = export_finance_component(defaults_root=defaults_root)
    assert bundle.slug == "finance"
    assert "manifest.json" in bundle.files
    assert "role/role.yaml" in bundle.files
    assert "role/doctrine.md" in bundle.files
    assert "persona/pack/persona.yaml" in bundle.files
    manifest = json.loads(bundle.files["manifest.json"])
    assert manifest["default_persona"]["ref"].startswith("casa/alex@")


def test_export_finance_component_bundle_writes_and_self_validates(tmp_path: Path) -> None:
    defaults_root = _build_synthetic_defaults_root(tmp_path)
    bundle = export_finance_component(defaults_root=defaults_root)
    write_export_bundle(bundle, tmp_path / "finance-export")
    validate_export_bundle_self_consistency(bundle)  # raises on any inconsistency — no exception here


def test_clean_image_install_of_the_exported_finance_bundle_succeeds(tmp_path: Path, monkeypatch) -> None:
    """Proves inspect_specialist_repo's check_slug_uniqueness passes for the
    exported finance bundle against the image's ACTUAL role slots — post-Step-9,
    that is simply the real, current _discover_image_role_slots() result (finance
    is genuinely gone now), so no synthetic clean-roles patch is needed anymore."""
    from specialist_export import export_finance_component, write_export_bundle
    from specialist_install import inspect_specialist_repo
    from specialist_registry import InstalledSpecialistIndex

    defaults_root = _build_synthetic_defaults_root(tmp_path)
    bundle = export_finance_component(defaults_root=defaults_root)
    fetched_repo = tmp_path / "fetched-finance-repo"
    write_export_bundle(bundle, fetched_repo)

    def _fake_resolve_and_fetch(repo, ref, subdir, dest, *, expected_revision=None):
        shutil.copytree(fetched_repo, dest)
        return "0" * 40

    monkeypatch.setattr("specialist_install.resolve_and_fetch", _fake_resolve_and_fetch)

    result = inspect_specialist_repo(
        "casa-org/casa-specialist-finance", "v0.1.0",
        staging_root=tmp_path / "staging", installed_index=InstalledSpecialistIndex(
            specialists_dir=str(tmp_path / "specialists")),
        receipts_dir=tmp_path / "receipts",
    )
    assert result.slug == "finance"  # no SpecialistInstallError("slug_collision", ...) raised


