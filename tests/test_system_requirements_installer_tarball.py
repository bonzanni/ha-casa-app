"""Tarball install strategy for casa.systemRequirements (§4.3.1).

Includes Bug 2 / Bug 3 (v0.14.6) regression suite:
- Symlinked tar member is rejected with UnsafeArchiveError.
- Path-traversal tar member (../../etc/foo) is rejected.
- Zip member with absolute path / `..` is rejected.
- Zip member encoded as a symlink (external_attr 0xA000) is rejected.
- Bad URL scheme (file://, ftp://) is refused before download.
- `extract` field with `..` is refused.
- `install_cmd` as a string raises (argv-list-only).
"""
from __future__ import annotations

import hashlib
import http.server
import io
import os
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

from system_requirements.tarball import (
    IntegrityError, UnsafeArchiveError, install_tarball,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def fixture_tarball(tmp_path: Path) -> tuple[Path, str]:
    """Build a small fixture tarball containing bin/fakebin (a shell stub)."""
    pkg_dir = tmp_path / "pkg"
    (pkg_dir / "bin").mkdir(parents=True)
    (pkg_dir / "bin" / "fakebin").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    (pkg_dir / "bin" / "fakebin").chmod(0o755)

    tar_path = tmp_path / "fakebin.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(pkg_dir, arcname=".")
    sha = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    return tar_path, sha


@pytest.fixture
def http_server(fixture_tarball, tmp_path: Path):
    tar_path, sha = fixture_tarball

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:  # type: ignore[override]
            return str(tar_path)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/pkg.tar.gz", sha
    server.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink requires developer mode on Windows")
def test_install_happy(tmp_path: Path, http_server) -> None:
    url, sha = http_server
    result = install_tarball(
        plugin_name="fakebin",
        spec={
            "type": "tarball",
            "url": url,
            "sha256": sha,
            "extract": ".",
            "verify_bin": "fakebin",
        },
        tools_root=tmp_path / "tools",
    )
    assert result.ok
    assert result.verify_bin_resolves
    assert (tmp_path / "tools" / "bin" / "fakebin").is_symlink()


def test_integrity_mismatch(tmp_path: Path, http_server) -> None:
    url, _sha = http_server
    with pytest.raises(IntegrityError):
        install_tarball(
            plugin_name="fakebin",
            spec={
                "type": "tarball",
                "url": url,
                "sha256": "0" * 64,
                "extract": ".",
                "verify_bin": "fakebin",
            },
            tools_root=tmp_path / "tools",
        )
    # Clean rollback: no published bin, no generation content.
    assert list((tmp_path / "tools" / "bin").iterdir()) == []
    assert list((tmp_path / "tools" / "tarball" / "fakebin").iterdir()) == []


# ---------------------------------------------------------------------------
# Bug 2 / Bug 3 (v0.14.6) regression suite
# ---------------------------------------------------------------------------


def _serve_local_file(path: Path):
    """Serve `path` over a throwaway HTTP server. Returns (url, sha)."""
    sha = hashlib.sha256(path.read_bytes()).hexdigest()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, p: str) -> str:  # type: ignore[override]
            return str(path)

        def log_message(self, *args, **kwargs) -> None:  # silence
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/x", sha, server


@pytest.mark.skipif(sys.platform == "win32",
                    reason="symlink in tarball requires posix")
def test_symlink_member_rejected(tmp_path: Path) -> None:
    """A tarball with a symlink pointing at /etc must be refused.

    Pre-fix: tarfile.extractall extracted the symlink as written;
    later steps could traverse it to host files.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "real.txt").write_text("ok", encoding="utf-8")
    os.symlink("/etc", pkg / "escape")

    tar_path = tmp_path / "bad.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(pkg, arcname=".")
    url, sha, server = _serve_local_file(tar_path)
    try:
        # Accept either our explicit "symlink" wording (raised by the
        # member-iteration guard at tarball.py:89) OR Python 3.11+'s
        # tarfile.AbsoluteLinkError "link to an absolute path" wording
        # (raised by the data filter at tarball.py:83 before we ever see
        # the member). Both signal correct refusal of the unsafe entry.
        with pytest.raises(UnsafeArchiveError,
                           match=r"symlink|link to an absolute path"):
            install_tarball(
                plugin_name="evil",
                spec={"type": "tarball", "url": url, "sha256": sha,
                      "extract": ".", "verify_bin": "real.txt"},
                tools_root=tmp_path / "tools",
            )
    finally:
        server.shutdown()


def test_path_traversal_member_rejected(tmp_path: Path) -> None:
    """A tar member named ../escape.txt is refused before extraction."""
    tar_path = tmp_path / "bad.tar.gz"
    payload = io.BytesIO(b"pwned")
    with tarfile.open(tar_path, "w:gz") as tf:
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(payload.getvalue())
        payload.seek(0)
        tf.addfile(info, payload)

    url, sha, server = _serve_local_file(tar_path)
    try:
        with pytest.raises(UnsafeArchiveError):
            install_tarball(
                plugin_name="evil",
                spec={"type": "tarball", "url": url, "sha256": sha,
                      "extract": ".", "verify_bin": "x"},
                tools_root=tmp_path / "tools",
            )
        # And nothing escaped to the parent of tools_root.
        assert not (tmp_path / "escape.txt").exists()
    finally:
        server.shutdown()


def test_zip_path_traversal_rejected(tmp_path: Path) -> None:
    """A zip with a member resolving above the extract dir is refused."""
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../escape.txt", b"pwned")
    url, sha, server = _serve_local_file(zip_path)
    try:
        with pytest.raises(UnsafeArchiveError):
            install_tarball(
                plugin_name="evil",
                spec={"type": "tarball", "url": url, "sha256": sha,
                      "extract": ".", "verify_bin": "x"},
                tools_root=tmp_path / "tools",
            )
        assert not (tmp_path / "escape.txt").exists()
    finally:
        server.shutdown()


def test_zip_symlink_member_rejected(tmp_path: Path) -> None:
    """Zip member with external_attr indicating a symlink is refused."""
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        info = zipfile.ZipInfo("escape")
        # 0xA000 = symlink mode; <<16 places it in the high word of external_attr.
        info.external_attr = (0xA1FF << 16) | 0
        zf.writestr(info, b"/etc")
    url, sha, server = _serve_local_file(zip_path)
    try:
        with pytest.raises(UnsafeArchiveError, match="symlink"):
            install_tarball(
                plugin_name="evil",
                spec={"type": "tarball", "url": url, "sha256": sha,
                      "extract": ".", "verify_bin": "x"},
                tools_root=tmp_path / "tools",
            )
    finally:
        server.shutdown()


def test_extract_path_traversal_refused(tmp_path: Path, fixture_tarball) -> None:
    """spec.extract='../..' must be refused even with a clean tarball."""
    tar_path, sha = fixture_tarball
    url, _sha2, server = _serve_local_file(tar_path)
    try:
        with pytest.raises(UnsafeArchiveError, match="extract path"):
            install_tarball(
                plugin_name="fakebin",
                spec={"type": "tarball", "url": url, "sha256": sha,
                      "extract": "../..", "verify_bin": "fakebin"},
                tools_root=tmp_path / "tools",
            )
    finally:
        server.shutdown()


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://anonymous@example.com/x.tgz",
    "jar:http://x/y.jar!/inside",
])
def test_unsafe_url_schemes_refused(tmp_path: Path, url: str) -> None:
    with pytest.raises(UnsafeArchiveError, match="scheme"):
        install_tarball(
            plugin_name="x",
            spec={"type": "tarball", "url": url, "sha256": "0" * 64,
                  "extract": ".", "verify_bin": "x"},
            tools_root=tmp_path / "tools",
        )


def test_install_cmd_string_refused(tmp_path: Path, fixture_tarball) -> None:
    """install_cmd as a shell string is no longer accepted.

    Pre-v0.14.6: subprocess.run(install_cmd, shell=True) — full RCE on
    the host as root for any marketplace author. The fix accepts only
    a list[str] (argv).
    """
    tar_path, sha = fixture_tarball
    url, _sha, server = _serve_local_file(tar_path)
    try:
        with pytest.raises(UnsafeArchiveError, match="install_cmd"):
            install_tarball(
                plugin_name="fakebin",
                spec={
                    "type": "tarball", "url": url, "sha256": sha,
                    "extract": ".", "verify_bin": "fakebin",
                    "install_cmd": "echo 'this used to be shell-eval'",
                },
                tools_root=tmp_path / "tools",
            )
    finally:
        server.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell needed for echo argv")
def test_install_cmd_argv_list_runs(tmp_path: Path, fixture_tarball) -> None:
    """install_cmd as argv list runs without shell=True."""
    tar_path, sha = fixture_tarball
    url, _sha, server = _serve_local_file(tar_path)
    try:
        result = install_tarball(
            plugin_name="fakebin",
            spec={
                "type": "tarball", "url": url, "sha256": sha,
                "extract": ".", "verify_bin": "fakebin",
                "install_cmd": ["true"],   # benign argv
            },
            tools_root=tmp_path / "tools",
        )
        assert result.ok
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# H13 — the download must be bounded by a socket timeout (was urlretrieve with
# the global default timeout of None → an unresponsive server hung the whole
# casa-main event loop forever).
# ---------------------------------------------------------------------------

def _install_spec(url: str, sha: str, **extra) -> dict:
    spec = {"type": "tarball", "url": url, "sha256": sha,
            "extract": ".", "verify_bin": "fakebin"}
    spec.update(extra)
    return spec


def _build_tarball(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    """Build a tarball at tmp_path/name containing the given path→text files."""
    pkg_dir = tmp_path / f"{name}-pkg"
    for rel, text in files.items():
        dest = pkg_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        dest.chmod(0o755)
    tar_path = tmp_path / name
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(pkg_dir, arcname=".")
    return tar_path


def test_failed_reinstall_preserves_existing_install(tmp_path: Path) -> None:
    """#308: a reinstall whose install_cmd fails must leave the previously
    working install (tree + resolving symlink) untouched.

    Pre-fix: rmtree(install_dir) ran before install_cmd, so the nonzero
    exit destroyed the working tree and left a half-installed replacement."""
    import subprocess

    v1_tar = _build_tarball(tmp_path, "v1.tar.gz",
                            {"bin/fakebin": "#!/bin/sh\necho v1\n"})
    v2_tar = _build_tarball(tmp_path, "v2.tar.gz",
                            {"other/fakebin": "#!/bin/sh\necho v2\n"})
    url1, sha1, server1 = _serve_local_file(v1_tar)
    url2, sha2, server2 = _serve_local_file(v2_tar)
    tools = tmp_path / "tools"
    try:
        result = install_tarball(
            plugin_name="fakebin", spec=_install_spec(url1, sha1),
            tools_root=tools,
        )
        assert result.verify_bin_resolves
        marker = result.install_dir / "bin" / "fakebin"

        with pytest.raises(subprocess.CalledProcessError):
            install_tarball(
                plugin_name="fakebin",
                spec=_install_spec(url2, sha2, install_cmd=["false"]),
                tools_root=tools,
            )
        # The old tree survives with its original content, and the
        # published symlink still resolves to a real file.
        assert marker.read_text(encoding="utf-8") == "#!/bin/sh\necho v1\n"
        link = tools / "bin" / "fakebin"
        assert link.is_symlink() and link.resolve().is_file()
    finally:
        server1.shutdown()
        server2.shutdown()


def test_invalid_install_cmd_shape_checked_before_touching_install(
        tmp_path: Path) -> None:
    """#308: the argv-shape validation of install_cmd must run before the
    existing install is disturbed."""
    v1_tar = _build_tarball(tmp_path, "v1.tar.gz",
                            {"bin/fakebin": "#!/bin/sh\necho v1\n"})
    v2_tar = _build_tarball(tmp_path, "v2.tar.gz",
                            {"bin/fakebin": "#!/bin/sh\necho v2\n"})
    url1, sha1, server1 = _serve_local_file(v1_tar)
    url2, sha2, server2 = _serve_local_file(v2_tar)
    tools = tmp_path / "tools"
    try:
        result = install_tarball(
            plugin_name="fakebin", spec=_install_spec(url1, sha1),
            tools_root=tools,
        )
        marker = result.install_dir / "bin" / "fakebin"
        with pytest.raises(UnsafeArchiveError, match="install_cmd"):
            install_tarball(
                plugin_name="fakebin",
                spec=_install_spec(url2, sha2, install_cmd="rm -rf /"),
                tools_root=tools,
            )
        assert marker.read_text(encoding="utf-8") == "#!/bin/sh\necho v1\n"
        link = tools / "bin" / "fakebin"
        assert link.is_symlink() and link.resolve().is_file()
    finally:
        server1.shutdown()
        server2.shutdown()


def test_reinstall_keeps_previous_generation_until_next_install(
        tmp_path: Path, fixture_tarball) -> None:
    """#308 (review round 2): a successful reinstall publishes a fresh
    generation and atomically re-points the launcher into it, but RETAINS the
    previous generation (grace for in-flight consumers of the old tree).
    The generation before that — no longer serving at the next install's
    start — is reclaimed then, so at most two generations ever accumulate.
    Staging leftovers never survive an install."""
    tar_path, sha = fixture_tarball
    url, _sha, server = _serve_local_file(tar_path)
    tools = tmp_path / "tools"
    gens_dir = tools / "tarball" / "fakebin"
    try:
        first = install_tarball(
            plugin_name="fakebin", spec=_install_spec(url, sha),
            tools_root=tools,
        )
        second = install_tarball(
            plugin_name="fakebin", spec=_install_spec(url, sha),
            tools_root=tools,
        )
        assert second.ok and second.verify_bin_resolves
        assert second.install_dir != first.install_dir
        link = tools / "bin" / "fakebin"
        # The link resolves INTO the new generation…
        assert str(link.resolve()).startswith(str(second.install_dir))
        # …while the previous generation is retained for in-flight consumers.
        assert first.install_dir.is_dir()

        third = install_tarball(
            plugin_name="fakebin", spec=_install_spec(url, sha),
            tools_root=tools,
        )
        assert third.ok
        # The first generation stopped serving before the third install
        # began, so its start-of-install reclaim removed it.
        assert not first.install_dir.exists()
        assert second.install_dir.is_dir()   # still the grace generation
        gens = sorted(p.name for p in gens_dir.iterdir())
        assert len(gens) == 2, f"expected exactly two generations: {gens}"
        assert not any(n.startswith(".") for n in gens), f"staging leaked: {gens}"
    finally:
        server.shutdown()


def test_verify_bin_rename_with_failed_install_keeps_old_launcher_serving(
        tmp_path: Path, fixture_tarball, monkeypatch) -> None:
    """#308 (review round 4, Terra P1-1): during a verify_bin RENAME the
    incoming name has no launcher yet — serving-detection must fall back to
    the plugin's manifest-recorded bin, or the start-of-install sweep deletes
    the tree the OLD launcher still serves and a failed install leaves it
    dangling."""
    from system_requirements.manifest import add_plugin_entry

    manifest_path = tmp_path / "system-requirements.yaml"
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH",
                        manifest_path)
    tar_path, sha = fixture_tarball
    url, _sha, server = _serve_local_file(tar_path)
    tools = tmp_path / "tools"
    try:
        first = install_tarball(
            plugin_name="fakebin", spec=_install_spec(url, sha),
            tools_root=tools,
        )
        assert first.verify_bin_resolves
        add_plugin_entry({"name": "fakebin", "winning_strategy": "tarball",
                          "install_dir": str(first.install_dir),
                          "verify_bin": "fakebin",
                          "declared_at": "2026-08-01T00:00:00Z"})
        old_link = tools / "bin" / "fakebin"
        target_before = old_link.resolve()

        # Renamed launcher; the archive does not contain "newtool", so the
        # install fails after the sweep already ran.
        result = install_tarball(
            plugin_name="fakebin",
            spec=_install_spec(url, sha, verify_bin="newtool"),
            tools_root=tools,
        )
        assert result.verify_bin_resolves is False
        # The old launcher AND the tree it serves survived the failed rename.
        assert first.install_dir.is_dir()
        assert old_link.resolve() == target_before
        assert old_link.resolve().is_file()
    finally:
        server.shutdown()


def test_rename_with_corrupt_manifest_still_keeps_serving_tree(
        tmp_path: Path, fixture_tarball, monkeypatch) -> None:
    """#308 (Sol r5-1): serving-detection must not depend on the manifest —
    with a CORRUPT manifest (reads as empty) and a verify_bin rename, the
    tree the old launcher still serves must survive the sweep, because any
    live tools/bin link marks its target generation as serving."""
    manifest_path = tmp_path / "system-requirements.yaml"
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH",
                        manifest_path)
    tar_path, sha = fixture_tarball
    url, _sha, server = _serve_local_file(tar_path)
    tools = tmp_path / "tools"
    try:
        first = install_tarball(
            plugin_name="fakebin", spec=_install_spec(url, sha),
            tools_root=tools,
        )
        assert first.verify_bin_resolves
        manifest_path.write_text("{ corrupt", encoding="utf-8")
        old_link = tools / "bin" / "fakebin"
        target_before = old_link.resolve()

        result = install_tarball(
            plugin_name="fakebin",
            spec=_install_spec(url, sha, verify_bin="newtool"),
            tools_root=tools,
        )
        assert result.verify_bin_resolves is False   # newtool not in archive
        assert first.install_dir.is_dir()
        assert old_link.resolve() == target_before
        assert old_link.resolve().is_file()
    finally:
        server.shutdown()


def test_missing_verify_bin_is_refused_without_touching_anything(
        tmp_path: Path, fixture_tarball) -> None:
    """#308 (review round 2, Terra P1-1): a tarball spec with no verify_bin
    can never succeed — it must be refused up front, never run the install
    and never disturb the prior working generation or its launcher."""
    tar_path, sha = fixture_tarball
    url, _sha, server = _serve_local_file(tar_path)
    tools = tmp_path / "tools"
    try:
        first = install_tarball(
            plugin_name="fakebin", spec=_install_spec(url, sha),
            tools_root=tools,
        )
        link = tools / "bin" / "fakebin"
        target_before = link.resolve()

        bad_spec = _install_spec(url, sha)
        del bad_spec["verify_bin"]
        result = install_tarball(
            plugin_name="fakebin", spec=bad_spec, tools_root=tools,
        )
        assert result.ok is False
        assert result.verify_bin_resolves is False
        assert first.install_dir.is_dir()
        assert link.resolve() == target_before
    finally:
        server.shutdown()


def test_old_generation_serves_until_publication(tmp_path: Path) -> None:
    """#308 (review round 2): the previous generation is never moved or
    deleted before the launcher symlink is retargeted — a failed replacement
    leaves both the old tree AND its resolving link exactly as they were
    (no rename-aside window, no restore step)."""
    import subprocess

    v1_tar = _build_tarball(tmp_path, "v1.tar.gz",
                            {"bin/fakebin": "#!/bin/sh\necho v1\n"})
    v2_tar = _build_tarball(tmp_path, "v2.tar.gz",
                            {"bin/fakebin": "#!/bin/sh\necho v2\n"})
    url1, sha1, server1 = _serve_local_file(v1_tar)
    url2, sha2, server2 = _serve_local_file(v2_tar)
    tools = tmp_path / "tools"
    try:
        first = install_tarball(
            plugin_name="fakebin", spec=_install_spec(url1, sha1),
            tools_root=tools,
        )
        link = tools / "bin" / "fakebin"
        target_before = link.resolve()
        assert str(target_before).startswith(str(first.install_dir))

        with pytest.raises(subprocess.CalledProcessError):
            install_tarball(
                plugin_name="fakebin",
                spec=_install_spec(url2, sha2, install_cmd=["false"]),
                tools_root=tools,
            )
        # Old generation untouched at its ORIGINAL path, link unmoved.
        assert first.install_dir.is_dir()
        assert link.resolve() == target_before
        assert link.resolve().read_text(encoding="utf-8") == "#!/bin/sh\necho v1\n"
    finally:
        server1.shutdown()
        server2.shutdown()


def test_cross_plugin_bin_claim_refused(tmp_path: Path, monkeypatch) -> None:
    """#354: plugin B declaring plugin A's verify_bin must be refused before
    anything is downloaded or overwritten — pre-fix B silently repointed
    A's tools/bin symlink at B's tree while both reported ready."""
    from system_requirements.manifest import BinOwnershipError, add_plugin_entry

    manifest_path = tmp_path / "system-requirements.yaml"
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH",
                        manifest_path)
    v1_tar = _build_tarball(tmp_path, "v1.tar.gz",
                            {"bin/fakebin": "#!/bin/sh\necho a\n"})
    url, sha, server = _serve_local_file(v1_tar)
    tools = tmp_path / "tools"
    try:
        result = install_tarball(
            plugin_name="plugin-a", spec=_install_spec(url, sha),
            tools_root=tools,
        )
        assert result.verify_bin_resolves
        add_plugin_entry({"name": "plugin-a", "winning_strategy": "tarball",
                          "install_dir": str(result.install_dir),
                          "verify_bin": "fakebin",
                          "declared_at": "2026-08-01T00:00:00Z"})
        link = tools / "bin" / "fakebin"
        target_before = link.resolve()

        with pytest.raises(BinOwnershipError):
            install_tarball(
                plugin_name="plugin-b", spec=_install_spec(url, sha),
                tools_root=tools,
            )
        assert link.resolve() == target_before
        assert not (tools / "tarball" / "plugin-b").exists()
    finally:
        server.shutdown()


def test_download_times_out_on_stalled_server(tmp_path: Path) -> None:
    """A server that accepts the TCP connection but never sends a response
    must not hang install_tarball. Pre-fix (urlretrieve) used the global
    default socket timeout (None) and this test hung forever."""
    import socket
    import time
    import urllib.error

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _accept_and_stall() -> None:
        conn, _ = srv.accept()
        try:
            conn.recv(65536)   # consume the HTTP request, then go silent
            time.sleep(30)     # stall far past the 1s timeout below
        except OSError:
            pass
        finally:
            conn.close()

    threading.Thread(target=_accept_and_stall, daemon=True).start()
    start = time.monotonic()
    with pytest.raises((TimeoutError, OSError, urllib.error.URLError)):
        install_tarball(
            plugin_name="stall",
            spec={"type": "tarball",
                  "url": f"http://127.0.0.1:{port}/x.tgz",
                  "sha256": "0" * 64, "extract": ".", "verify_bin": "x"},
            tools_root=tmp_path / "tools",
            timeout=1,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 10, f"download ignored timeout (took {elapsed:.1f}s)"
    srv.close()
