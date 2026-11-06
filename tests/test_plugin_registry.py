"""Unified plugin architecture §3.1/§3.2: registry schema, identity, seeding.

Identity: artifact_id = SHA-256(repo_norm\nrevision\nsubdir_norm\nname), UTF-8.
Failure scoping: registry-wide invalid (unparseable/bad schema_version) vs
per-entry invalid (entry skipped + issue; the rest resolve normally).
Seeding: a default is added ONLY if absent from BOTH plugins and
seeded_defaults; auto-seeded names are appended to seeded_defaults forever.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from plugin_registry import (
    RegistryData,
    compute_artifact_id,
    load_registry,
    normalize_repo,
    normalize_subdir,
    save_registry,
    seed_defaults,
)

pytestmark = pytest.mark.unit


def _entry(name="lantern-invoice", **kw):
    e = {
        "name": name,
        "source": {
            "type": "github",
            "repo": "bonzanni/casa-plugin-lantern-invoice",
            "ref": "master",
            "revision": "git:" + "2e" * 20,
            "subdir": "",
        },
        "version": "1.2.0",
        "targets": ["specialist:finance"],
    }
    e["artifact_id"] = compute_artifact_id(
        repo=e["source"]["repo"], revision=e["source"]["revision"],
        subdir=e["source"]["subdir"], name=name,
    )
    e.update(kw)
    return e


def _write(tmp_path, doc) -> Path:
    p = tmp_path / "registry.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_identity_exact_vector():
    # Locked vector: identity must never drift (it addresses artifacts on disk).
    ident = "\n".join([
        "obra/superpowers",
        "git:f2cbfbefebbfef77321e4c9abc9e949826bea9d7",
        "",
        "superpowers",
    ]).encode("utf-8")
    assert compute_artifact_id(
        repo="Obra/Superpowers",
        revision="git:f2cbfbefebbfef77321e4c9abc9e949826bea9d7",
        subdir="", name="superpowers",
    ) == hashlib.sha256(ident).hexdigest() == (
        "cf07094ce09695b6b29100276efe6bae9fbdde2be0ccf2dfb6bc27b2029e4966"
    )


def test_normalizers():
    assert normalize_repo("Owner/Repo") == "owner/repo"
    assert normalize_subdir("") == ""
    assert normalize_subdir("/plugins/x/") == "plugins/x"
    assert normalize_subdir("a//b/") == "a/b"
    for bad in ("../x", "a/./b", "a/../b", "a\\b"):
        with pytest.raises(ValueError):
            normalize_subdir(bad)


def test_traversal_subdir_is_per_entry_invalid(tmp_path):
    e = _entry()
    e["source"]["subdir"] = "../evil"        # artifact_id left stale on purpose
    p = _write(tmp_path, {"schema_version": 1, "plugins": [e]})
    data = load_registry(p)
    assert data.entries == [] and data.entry_issues[0].reason_code == "entry_invalid"


def test_malformed_seeded_defaults_is_registry_wide_invalid(tmp_path):
    p = _write(tmp_path, {"schema_version": 1, "seeded_defaults": "oops",
                          "plugins": [_entry()]})
    assert load_registry(p).valid is False


def test_load_valid_registry(tmp_path):
    p = _write(tmp_path, {"schema_version": 1, "plugins": [_entry()]})
    data = load_registry(p)
    assert data.valid is True
    assert [e["name"] for e in data.entries] == ["lantern-invoice"]
    assert data.entry_issues == []


def test_missing_file_is_valid_empty(tmp_path):
    data = load_registry(tmp_path / "absent.json")
    assert data.valid is True and data.entries == []


def test_unparseable_json_registry_wide_invalid(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text("{nope", encoding="utf-8")
    data = load_registry(p)
    assert data.valid is False and data.entries == []


def test_unsupported_schema_version_registry_wide_invalid(tmp_path):
    p = _write(tmp_path, {"schema_version": 99, "plugins": [_entry()]})
    assert load_registry(p).valid is False


def test_malformed_entry_is_per_entry_skip(tmp_path):
    bad = _entry(name="Bad_Name!")  # violates NAME_RE
    good = _entry()
    p = _write(tmp_path, {"schema_version": 1, "plugins": [bad, good]})
    data = load_registry(p)
    assert data.valid is True
    assert [e["name"] for e in data.entries] == ["lantern-invoice"]
    assert len(data.entry_issues) == 1
    assert data.entry_issues[0].reason_code == "entry_invalid"


def test_bad_target_grammar_is_per_entry_skip(tmp_path):
    bad = _entry(targets=["butler"])  # missing tier prefix
    p = _write(tmp_path, {"schema_version": 1, "plugins": [bad]})
    data = load_registry(p)
    assert data.entries == [] and data.entry_issues[0].reason_code == "entry_invalid"


def test_duplicate_name_skips_both(tmp_path):
    p = _write(tmp_path, {"schema_version": 1, "plugins": [_entry(), _entry(version="9.9.9")]})
    data = load_registry(p)
    assert data.entries == []
    assert {i.reason_code for i in data.entry_issues} == {"duplicate_name"}
    assert len(data.entry_issues) == 2


def test_artifact_id_mismatch_is_per_entry_skip(tmp_path):
    """Pins INV-PLUG-001. Red case demonstrated: neutering _entry_error's
    artifact-id mismatch check (`if False:`) fails this test."""
    bad = _entry(artifact_id="0" * 64)
    p = _write(tmp_path, {"schema_version": 1, "plugins": [bad]})
    data = load_registry(p)
    assert data.entries == [] and data.entry_issues[0].reason_code == "entry_invalid"


def test_legacy_content_revision_is_serializable(tmp_path):
    src = {
        "type": "github", "repo": "x/y", "ref": "master",
        "revision": "legacy-content:" + "ab" * 32, "subdir": "",
    }
    e = _entry()
    e["source"] = src
    e["artifact_id"] = compute_artifact_id(
        repo="x/y", revision=src["revision"], subdir="", name=e["name"])
    p = _write(tmp_path, {"schema_version": 1, "plugins": [e]})
    data = load_registry(p)
    assert data.valid and len(data.entries) == 1


def test_unknown_fields_preserved_roundtrip(tmp_path):
    doc = {"schema_version": 1, "future_field": {"x": 1},
           "plugins": [dict(_entry(), custom_note="keep me")]}
    p = _write(tmp_path, doc)
    data = load_registry(p)
    save_registry(data, p)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert out["future_field"] == {"x": 1}
    assert out["plugins"][0]["custom_note"] == "keep me"


def test_seed_defaults_no_resurrection(tmp_path):
    default = _write(tmp_path, {"schema_version": 1, "plugins": [_entry(name="superpowers")]})
    # Fresh registry: default arrives once + bookkeeping.
    reg = tmp_path / "r.json"
    data = load_registry(reg)
    assert seed_defaults(data, default) is True
    assert [e["name"] for e in data.entries] == ["superpowers"]
    assert data.raw["seeded_defaults"] == ["superpowers"]
    save_registry(data, reg)
    # Operator removes the plugin; seeded_defaults keeps the name.
    doc = json.loads(reg.read_text(encoding="utf-8"))
    doc["plugins"] = []
    reg.write_text(json.dumps(doc), encoding="utf-8")
    data2 = load_registry(reg)
    assert seed_defaults(data2, default) is False       # NOT re-added
    assert data2.entries == []


def test_seed_defaults_adds_new_default_without_resurrecting_removed(tmp_path):
    """v0.72.0 (migration removed): on an EXISTING install a newly-introduced
    default is added while an operator-removed default (recorded in
    seeded_defaults) is NOT resurrected. The seeded_defaults ledger — not any
    migration sentinel — is the sole guard, so unconditional boot seeding is safe."""
    # A newer release's catalog ships BOTH the previously-removed default and a
    # brand-new one.
    default = _write(tmp_path, {"schema_version": 1,
                                "plugins": [_entry(name="old-removed"),
                                            _entry(name="brand-new")]})
    reg = tmp_path / "r.json"
    # Existing registry: old-removed was seeded then removed (ledger retains it);
    # an operator-installed plugin is present.
    reg.write_text(json.dumps({"schema_version": 1,
                               "seeded_defaults": ["old-removed"],
                               "plugins": [_entry(name="mine")]}), encoding="utf-8")
    data = load_registry(reg)
    assert seed_defaults(data, default) is True
    names = sorted(e["name"] for e in data.entries)
    assert names == ["brand-new", "mine"]        # new added; removed NOT resurrected
    assert sorted(data.raw["seeded_defaults"]) == ["brand-new", "old-removed"]


def test_seed_defaults_skips_present_plugin_without_bookkeeping_dup(tmp_path):
    default = _write(tmp_path, {"schema_version": 1, "plugins": [_entry(name="superpowers")]})
    reg = tmp_path / "r.json"
    d = load_registry(reg)
    seed_defaults(d, default)
    save_registry(d, reg)
    d2 = load_registry(reg)
    assert seed_defaults(d2, default) is False           # idempotent
    assert d2.raw["seeded_defaults"] == ["superpowers"]
