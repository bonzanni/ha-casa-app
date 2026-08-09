"""plugin_outbox — FD-based, TOCTOU-safe claim/capture/sweep (v0.73.0, spec §3.4)."""
from __future__ import annotations

import errno
import os
import socket
import stat
from pathlib import Path

import pytest

import plugin_outbox
from plugin_outbox import MAX_AGE_S, OutboxError, PluginOutbox

pytestmark = pytest.mark.unit

PDF = b"%PDF-1.7\n" + b"x" * 200
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 100


@pytest.fixture
def outbox(tmp_path):
    root = tmp_path / "plugin-outbox"
    ob = PluginOutbox(str(root))
    yield ob
    ob.close()


def _write_outbox_file(outbox_root: str, name: str, data: bytes) -> str:
    p = os.path.join(outbox_root, name)
    with open(p, "wb") as fh:
        fh.write(data)
    return p


def _claim_of(outbox, name, data):
    src = _write_outbox_file(outbox._root_realpath, name, data)
    return outbox.claim(src)


# ---------------------------------------------------------------------------
# init + claim + remove_claim
# ---------------------------------------------------------------------------


def test_init_creates_dirs_0770_and_fds(tmp_path):
    root = tmp_path / "ob"
    ob = PluginOutbox(str(root))
    try:
        assert (root).is_dir()
        assert (root / ".claims").is_dir()
        assert stat.S_IMODE(os.stat(root).st_mode) == 0o770
        assert stat.S_IMODE(os.stat(root / ".claims").st_mode) == 0o770
        assert isinstance(ob._outbox_dirfd, int) and isinstance(ob._claims_dirfd, int)
    finally:
        ob.close()


def test_claim_moves_file_into_claims_and_returns_name(outbox):
    src = _write_outbox_file(outbox._root_realpath, "invoice-2026-07-abc.pdf", PDF)
    name = outbox.claim(src)
    assert "-" in name                              # <epoch_ms>-<uuid4hex>
    assert not os.path.exists(src)                  # original path is gone (claimed)
    assert os.path.exists(os.path.join(outbox._claims_realpath, name))


def test_claim_outside_outbox_refused(outbox, tmp_path):
    other = tmp_path / "secret"
    other.write_bytes(b"nope")
    with pytest.raises(OutboxError) as ei:
        outbox.claim(str(other))
    assert ei.value.kind == "outside_outbox"


def test_claim_parent_traversal_refused(outbox):
    bad = os.path.join(outbox._root_realpath, "..", "secret.pdf")
    with pytest.raises(OutboxError) as ei:
        outbox.claim(bad)
    assert ei.value.kind == "outside_outbox"


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b.pdf", "a\x00b.pdf", "a\nb.pdf"])
def test_claim_bad_basename_refused(outbox, bad):
    p = outbox._root_realpath + "/" + bad
    with pytest.raises(OutboxError) as ei:
        outbox.claim(p)
    assert ei.value.kind in ("bad_name", "outside_outbox")


def test_claim_missing_source(outbox):
    p = os.path.join(outbox._root_realpath, "gone.pdf")
    with pytest.raises(OutboxError) as ei:
        outbox.claim(p)
    assert ei.value.kind == "missing"


def test_claim_bare_basename_refused(outbox):
    # A bare basename (empty dirname) is refused deterministically (not CWD-dependent).
    with pytest.raises(OutboxError) as ei:
        outbox.claim("bare.pdf")
    assert ei.value.kind == "outside_outbox"


def test_claim_non_enoent_rename_is_guard_error(outbox, monkeypatch):
    src = _write_outbox_file(outbox._root_realpath, "x.pdf", PDF)

    def fake_rename(*a, **k):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(plugin_outbox.os, "rename", fake_rename)
    with pytest.raises(OutboxError) as ei:
        outbox.claim(src)
    assert ei.value.kind == "guard_error"


def test_claim_race_one_winner(outbox):
    src = _write_outbox_file(outbox._root_realpath, "race.pdf", PDF)
    name1 = outbox.claim(src)
    with pytest.raises(OutboxError) as ei:
        outbox.claim(src)                            # loser: source already renamed away
    assert ei.value.kind == "missing"
    assert os.path.exists(os.path.join(outbox._claims_realpath, name1))


def test_claim_concurrent_threads_one_winner(outbox):
    import threading
    src = _write_outbox_file(outbox._root_realpath, "conc.pdf", PDF)
    results: list = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()                                # maximise contention
        try:
            results.append(("ok", outbox.claim(src)))
        except OutboxError as e:
            results.append(("err", e.kind))

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    oks = [r for r in results if r[0] == "ok"]
    errs = [r for r in results if r[0] == "err"]
    assert len(oks) == 1 and len(errs) == 1 and errs[0][1] == "missing"
    outbox.remove_claim(oks[0][1])


def test_remove_claim_file(outbox):
    src = _write_outbox_file(outbox._root_realpath, "x.pdf", PDF)
    name = outbox.claim(src)
    outbox.remove_claim(name)
    assert not os.path.exists(os.path.join(outbox._claims_realpath, name))


def test_remove_claim_directory(outbox):
    d = os.path.join(outbox._root_realpath, "weird.pdf")
    os.mkdir(d)
    with open(os.path.join(d, "inner"), "wb") as fh:
        fh.write(b"z")
    name = outbox.claim(d)
    outbox.remove_claim(name)
    assert not os.path.exists(os.path.join(outbox._claims_realpath, name))


def test_init_outbox_singleton(tmp_path):
    root = tmp_path / "singleton-ob"
    ob = plugin_outbox.init_outbox(str(root))
    try:
        assert plugin_outbox.get_outbox() is ob
    finally:
        ob.close()
        plugin_outbox._OUTBOX = None


def test_closed_outbox_fails_closed_not_cwd(tmp_path, monkeypatch):
    # Regression (Sol diff review): after close(), the dir-FDs are None; a
    # dir_fd=None op resolves relative to the process CWD (fail-OPEN). Operations
    # on a closed outbox MUST fail CLOSED and touch NOTHING — never grab a
    # same-named CWD file.
    ob = PluginOutbox(str(tmp_path / "ob"))
    src = _write_outbox_file(ob._root_realpath, "invoice.pdf", PDF)
    ob.close()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "invoice.pdf").write_bytes(b"CWD-FILE")
    monkeypatch.chdir(cwd)
    with pytest.raises(OutboxError) as ei:
        ob.claim(src)
    assert ei.value.kind == "guard_error"
    assert (cwd / "invoice.pdf").read_bytes() == b"CWD-FILE"   # CWD file untouched
    assert os.path.exists(src)                                  # outbox file untouched
    with pytest.raises(OutboxError):
        ob.capture("anything", "document")                     # capture fail-closed too
    assert ob.sweep_once(10_000_000_000_000) == 0              # sweep no-op when closed


def test_close_serializes_with_inflight_op_no_cwd_grab(tmp_path, monkeypatch):
    # Regression (Sol diff review r2): the _closed flag is a CHECK, not
    # synchronization — without the lock, close() could null the FDs between an
    # op's guard and its syscall (dir_fd=None -> CWD grab). The lock serializes
    # close() against the FD syscalls: an in-flight claim runs on VALID fds while
    # close() waits, so it targets the outbox, never a same-named CWD file.
    import threading
    ob = PluginOutbox(str(tmp_path / "ob"))
    src = _write_outbox_file(ob._root_realpath, "invoice.pdf", PDF)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "invoice.pdf").write_bytes(b"CWD-FILE")
    monkeypatch.chdir(cwd)

    in_rename = threading.Event()
    release = threading.Event()
    real_rename = os.rename

    def slow_rename(*a, **k):
        in_rename.set()
        release.wait(5)                 # hold the lock mid-rename (runs INSIDE it)
        return real_rename(*a, **k)

    monkeypatch.setattr(plugin_outbox.os, "rename", slow_rename)
    result: dict = {}

    def worker():
        try:
            result["claim"] = ob.claim(src)
        except OutboxError as e:            # pragma: no cover - defensive
            result["err"] = e.kind

    t = threading.Thread(target=worker)
    t.start()
    assert in_rename.wait(5)             # worker is inside the lock, mid-rename
    closed = threading.Event()
    ct = threading.Thread(target=lambda: (ob.close(), closed.set()))
    ct.start()                            # close() blocks on the lock the worker holds
    release.set()                         # let the rename finish on VALID fds
    t.join(5)
    ct.join(5)

    assert "claim" in result                                   # ran on valid fds
    assert (cwd / "invoice.pdf").read_bytes() == b"CWD-FILE"   # CWD untouched
    assert not os.path.exists(src)                             # outbox file claimed
    assert closed.is_set()                                     # close() completed


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


def test_capture_returns_bytes_for_valid_pdf(outbox):
    name = _claim_of(outbox, "ok.pdf", PDF)
    got = outbox.capture(name, "document")
    assert got == PDF


def test_capture_magic_mismatch_pdf_as_photo(outbox):
    name = _claim_of(outbox, "ok.pdf", PDF)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "photo")
    assert ei.value.kind == "magic_mismatch"


def test_capture_photo_ok(outbox):
    name = _claim_of(outbox, "p.jpg", JPEG)
    assert outbox.capture(name, "photo") == JPEG


def test_capture_symlink_refused_via_nofollow(outbox, tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"%PDF-secret")
    link = os.path.join(outbox._root_realpath, "link.pdf")
    os.symlink(str(secret), link)                    # symlink lives in the outbox
    name = outbox.claim(link)                          # rename moves the symlink itself
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "not_regular"
    outbox.remove_claim(name)


def test_capture_fifo_refused(outbox):
    p = os.path.join(outbox._root_realpath, "pipe.pdf")
    os.mkfifo(p)
    name = outbox.claim(p)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "not_regular"
    outbox.remove_claim(name)


def test_capture_hardlink_refused(outbox, tmp_path):
    target = tmp_path / "outside.pdf"
    target.write_bytes(PDF)
    link = os.path.join(outbox._root_realpath, "hard.pdf")
    os.link(str(target), link)                        # nlink == 2
    name = outbox.claim(link)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "multi_link"
    outbox.remove_claim(name)


def test_capture_oversize_refused(outbox):
    big = b"%PDF-" + b"x" * (10 * 1024 * 1024 + 5)     # > 10 MB photo cap
    name = _claim_of(outbox, "big.jpg", b"\xff\xd8\xff" + big)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "photo")
    assert ei.value.kind == "too_large"
    outbox.remove_claim(name)


def test_capture_directory_typed_claim_is_not_regular(outbox):
    d = os.path.join(outbox._root_realpath, "dir.pdf")
    os.mkdir(d)
    name = outbox.claim(d)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "not_regular"
    outbox.remove_claim(name)                          # must rmtree cleanly
    assert not os.path.exists(os.path.join(outbox._claims_realpath, name))


def test_capture_empty_file_magic_mismatch(outbox):
    name = _claim_of(outbox, "empty.pdf", b"")
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")               # accepts(b"") is False, no crash
    assert ei.value.kind == "magic_mismatch"
    outbox.remove_claim(name)


def test_capture_jpeg_as_document_magic_mismatch(outbox):
    name = _claim_of(outbox, "j.jpg", JPEG)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "magic_mismatch"
    outbox.remove_claim(name)


def test_capture_shrink_is_guard_error(outbox, monkeypatch):
    name = _claim_of(outbox, "s.pdf", PDF)
    # fstat sees the real size, but the read returns fewer bytes -> integrity fault.
    monkeypatch.setattr(plugin_outbox, "_read_capped", lambda fd, cap: b"%PDF")
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "guard_error"
    outbox.remove_claim(name)


def test_capture_socket_is_not_regular(outbox):
    # A UNIX socket: lstat gate -> not_regular. (open() would ENXIO, not ELOOP —
    # errno-matching on the open alone would mis-map it to guard_error.)
    sp = os.path.join(outbox._root_realpath, "sock.pdf")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(sp)
    try:
        name = outbox.claim(sp)
        with pytest.raises(OutboxError) as ei:
            outbox.capture(name, "document")
        assert ei.value.kind == "not_regular"
        outbox.remove_claim(name)
    finally:
        srv.close()


def test_capture_one_byte_magic_mismatch(outbox):
    name = _claim_of(outbox, "one.pdf", b"x")
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "magic_mismatch"
    outbox.remove_claim(name)


def test_capture_non_eloop_open_failure_is_guard_error(outbox, monkeypatch):
    name = _claim_of(outbox, "g.pdf", PDF)
    real_open = os.open

    def fake_open(path, *a, **k):
        if path == name:                    # only the claim open fails; delegate the rest
            raise OSError(errno.EACCES, "denied")
        return real_open(path, *a, **k)

    monkeypatch.setattr(plugin_outbox.os, "open", fake_open)
    with pytest.raises(OutboxError) as ei:
        outbox.capture(name, "document")
    assert ei.value.kind == "guard_error"   # EACCES (not ELOOP) -> guard_error
    monkeypatch.undo()
    outbox.remove_claim(name)


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def _set_mtime(path, epoch_s):
    os.utime(path, (epoch_s, epoch_s))


def test_sweep_reaps_old_outbox_files_keeps_fresh(outbox):
    now_ms = 10_000_000_000_000
    old = _write_outbox_file(outbox._root_realpath, "old.pdf", PDF)
    fresh = _write_outbox_file(outbox._root_realpath, "fresh.pdf", PDF)
    _set_mtime(old, now_ms / 1000 - (MAX_AGE_S + 60))      # 2h+ old
    _set_mtime(fresh, now_ms / 1000 - 60)                   # 1 min old
    n = outbox.sweep_once(now_ms)
    assert n == 1
    assert not os.path.exists(old)
    assert os.path.exists(fresh)


def test_sweep_excludes_claims_dir_itself(outbox):
    now_ms = 10_000_000_000_000
    _set_mtime(outbox._claims_realpath, now_ms / 1000 - (MAX_AGE_S * 100))
    outbox.sweep_once(now_ms)
    assert os.path.isdir(outbox._claims_realpath)


def test_sweep_reaps_old_claims_by_embedded_epoch(outbox):
    now_ms = 10_000_000_000_000
    old_epoch = now_ms - (MAX_AGE_S + 120) * 1000
    fresh_epoch = now_ms - 30_000
    old_name = f"{old_epoch}-{'a' * 32}"
    fresh_name = f"{fresh_epoch}-{'b' * 32}"
    for nm in (old_name, fresh_name):
        with open(os.path.join(outbox._claims_realpath, nm), "wb") as fh:
            fh.write(b"z")
    # Give the old claim a RECENT mtime — rename preserves source mtime, so age
    # must come from the embedded epoch, NOT mtime.
    _set_mtime(os.path.join(outbox._claims_realpath, old_name), now_ms / 1000 - 10)
    n = outbox.sweep_once(now_ms)
    assert not os.path.exists(os.path.join(outbox._claims_realpath, old_name))
    assert os.path.exists(os.path.join(outbox._claims_realpath, fresh_name))
    assert n == 1


def test_sweep_reaps_directory_typed_stale_claim(outbox):
    now_ms = 10_000_000_000_000
    old_epoch = now_ms - (MAX_AGE_S + 120) * 1000
    d = os.path.join(outbox._claims_realpath, f"{old_epoch}-{'c' * 32}")
    os.mkdir(d)
    with open(os.path.join(d, "inner"), "wb") as fh:
        fh.write(b"z")
    outbox.sweep_once(now_ms)
    assert not os.path.exists(d)


async def test_sweep_job_reaps_and_runs_off_loop(tmp_path, monkeypatch):
    """The casa_core sweep coroutine reaps orphans AND runs the reap off the
    event loop (worker thread) — asserted by a differing thread ident."""
    import threading
    ob = plugin_outbox.init_outbox(str(tmp_path / "job-ob"))
    try:
        old = _write_outbox_file(ob._root_realpath, "old.pdf", PDF)
        _set_mtime(old, os.stat(old).st_mtime - (MAX_AGE_S + 60))
        main_ident = threading.get_ident()
        seen: dict = {}
        real_sweep = ob.sweep_once

        def spy(now_ms):
            seen["ident"] = threading.get_ident()
            return real_sweep(now_ms)

        monkeypatch.setattr(ob, "sweep_once", spy)     # sweep_now -> sweep_once
        n = await plugin_outbox.sweep_job()
        assert n == 1
        assert not os.path.exists(old)
        assert seen["ident"] != main_ident             # ran off the event loop
    finally:
        ob.close()
        plugin_outbox._OUTBOX = None


async def test_sweep_job_noop_when_uninitialised(monkeypatch):
    monkeypatch.setattr(plugin_outbox, "_OUTBOX", None)
    assert await plugin_outbox.sweep_job() == 0


async def test_wire_inits_and_registers_hourly_job(tmp_path):
    """Executable wiring coverage: wire() inits the outbox AND registers the
    hourly job on a fake scheduler — catches a misregistered trigger/id that
    byte-compiling casa_core cannot."""
    jobs: list = []

    class _FakeScheduler:
        def add_job(self, func, **kw):
            jobs.append((func, kw))

    await plugin_outbox.wire(_FakeScheduler(), str(tmp_path / "wired-ob"))
    try:
        assert plugin_outbox.get_outbox() is not None       # init happened
        assert len(jobs) == 1
        func, kw = jobs[0]
        assert func is plugin_outbox.sweep_job
        assert kw["id"] == "plugin_outbox_sweep"
        assert kw["trigger"] == "interval" and kw["hours"] == 1
        assert kw["max_instances"] == 1
    finally:
        plugin_outbox.get_outbox().close()
        plugin_outbox._OUTBOX = None


# ---------------------------------------------------------------------------
# #330 — sweep TOCTOU: expiry decided on one inode must not delete another
# ---------------------------------------------------------------------------


def test_sweep_does_not_reap_fresh_file_renamed_over_expired_name(outbox):
    """#330: producers publish via atomic rename OUTSIDE the outbox lock — a
    fresh file renamed over a reused name between the sweep's lstat and its
    unlink was deleted, and the producer's returned path went missing. The
    reap must confirm the name still names the inode it judged expired."""
    old = _write_outbox_file(outbox._root_realpath, "reused.pdf", PDF)
    past = (plugin_outbox._now_ms() - (MAX_AGE_S + 60) * 1000) / 1000
    os.utime(old, (past, past))
    st_old = os.lstat(old)

    # Simulate the producer racing the sweep: a FRESH inode appears under
    # the same name after the sweep captured st_old.
    tmp = _write_outbox_file(outbox._root_realpath, ".fresh.tmp", JPEG)
    os.rename(tmp, old)

    reaped = outbox._reap(outbox._outbox_dirfd, "reused.pdf", st_old)
    assert reaped == 0
    assert os.path.exists(old), "fresh producer file was deleted by the sweep"


def _strand_reap_entry(outbox, dname: str, ename: str, data: bytes) -> str:
    """Simulate a crash mid-reap: a held entry inside a per-reap ownership
    dir (`.reap/<origin>.<pid>.<uuid>/<original-name>`)."""
    pdir = os.path.join(outbox._reap_realpath, dname)
    os.mkdir(pdir, 0o700)
    p = os.path.join(pdir, ename)
    with open(p, "wb") as fh:
        fh.write(data)
    return p


def test_reap_restore_yields_to_newer_same_name_publication(outbox):
    """Terra r2 (#330): restoring a fresh inode after the ownership rename
    must NOT replace an even newer publication that took the name in the
    meantime — the no-replace restore keeps the newest file and drops the
    superseded held copy (same outcome as producer-overwrites-producer)."""
    held = _strand_reap_entry(outbox, "root.1.aaaa", "reused.pdf", PDF)
    newest = _write_outbox_file(outbox._root_realpath, "reused.pdf", JPEG)

    outbox.sweep_once(plugin_outbox._now_ms())

    with open(newest, "rb") as fh:
        assert fh.read() == JPEG          # newest publication untouched
    assert not os.path.exists(held)


def test_reap_restore_puts_fresh_file_back_when_name_free(outbox):
    """The ordinary restore: the name is free again, the held fresh inode
    goes back under its published name."""
    held = _strand_reap_entry(outbox, "root.1.bbbb", "back.pdf", PDF)

    outbox.sweep_once(plugin_outbox._now_ms())

    with open(os.path.join(outbox._root_realpath, "back.pdf"), "rb") as fh:
        assert fh.read() == PDF
    assert not os.path.exists(held)


def test_stranded_reap_entry_restored_on_next_sweep(outbox):
    """Terra r5 (#330): a crash between the ownership rename and the restore
    leaves a fresh publication stranded inside its per-reap dir. The sweep
    must RESTORE stranded entries — never age-reap a fresh one."""
    held = _strand_reap_entry(outbox, "root.123.deadbeef", "fresh.pdf", PDF)

    outbox.sweep_once(plugin_outbox._now_ms())

    restored = os.path.join(outbox._root_realpath, "fresh.pdf")
    assert os.path.exists(restored), "stranded fresh publication not restored"
    with open(restored, "rb") as fh:
        assert fh.read() == PDF
    assert not os.path.exists(held)
    assert not os.path.exists(os.path.dirname(held))   # ownership dir gone


def test_stranded_expired_reap_entry_restored_then_reaped(outbox):
    """A stranded EXPIRED entry is restored to its original name and then
    reaped by the normal expiry pass — never lost, never leaked."""
    held = _strand_reap_entry(outbox, "root.123.cafebabe", "old.pdf", PDF)
    past = (plugin_outbox._now_ms() - (MAX_AGE_S + 60) * 1000) / 1000
    os.utime(held, (past, past))

    outbox.sweep_once(plugin_outbox._now_ms())

    assert not os.path.exists(held)
    assert not os.path.exists(os.path.join(outbox._root_realpath, "old.pdf"))


def test_stranded_reap_entry_superseded_by_newer_publication(outbox):
    """If the original name was re-published while the entry was stranded,
    the newer publication wins and the stranded copy is dropped."""
    held = _strand_reap_entry(outbox, "root.123.feedface", "reused.pdf", PDF)
    newest = _write_outbox_file(outbox._root_realpath, "reused.pdf", JPEG)

    outbox.sweep_once(plugin_outbox._now_ms())

    with open(newest, "rb") as fh:
        assert fh.read() == JPEG
    assert not os.path.exists(held)


def test_long_name_orphan_is_still_collectable(outbox):
    """Sol r6 (#330): a legal near-NAME_MAX producer name must still be
    reapable — the bounded per-reap ownership dir keeps the entry's own
    name unchanged, so no ENAMETOOLONG."""
    long_name = "x" * 250 + ".pdf"
    p = _write_outbox_file(outbox._root_realpath, long_name, PDF)
    past = (plugin_outbox._now_ms() - (MAX_AGE_S + 60) * 1000) / 1000
    os.utime(p, (past, past))

    reaped = outbox.sweep_once(plugin_outbox._now_ms())

    assert reaped == 1
    assert not os.path.exists(p)


def test_producer_file_named_like_reap_prefix_is_left_alone(outbox):
    """Terra r6 (#330): `_safe_basename` allows dotfiles, so a producer may
    legally publish a root file named `.reap.<x>.<y>.<z>` — the sweep must
    never hijack it as a crash-stranded ownership entry (stranded entries
    live in the sweep-owned `.reap/` SUBDIRECTORY, not the root)."""
    p = _write_outbox_file(outbox._root_realpath, ".reap.a.b.evil.pdf", PDF)

    outbox.sweep_once(plugin_outbox._now_ms())

    assert os.path.exists(p)          # fresh producer file untouched
    assert not os.path.exists(
        os.path.join(outbox._root_realpath, "evil.pdf"))


def test_init_displaces_preexisting_producer_file_named_reap(tmp_path):
    """Sol r7 (#330): `.reap` was a LEGAL producer basename before the
    sweep-owned subdir existed — an upgrade over such a file must not brick
    outbox init (makedirs FileExistsError → send_media disabled, no
    self-heal). The occupant is displaced to a fresh producer-visible name,
    content preserved."""
    root = tmp_path / "ob"
    root.mkdir()
    (root / ".reap").write_bytes(PDF)

    ob = PluginOutbox(str(root))
    try:
        assert (root / ".reap").is_dir()
        displaced = [p for p in root.iterdir()
                     if p.name.startswith("reap-displaced-")]
        assert len(displaced) == 1
        assert displaced[0].read_bytes() == PDF
    finally:
        ob.close()


# ---------------------------------------------------------------------------
# Per-engagement private outbox (containment stage 2, Task 11).
# ---------------------------------------------------------------------------


def test_provision_engagement_outbox_owned_by_uid(tmp_path):
    root = tmp_path / "eng-outbox"
    uid = os.getuid()  # unit tests run unprivileged — chown to our own uid
    d = plugin_outbox.provision_engagement_outbox(uid, root=str(root))
    st = os.stat(d)
    assert st.st_uid == uid
    assert stat.S_IMODE(st.st_mode) == 0o700
    # the shared parent must stay root's (here: the test uid's) but grant
    # o+x so a DIFFERENT uid can still traverse THROUGH it to its own dir —
    # never o+w, never o+r (no listing of sibling engagement dirs).
    parent_mode = stat.S_IMODE(os.stat(root).st_mode)
    assert parent_mode & 0o001
    assert parent_mode & 0o002 == 0
    assert parent_mode & 0o004 == 0


def test_provision_engagement_outbox_idempotent(tmp_path):
    root = tmp_path / "eng-outbox"
    uid = os.getuid()
    d1 = plugin_outbox.provision_engagement_outbox(uid, root=str(root))
    (Path(d1) / "leftover.txt").write_bytes(b"x")
    d2 = plugin_outbox.provision_engagement_outbox(uid, root=str(root))
    assert d1 == d2
    assert (Path(d2) / "leftover.txt").exists()  # re-provision never wipes


def test_provision_engagement_outbox_fresh_clears_leftover(tmp_path):
    # S1 r7: fresh=True (a newly-allocated uid) must never inherit a
    # predecessor's leftover files — an existing dir is rmtree'd + recreated.
    root = tmp_path / "eng-outbox"
    uid = os.getuid()
    d1 = plugin_outbox.provision_engagement_outbox(uid, root=str(root))
    (Path(d1) / "sibling-secret.pdf").write_bytes(b"A's media")
    d2 = plugin_outbox.provision_engagement_outbox(uid, root=str(root), fresh=True)
    assert d1 == d2
    assert not (Path(d2) / "sibling-secret.pdf").exists()  # cleared
    assert os.path.isdir(d2)                                # recreated fresh


def test_provision_engagement_outbox_fresh_replaces_stray_nondir(tmp_path):
    # fresh=True also clears a stray non-dir planted at the outbox path.
    root = tmp_path / "eng-outbox"
    uid = os.getuid()
    plugin_outbox.provision_engagement_outbox(uid, root=str(root))  # makes base
    d = plugin_outbox.engagement_outbox_dir(uid, root=str(root))
    import shutil as _sh
    _sh.rmtree(d)
    Path(d).write_bytes(b"not a dir")
    d2 = plugin_outbox.provision_engagement_outbox(uid, root=str(root), fresh=True)
    assert os.path.isdir(d2)


def test_get_engagement_outbox_caches_instance(tmp_path):
    root = tmp_path / "eng-outbox"
    uid = os.getuid()
    try:
        ob1 = plugin_outbox.get_engagement_outbox(uid, root=str(root))
        ob2 = plugin_outbox.get_engagement_outbox(uid, root=str(root))
        assert ob1 is ob2
    finally:
        plugin_outbox.teardown_engagement_outbox(uid, root=str(root))


def test_teardown_engagement_outbox_closes_and_removes(tmp_path):
    root = tmp_path / "eng-outbox"
    uid = os.getuid()
    ob = plugin_outbox.get_engagement_outbox(uid, root=str(root))
    d = ob._root_realpath
    plugin_outbox.teardown_engagement_outbox(uid, root=str(root))
    assert not os.path.exists(d)
    assert ob._closed is True
    # a fresh get() after teardown provisions a new instance, not the closed one
    ob2 = plugin_outbox.get_engagement_outbox(uid, root=str(root))
    try:
        assert ob2 is not ob
    finally:
        plugin_outbox.teardown_engagement_outbox(uid, root=str(root))


def test_get_engagement_outbox_root_is_uid_owned_with_private_group(
    tmp_path, monkeypatch,
):
    """Fix-loop round 1, finding 2: PluginOutbox.__init__ (shared with the
    non-private outbox) unconditionally re-chmods its root to 0770 on
    construction — the FIRST get_engagement_outbox() call for a uid ends
    with the dir at 0770, not the 0700 provision_engagement_outbox() sets
    in isolation. That is NOT a cross-engagement hole today only because
    every engagement's GID equals its own uid (a private, single-member
    group — design §2), so the widened group-rwx bits still only grant
    access back to the SAME uid.

    Runs against an ARBITRARY allocated uid (not this process's own) so the
    assertion holds regardless of the test runner's real uid/gid layout —
    real ownership is only asserted when actually running as root (the only
    case where the chown can truly land); everywhere else the recorded
    ``os.chown`` call args are asserted instead (same
    real-ownership-when-root / recorded-args-otherwise split Task 8 used for
    ``chown_workspace``). If a future change ever gave two engagements a
    SHARED gid, this test — not just the docstring — would catch the
    reopened cross-engagement exposure."""
    root = tmp_path / "eng-outbox"
    uid = 200099
    chown_calls: list[tuple] = []
    real_chown = os.chown

    def _recording_chown(path, u, g, **kw):
        chown_calls.append((path, u, g))
        if os.geteuid() == 0:
            real_chown(path, u, g, **kw)
    monkeypatch.setattr(os, "chown", _recording_chown)

    try:
        ob = plugin_outbox.get_engagement_outbox(uid, root=str(root))
        assert chown_calls, "provisioning must chown the outbox dir"
        _path, called_uid, called_gid = chown_calls[0]
        assert called_uid == uid
        assert called_gid == uid, (
            "engagement outbox root's group must be the engagement's OWN "
            "uid (private single-member group) — a shared group here would "
            "let another engagement using that group reach this outbox")
        if os.geteuid() == 0:
            st = os.stat(ob._root_realpath)
            assert st.st_uid == uid and st.st_gid == uid
        # Document the actual current mode (0770, not the isolated
        # provision_engagement_outbox()'s 0700) so a silent widening beyond
        # 0770 — e.g. to include "other" bits — is also caught here.
        assert stat.S_IMODE(os.stat(ob._root_realpath).st_mode) == 0o770
    finally:
        monkeypatch.setattr(os, "chown", real_chown)
        plugin_outbox.teardown_engagement_outbox(uid, root=str(root))
