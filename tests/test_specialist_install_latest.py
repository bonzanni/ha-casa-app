"""INV-SPEC-018 pins (#1007): the ref literal ``latest`` on the specialist path.

``plugin_add`` resolves ``latest`` server-side to the newest published
release tag (INV-PLUG-022); ``inspect_specialist_repo`` used to hand the
literal to the commits API and refuse with ``ref_not_found``. These tests pin
the specialist path to the same contract: the tag, never the literal, is the
ref the receipt records and the result reports; the fetch is guarded against
the peeled commit; a repository with no published release is refused before
anything is staged.

Every test stubs ``specialist_install.resolve_and_fetch`` (the network/git
seam, exactly as tests/test_specialist_install.py does) and
``plugin_store.resolve_latest_release`` (the release lookup). ``plugin_store.
resolve_ref`` is patched to FAIL: the literal must never reach a commit
lookup.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import plugin_store
import specialist_install
import specialist_receipt
from personality_binding import InstanceDir
from specialist_install import SpecialistInstallError
from specialist_registry import InstalledSpecialistIndex
from test_specialist_install import (
    _specialist_binding, _specialist_tuple, _stub_resolve_and_fetch, _write_component,
)

TAG = "v1.2.0"
PEELED = "b" * 40


def _recording_fetch(component_root: Path):
    """resolve_and_fetch stub that also records (repo, ref, expected_revision)."""
    calls: list[tuple[str, str, str | None]] = []
    inner = _stub_resolve_and_fetch(component_root)

    def _stub(repo: str, ref: str, subdir: str, dest: Path, *,
              expected_revision: str | None = None) -> str:
        calls.append((repo, ref, expected_revision))
        return inner(repo, ref, subdir, dest, expected_revision=expected_revision)

    return _stub, calls


def _never_commits_lookup(repo: str, ref: str, **_kw) -> str:
    raise AssertionError(f"commits lookup reached for {repo}@{ref}")


def _index(tmp_path: Path) -> InstalledSpecialistIndex:
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()
    return index


def test_latest_resolves_to_the_release_tag_and_guards_the_fetch_on_its_peel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_component(tmp_path / "component", slug="fresh-specialist")
    stub, calls = _recording_fetch(root)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", stub)
    monkeypatch.setattr(plugin_store, "resolve_latest_release", lambda repo, **_k: (TAG, PEELED))
    monkeypatch.setattr(plugin_store, "resolve_ref", _never_commits_lookup)

    result = specialist_install.inspect_specialist_repo(
        "org/repo", "latest", staging_root=tmp_path / "staging",
        installed_index=_index(tmp_path), receipts_dir=tmp_path / "receipts",
    )

    # The fetch sees the TAG, guarded against the peeled commit — never the literal.
    assert calls == [("org/repo", TAG, PEELED)]
    assert result.resolved_ref == TAG
    receipt = specialist_receipt.load(result.receipt_id, receipts_dir=tmp_path / "receipts")
    assert receipt is not None
    assert receipt.component_ref == TAG


def test_an_exact_ref_is_its_own_resolved_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_component(tmp_path / "component", slug="fresh-specialist")
    stub, calls = _recording_fetch(root)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", stub)

    def _no_release_lookup(repo: str, **_k):
        raise AssertionError("release lookup reached for an exact ref")

    monkeypatch.setattr(plugin_store, "resolve_latest_release", _no_release_lookup)

    result = specialist_install.inspect_specialist_repo(
        "org/repo", "main", staging_root=tmp_path / "staging",
        installed_index=_index(tmp_path), receipts_dir=tmp_path / "receipts",
    )
    assert calls == [("org/repo", "main", None)]
    assert result.resolved_ref == "main"


def test_no_published_release_is_refused_before_anything_is_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_component(tmp_path / "component", slug="fresh-specialist")
    stub, calls = _recording_fetch(root)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", stub)

    def _none(repo: str, **_k):
        raise plugin_store.NoReleaseFound(f"{repo}: no published release tag (v<semver>) found")

    monkeypatch.setattr(plugin_store, "resolve_latest_release", _none)
    monkeypatch.setattr(plugin_store, "resolve_ref", _never_commits_lookup)

    staging = tmp_path / "staging"
    receipts = tmp_path / "receipts"
    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "latest", staging_root=staging,
            installed_index=_index(tmp_path), receipts_dir=receipts,
        )
    assert exc_info.value.kind == "no_release_found"
    assert calls == []
    # Strict: the refusal runs BEFORE the staging root is even created — a
    # staging directory (empty or not) would mean resolution ran after mkdir.
    assert not staging.exists()
    assert not receipts.exists()


@pytest.mark.parametrize("raised, kind", [
    (plugin_store.RefNotFound("org/repo: repository not visible (HTTP 404)"), "ref_not_found"),
    (plugin_store.ResolveAuthFailed("401"), "resolve_auth_failed"),
    (plugin_store.SourceEmpty("org/repo: empty repository (HTTP 409)"), "source_empty"),
    (plugin_store.ResolveUnavailable("503"), "resolve_unavailable"),
])
def test_release_lookup_failures_keep_the_resolve_taxonomy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raised: Exception, kind: str,
) -> None:
    root = _write_component(tmp_path / "component", slug="fresh-specialist")
    stub, calls = _recording_fetch(root)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", stub)

    def _raise(repo: str, **_k):
        raise raised

    monkeypatch.setattr(plugin_store, "resolve_latest_release", _raise)

    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "latest", staging_root=tmp_path / "staging",
            installed_index=_index(tmp_path), receipts_dir=tmp_path / "receipts",
        )
    assert exc_info.value.kind == kind
    assert calls == []


def test_an_expected_revision_that_is_not_the_peel_is_a_revision_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The producer's handed-off sha is checked against the release's peeled
    commit, exactly as plugin_add's guard runs against the tag's peel."""
    root = _write_component(tmp_path / "component", slug="fresh-specialist")
    stub, calls = _recording_fetch(root)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", stub)
    monkeypatch.setattr(plugin_store, "resolve_latest_release", lambda repo, **_k: (TAG, PEELED))

    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "latest", expected_revision="c" * 40,
            staging_root=tmp_path / "staging",
            installed_index=_index(tmp_path), receipts_dir=tmp_path / "receipts",
        )
    assert exc_info.value.kind == "revision_mismatch"
    assert calls == []


def test_a_commits_lookup_that_disagrees_with_the_peel_fetches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REAL resolve_and_fetch runs here. plugin_store.resolve_ref is the
    commits lookup — faked to answer with a sha that is NOT the release's
    peeled commit (what a same-named branch shadowing the tag would produce);
    plugin_store.fetch_commit_tree is faked only to record whether it was
    reached. The guard must refuse with revision_mismatch, fetch nothing and
    write no receipt — in install AND upgrade mode."""
    fetched: list[str] = []
    monkeypatch.setattr(plugin_store, "resolve_latest_release", lambda repo, **_k: (TAG, PEELED))
    monkeypatch.setattr(plugin_store, "resolve_ref", lambda repo, ref, **_k: "c" * 40)
    monkeypatch.setattr(
        plugin_store, "fetch_commit_tree",
        lambda repo, commit, subdir, dest, **_k: fetched.append(commit))

    staging = tmp_path / "staging"
    receipts = tmp_path / "receipts"
    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "latest", staging_root=staging,
            installed_index=_index(tmp_path), receipts_dir=receipts,
        )
    assert exc_info.value.kind == "revision_mismatch"
    assert fetched == []
    assert not receipts.exists()
    assert list(staging.iterdir()) == []      # the failed staging dir is reclaimed

    target_slug = "upgrade-target"
    specialists_dir = tmp_path / "specialists"
    target_dir = InstanceDir(specialists_dir / target_slug)
    target_dir.stage_desired(_specialist_tuple(_specialist_binding(target_slug)))
    target_dir.commit_desired_to_active()
    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()
    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "latest", mode="upgrade", target_slug=target_slug,
            staging_root=staging, installed_index=index,
            specialists_dir=specialists_dir, receipts_dir=receipts,
        )
    assert exc_info.value.kind == "revision_mismatch"
    assert fetched == []
    assert not receipts.exists()


def test_upgrade_mode_resolves_latest_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_slug = "upgrade-target"
    specialists_dir = tmp_path / "specialists"
    target_dir = InstanceDir(specialists_dir / target_slug)
    target_dir.stage_desired(_specialist_tuple(_specialist_binding(target_slug)))
    target_dir.commit_desired_to_active()
    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()

    root = _write_component(tmp_path / "component", slug=target_slug)
    stub, calls = _recording_fetch(root)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", stub)
    monkeypatch.setattr(plugin_store, "resolve_latest_release", lambda repo, **_k: (TAG, PEELED))
    monkeypatch.setattr(plugin_store, "resolve_ref", _never_commits_lookup)

    result = specialist_install.inspect_specialist_repo(
        "org/repo", "latest", mode="upgrade", target_slug=target_slug,
        staging_root=tmp_path / "staging", installed_index=index,
        specialists_dir=specialists_dir, receipts_dir=tmp_path / "receipts",
    )
    assert result.slug == target_slug
    assert result.resolved_ref == TAG
    assert calls == [("org/repo", TAG, PEELED)]
