import os
import stat

import pytest

from safe_fs import (
    HAS_OPENAT2,
    SymlinkRefused,
    atomic_write_beneath,
    list_dir_beneath,
    read_text_beneath,
)


def test_reads_regular_file(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    assert read_text_beneath(str(tmp_path), "a.txt") == "hi"


def test_refuses_symlink_final_component(tmp_path):
    outside = tmp_path.parent / "secret"
    outside.write_text("token")
    (tmp_path / "a.txt").symlink_to(outside)
    with pytest.raises(SymlinkRefused):
        read_text_beneath(str(tmp_path), "a.txt")


def test_refuses_symlink_intermediate_component(tmp_path):
    outside = tmp_path.parent / "sib"
    outside.mkdir()
    (outside / "f").write_text("x")
    (tmp_path / "d").symlink_to(outside)
    with pytest.raises(SymlinkRefused):
        read_text_beneath(str(tmp_path), "d/f")


def test_refuses_escape_via_dotdot(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    with pytest.raises((SymlinkRefused, OSError)):
        read_text_beneath(str(sub), "../a.txt")


def test_owner_uid_mismatch_refused(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    # owner check: current euid owns it; assert a wrong uid is rejected
    with pytest.raises((SymlinkRefused, PermissionError)):
        read_text_beneath(str(tmp_path), "a.txt", owner_uid=999999)


def test_refuses_root_itself_a_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.txt").write_text("x")
    link_root = tmp_path / "link_root"
    link_root.symlink_to(real)
    with pytest.raises(SymlinkRefused):
        read_text_beneath(str(link_root), "a.txt")


def test_list_dir_beneath(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    names = sorted(list_dir_beneath(str(tmp_path)))
    assert names == ["a.txt", "b.txt"]


class TestForcedFallback:
    """Re-run the symlink-refusal cases with HAS_OPENAT2 forced False, so the
    FD-relative fallback path is exercised even on a kernel that has openat2."""

    def test_reads_regular_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("safe_fs.HAS_OPENAT2", False)
        (tmp_path / "a.txt").write_text("hi")
        assert read_text_beneath(str(tmp_path), "a.txt") == "hi"

    def test_refuses_symlink_final_component(self, tmp_path, monkeypatch):
        monkeypatch.setattr("safe_fs.HAS_OPENAT2", False)
        outside = tmp_path.parent / "secret2"
        outside.write_text("token")
        (tmp_path / "a.txt").symlink_to(outside)
        with pytest.raises(SymlinkRefused):
            read_text_beneath(str(tmp_path), "a.txt")

    def test_refuses_symlink_intermediate_component(self, tmp_path, monkeypatch):
        monkeypatch.setattr("safe_fs.HAS_OPENAT2", False)
        outside = tmp_path.parent / "sib2"
        outside.mkdir()
        (outside / "f").write_text("x")
        (tmp_path / "d").symlink_to(outside)
        with pytest.raises(SymlinkRefused):
            read_text_beneath(str(tmp_path), "d/f")

    def test_refuses_escape_via_dotdot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("safe_fs.HAS_OPENAT2", False)
        sub = tmp_path / "sub"
        sub.mkdir()
        with pytest.raises((SymlinkRefused, OSError)):
            read_text_beneath(str(sub), "../a.txt")

    def test_owner_uid_mismatch_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr("safe_fs.HAS_OPENAT2", False)
        (tmp_path / "a.txt").write_text("x")
        with pytest.raises((SymlinkRefused, PermissionError)):
            read_text_beneath(str(tmp_path), "a.txt", owner_uid=999999)

    def test_refuses_root_itself_a_symlink(self, tmp_path, monkeypatch):
        monkeypatch.setattr("safe_fs.HAS_OPENAT2", False)
        real = tmp_path / "real2"
        real.mkdir()
        (real / "a.txt").write_text("x")
        link_root = tmp_path / "link_root2"
        link_root.symlink_to(real)
        with pytest.raises(SymlinkRefused):
            read_text_beneath(str(link_root), "a.txt")

    def test_list_dir_beneath(self, tmp_path, monkeypatch):
        monkeypatch.setattr("safe_fs.HAS_OPENAT2", False)
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "b.txt").write_text("y")
        names = sorted(list_dir_beneath(str(tmp_path)))
        assert names == ["a.txt", "b.txt"]


def test_refuses_absolute_rel_path_real(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(SymlinkRefused):
        read_text_beneath(str(tmp_path), "/etc/passwd")


def test_refuses_absolute_rel_path_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("safe_fs.HAS_OPENAT2", False)
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(SymlinkRefused):
        read_text_beneath(str(tmp_path), "/etc/passwd")


class TestAtomicWriteBeneath:
    """S1 code-gate fix: the symlink-safe atomic writer (write-side sibling of
    open_beneath), exercised on both the openat2 and forced-fallback paths."""

    @pytest.mark.parametrize("has_openat2", [True, False])
    def test_writes_regular_file_with_exact_mode(self, tmp_path, monkeypatch, has_openat2):
        monkeypatch.setattr("safe_fs.HAS_OPENAT2", has_openat2)
        atomic_write_beneath(str(tmp_path), "a.txt", "hello", mode=0o600)
        p = tmp_path / "a.txt"
        assert p.read_text() == "hello"
        assert stat.S_IMODE(p.stat().st_mode) == 0o600

    @pytest.mark.parametrize("has_openat2", [True, False])
    def test_writes_into_subdir_and_replaces_existing(self, tmp_path, monkeypatch, has_openat2):
        monkeypatch.setattr("safe_fs.HAS_OPENAT2", has_openat2)
        (tmp_path / "d").mkdir()
        (tmp_path / "d" / "f").write_text("old")
        atomic_write_beneath(str(tmp_path), "d/f", b"new-bytes")
        assert (tmp_path / "d" / "f").read_text() == "new-bytes"

    @pytest.mark.parametrize("has_openat2", [True, False])
    def test_refuses_symlinked_target_leaves_sibling_unchanged(self, tmp_path, monkeypatch, has_openat2):
        monkeypatch.setattr("safe_fs.HAS_OPENAT2", has_openat2)
        outside = tmp_path.parent / f"victim-{tmp_path.name}"
        outside.write_text("SECRET")
        (tmp_path / "t.txt").symlink_to(outside)
        with pytest.raises(SymlinkRefused):
            atomic_write_beneath(str(tmp_path), "t.txt", "attacker")
        # The link is untouched and the sibling was NOT written through.
        assert (tmp_path / "t.txt").is_symlink()
        assert outside.read_text() == "SECRET"

    @pytest.mark.parametrize("has_openat2", [True, False])
    def test_refuses_symlinked_parent_dir(self, tmp_path, monkeypatch, has_openat2):
        monkeypatch.setattr("safe_fs.HAS_OPENAT2", has_openat2)
        outside = tmp_path.parent / f"sibdir-{tmp_path.name}"
        outside.mkdir()
        (outside / "f").write_text("SECRET")
        (tmp_path / "d").symlink_to(outside)   # parent component is a symlink
        with pytest.raises(SymlinkRefused):
            atomic_write_beneath(str(tmp_path), "d/f", "attacker")
        assert (outside / "f").read_text() == "SECRET"

    @pytest.mark.parametrize("has_openat2", [True, False])
    def test_owner_mismatch_refused(self, tmp_path, monkeypatch, has_openat2):
        monkeypatch.setattr("safe_fs.HAS_OPENAT2", has_openat2)
        with pytest.raises((SymlinkRefused, PermissionError)):
            atomic_write_beneath(str(tmp_path), "a.txt", "x", owner_uid=999999)
        assert not (tmp_path / "a.txt").exists()

    @pytest.mark.parametrize("has_openat2", [True, False])
    def test_no_fd_leak_on_repeated_refusal(self, tmp_path, monkeypatch, has_openat2):
        monkeypatch.setattr("safe_fs.HAS_OPENAT2", has_openat2)
        outside = tmp_path.parent / f"victim2-{tmp_path.name}"
        outside.write_text("SECRET")
        (tmp_path / "t.txt").symlink_to(outside)

        def count_open_fds():
            return len(os.listdir(f"/proc/{os.getpid()}/fd"))

        baseline = count_open_fds()
        for _ in range(50):
            with pytest.raises(SymlinkRefused):
                atomic_write_beneath(str(tmp_path), "t.txt", "x")
        assert count_open_fds() <= baseline + 2


def test_intermediate_owner_mismatch_refused_fallback(tmp_path, monkeypatch):
    """Distinct from test_owner_uid_mismatch_refused: that test mismatches on
    the LEAF file. This exercises the fallback's PER-COMPONENT owner check on
    a legitimate (non-symlink) intermediate directory. We can't actually chown
    to a different uid without root, so we fake the first os.fstat() call
    (which the walk makes on the intermediate dir "d") to report a foreign
    owner, while leaving the real fstat behavior for everything else —
    confirming the mismatch is caught before "f" is ever reached."""
    monkeypatch.setattr("safe_fs.HAS_OPENAT2", False)
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_text("x")

    real_fstat = os.fstat
    calls = {"n": 0}

    class _FakeStat:
        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def __getattr__(self, name):
            return getattr(object.__getattribute__(self, "_real"), name)

    def fake_fstat(fd):
        calls["n"] += 1
        real = real_fstat(fd)
        if calls["n"] == 1:
            fake = _FakeStat(real)
            object.__setattr__(fake, "st_uid", real.st_uid + 1)
            return fake
        return real

    monkeypatch.setattr("safe_fs.os.fstat", fake_fstat)
    with pytest.raises(SymlinkRefused):
        read_text_beneath(str(tmp_path), "d/f", owner_uid=os.getuid())
    assert calls["n"] == 1, "walk should refuse at the intermediate dir, never reaching the leaf"


def test_no_fd_leak_on_symlink_refusal_fallback(tmp_path, monkeypatch):
    """Regression: the fallback must close the parent fd on every raise path,
    including the intermediate-symlink case, or fds leak on repeated refusals."""
    monkeypatch.setattr("safe_fs.HAS_OPENAT2", False)
    outside = tmp_path.parent / "sib3"
    outside.mkdir()
    (outside / "f").write_text("x")
    (tmp_path / "d").symlink_to(outside)

    def count_open_fds():
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))

    baseline = count_open_fds()
    for _ in range(50):
        with pytest.raises(SymlinkRefused):
            read_text_beneath(str(tmp_path), "d/f")
    assert count_open_fds() <= baseline + 2
