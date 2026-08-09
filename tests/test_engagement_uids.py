import json, os, pytest
from engagement_uids import UidAllocator, UidStateError, UID_BASE, UNALLOCATED_UID

# S1 r2: reconstruct now folds LIVE /proc uids into the high-water. The
# exact-value tests below inject an EMPTY live-uid scanner so they do not depend
# on the real /proc contents of the test host (a process with uid >= UID_BASE
# would otherwise perturb the asserted allocation). Tests that exercise the
# live-uid path inject their own scanner.
_NO_LIVE = lambda: set()


def test_allocate_is_monotonic_and_never_reused(tmp_path):
    a = UidAllocator(str(tmp_path / "uids.json"), proc_scanner=_NO_LIVE)
    a.reconstruct(known_uids=[], dir_owner_uids=[])
    first = a.allocate(); second = a.allocate()
    assert first == UID_BASE and second == UID_BASE + 1


def test_reconstruct_takes_max_over_all_sources_not_liveness(tmp_path):
    a = UidAllocator(str(tmp_path / "uids.json"), proc_scanner=_NO_LIVE)
    a.reconstruct(known_uids=[UID_BASE + 5], dir_owner_uids=[UID_BASE + 9])
    assert a.allocate() == UID_BASE + 10   # never below any seen uid


def test_corrupt_counter_file_refuses(tmp_path):
    p = tmp_path / "uids.json"; p.write_text("{not json")
    a = UidAllocator(str(p), proc_scanner=_NO_LIVE)
    with pytest.raises(UidStateError):
        a.reconstruct(known_uids=[], dir_owner_uids=[])


def test_counter_missing_with_live_proc_uid_does_not_reset_below(tmp_path):
    # S1 r2 (both reviewers): a LOST counter with ZERO filesystem/passwd
    # evidence must STILL never reissue a uid held by a LIVE process — a
    # setsid/double-fork descendant that escaped the supervised group survives
    # teardown (which prunes record + workspace + passwd) yet lingers in /proc.
    a = UidAllocator(
        str(tmp_path / "uids.json"),   # counter MISSING
        passwd_path=str(tmp_path / "passwd"),   # absent → no passwd evidence
        group_path=str(tmp_path / "group"),
        proc_scanner=lambda: {UID_BASE},   # a live survivor holds 200000
    )
    a.reconstruct(known_uids=[], dir_owner_uids=[])   # no fs/passwd evidence
    assert a.allocate() == UID_BASE + 1   # strictly above the live survivor


def test_counter_missing_zero_evidence_and_clean_proc_starts_at_base(tmp_path):
    # Genuine fresh install: no counter, no records/dirs/passwd, and the /proc
    # scan finds no casa uids → allocation starts at UID_BASE (not refused).
    a = UidAllocator(
        str(tmp_path / "uids.json"),
        passwd_path=str(tmp_path / "passwd"),
        group_path=str(tmp_path / "group"),
        proc_scanner=_NO_LIVE,
    )
    a.reconstruct(known_uids=[], dir_owner_uids=[])
    assert a.allocate() == UID_BASE


def test_proc_scan_failure_refuses_fail_closed(tmp_path):
    # S1 r2: if the /proc live-uid scan cannot run at all, reconstruct must
    # REFUSE (UidStateError), never proceed blind to which uids are live —
    # same fail-closed posture as a malformed counter.
    def _boom():
        raise OSError("/proc unavailable")
    a = UidAllocator(str(tmp_path / "uids.json"), proc_scanner=_boom)
    with pytest.raises(UidStateError):
        a.reconstruct(known_uids=[], dir_owner_uids=[])


def test_scan_proc_uids_reads_real_uid_ge_base(tmp_path):
    from engagement_uids import scan_proc_uids
    proc = tmp_path / "proc"
    # A high-uid process (in Casa's engagement range) and a low-uid one.
    (proc / "111").mkdir(parents=True)
    (proc / "111" / "status").write_text(
        "Name:\tclaude\nUid:\t200007\t200007\t200007\t200007\n")
    (proc / "222").mkdir()
    (proc / "222" / "status").write_text(
        "Name:\tsh\nUid:\t1000\t1000\t1000\t1000\n")
    (proc / "not-a-pid").mkdir()   # non-numeric entry ignored
    assert scan_proc_uids(str(proc)) == {200007}
    # A missing proc root raises (fail-closed signal to reconstruct).
    with pytest.raises(OSError):
        scan_proc_uids(str(tmp_path / "nope"))


def test_counter_missing_with_passwd_entry_does_not_reset_below(tmp_path):
    # S1 code-gate fix (design §2): a LOST counter file must never reissue a
    # uid still evidenced by a ``casa-eng-<uid>`` passwd entry — even when NO
    # record and NO workspace dir preserve the high-water (a detached survivor
    # whose record+workspace were pruned but whose passwd entry lingers).
    pw = tmp_path / "passwd"
    pw.write_text(
        "root:x:0:0::/root:/bin/sh\n"
        f"casa-eng-{UID_BASE}:x:{UID_BASE}:{UID_BASE}::"
        "/data/engagements/x/.home:/usr/sbin/nologin\n")
    gr = tmp_path / "group"; gr.write_text("")
    a = UidAllocator(
        str(tmp_path / "uids.json"),   # counter MISSING
        passwd_path=str(pw), group_path=str(gr), proc_scanner=_NO_LIVE)
    a.reconstruct(known_uids=[], dir_owner_uids=[])   # no record/workspace
    # Next uid is strictly ABOVE the still-evidenced 200000, never a reissue.
    assert a.allocate() == UID_BASE + 1


def test_counter_missing_zero_evidence_starts_at_base(tmp_path):
    # Genuine fresh install: no counter, no records, no dirs, no casa-eng
    # passwd entries, clean /proc → allocation starts at UID_BASE (not refused).
    pw = tmp_path / "passwd"; pw.write_text("root:x:0:0::/root:/bin/sh\n")
    gr = tmp_path / "group"; gr.write_text("")
    a = UidAllocator(
        str(tmp_path / "uids.json"),
        passwd_path=str(pw), group_path=str(gr), proc_scanner=_NO_LIVE)
    a.reconstruct(known_uids=[], dir_owner_uids=[])
    assert a.allocate() == UID_BASE


def test_scan_passwd_uids_reads_uid_field(tmp_path):
    from engagement_uids import scan_passwd_uids
    pw = tmp_path / "passwd"
    pw.write_text(
        "root:x:0:0::/root:/bin/sh\n"
        "casa-eng-200003:x:200003:200003::/h:/usr/sbin/nologin\n"
        "someuser:x:1000:1000::/home/x:/bin/sh\n"
        "casa-eng-bogus:x:notanint:0::/h:/usr/sbin/nologin\n")
    assert sorted(scan_passwd_uids(str(pw))) == [200003]
    # Missing file contributes nothing (never raises).
    assert scan_passwd_uids(str(tmp_path / "nope")) == []


def test_allocate_before_reconstruct_refuses(tmp_path):
    a = UidAllocator(str(tmp_path / "uids.json"))
    with pytest.raises(UidStateError):
        a.allocate()


def test_persist_survives_reload(tmp_path):
    p = str(tmp_path / "uids.json")
    a = UidAllocator(p, proc_scanner=_NO_LIVE)
    a.reconstruct([], []); a.allocate(); a.allocate()
    b = UidAllocator(p, proc_scanner=_NO_LIVE); b.reconstruct([], [])
    assert b.allocate() == UID_BASE + 2   # continues, never reuses


def test_ensure_and_prune_identity(tmp_path):
    pw = tmp_path / "passwd"; pw.write_text("root:x:0:0:root:/root:/bin/bash\n")
    gr = tmp_path / "group"; gr.write_text("root:x:0:\n")
    a = UidAllocator(str(tmp_path/"uids.json"), passwd_path=str(pw), group_path=str(gr))
    a.ensure_identity(UID_BASE, "/data/engagements/x/.home")
    assert f"casa-eng-{UID_BASE}:x:{UID_BASE}:{UID_BASE}::" in pw.read_text()
    a.ensure_identity(UID_BASE, "/data/engagements/x/.home")  # idempotent
    assert pw.read_text().count(f"casa-eng-{UID_BASE}") == 1
    a.prune_identity(UID_BASE)
    assert f"casa-eng-{UID_BASE}" not in pw.read_text()


def test_module_level_ensure_and_prune_identity(tmp_path):
    """Task 8 (containment stage 2): ensure_identity/prune_identity are now
    module-level functions provisioning can call without an allocator
    instance in hand — UidAllocator's own methods delegate to these."""
    from engagement_uids import ensure_identity, prune_identity

    pw = tmp_path / "passwd"; pw.write_text("root:x:0:0:root:/root:/bin/bash\n")
    gr = tmp_path / "group"; gr.write_text("root:x:0:\n")
    ensure_identity(
        UID_BASE, "/data/engagements/x/.home",
        passwd_path=str(pw), group_path=str(gr),
    )
    assert f"casa-eng-{UID_BASE}:x:{UID_BASE}:{UID_BASE}::" in pw.read_text()
    ensure_identity(  # idempotent
        UID_BASE, "/data/engagements/x/.home",
        passwd_path=str(pw), group_path=str(gr),
    )
    assert pw.read_text().count(f"casa-eng-{UID_BASE}") == 1
    prune_identity(UID_BASE, passwd_path=str(pw), group_path=str(gr))
    assert f"casa-eng-{UID_BASE}" not in pw.read_text()


def test_module_level_defaults_match_etc_passwd(tmp_path):
    """The module-level functions' default paths are /etc/passwd and
    /etc/group — same defaults UidAllocator's constructor used before the
    refactor."""
    import inspect

    from engagement_uids import ensure_identity, prune_identity

    ei_defaults = inspect.signature(ensure_identity).parameters
    assert ei_defaults["passwd_path"].default == "/etc/passwd"
    assert ei_defaults["group_path"].default == "/etc/group"
    pi_defaults = inspect.signature(prune_identity).parameters
    assert pi_defaults["passwd_path"].default == "/etc/passwd"
    assert pi_defaults["group_path"].default == "/etc/group"


def test_allocator_ensure_identity_delegates_to_module_function(tmp_path, monkeypatch):
    """UidAllocator.ensure_identity/prune_identity must delegate to the
    module-level functions bound to the allocator's own passwd/group
    paths — not reimplement the logic a second time."""
    import engagement_uids as eu_mod

    calls = []
    monkeypatch.setattr(
        eu_mod, "ensure_identity",
        lambda uid, home, *, passwd_path, group_path: calls.append(
            ("ensure", uid, home, passwd_path, group_path)),
    )
    monkeypatch.setattr(
        eu_mod, "prune_identity",
        lambda uid, *, passwd_path, group_path: calls.append(
            ("prune", uid, passwd_path, group_path)),
    )
    a = UidAllocator(str(tmp_path / "uids.json"),
                      passwd_path="/x/passwd", group_path="/x/group")
    a.ensure_identity(UID_BASE, "/home/x")
    a.prune_identity(UID_BASE)
    assert calls == [
        ("ensure", UID_BASE, "/home/x", "/x/passwd", "/x/group"),
        ("prune", UID_BASE, "/x/passwd", "/x/group"),
    ]


def test_ensure_identity_does_not_corrupt_missing_trailing_newline(tmp_path):
    pw = tmp_path / "passwd"
    pw.write_text("root:x:0:0:root:/root:/bin/bash")  # no trailing newline
    gr = tmp_path / "group"
    gr.write_text("root:x:0:")  # no trailing newline
    a = UidAllocator(str(tmp_path / "uids.json"), passwd_path=str(pw), group_path=str(gr))
    a.ensure_identity(UID_BASE, "/data/engagements/x/.home")

    pw_lines = pw.read_text().split("\n")
    assert "root:x:0:0:root:/root:/bin/bash" in pw_lines
    assert f"casa-eng-{UID_BASE}:x:{UID_BASE}:{UID_BASE}::/data/engagements/x/.home:/usr/sbin/nologin" in pw_lines

    gr_lines = gr.read_text().split("\n")
    assert "root:x:0:" in gr_lines
    assert f"casa-eng-{UID_BASE}:x:{UID_BASE}:" in gr_lines
