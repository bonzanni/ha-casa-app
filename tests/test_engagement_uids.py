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


def test_reconstruct_evidence_raises_above_valid_durable(tmp_path):
    # S1 r6: evidence only ever RAISES above a valid durable copy (never lowers
    # it below the durable). Here a valid counter (200002) plus higher evidence
    # (200009) yields 200009.
    counter = tmp_path / "uids.json"
    counter.write_text('{"high_water": %d}' % (UID_BASE + 2))
    a = UidAllocator(str(counter), passwd_path=str(tmp_path / "pw"),
                     group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    a.reconstruct(known_uids=[UID_BASE + 9], dir_owner_uids=[])
    assert a.allocate() == UID_BASE + 10


def test_corrupt_counter_file_refuses(tmp_path):
    # A malformed counter with NO valid anchor: prior-existence (the file exists)
    # + no valid durable → POISON (never reset).
    p = tmp_path / "uids.json"; p.write_text("{not json")
    a = UidAllocator(str(p), passwd_path=str(tmp_path / "pw"),
                     group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    with pytest.raises(UidStateError):
        a.reconstruct(known_uids=[], dir_owner_uids=[])


def test_anchor_recovers_when_counter_absent_evidence_only_lower(tmp_path):
    # S1 r6 red-case #1: counter absent + anchor present (200002) + evidence
    # only [200000] → reconstruct = 200002 (from the anchor, NOT the lower
    # evidence); next allocate = 200003 (never reissues 200001/200002).
    anchor = tmp_path / "uids.json.initialized"
    anchor.write_text('{"high_water": %d}' % (UID_BASE + 2))
    a = UidAllocator(str(tmp_path / "uids.json"), passwd_path=str(tmp_path / "pw"),
                     group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    a.reconstruct(known_uids=[UID_BASE], dir_owner_uids=[])
    assert a.allocate() == UID_BASE + 3


def test_stale_low_counter_ignored_recovers_from_anchor(tmp_path):
    # S1 r6 red-case #4 (Terra S2): a stale-low counter {"high_water":0} is
    # INVALID; a valid anchor (200005) is used. reconstruct = 200005.
    counter = tmp_path / "uids.json"; counter.write_text('{"high_water": 0}')
    anchor = tmp_path / "uids.json.initialized"
    anchor.write_text('{"high_water": %d}' % (UID_BASE + 5))
    a = UidAllocator(str(counter), passwd_path=str(tmp_path / "pw"),
                     group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    a.reconstruct([], [])
    assert a.allocate() == UID_BASE + 6


def test_marker_present_both_copies_lost_refuses_reissue(tmp_path):
    # v0.170.1-r2 REISSUE regression (the reopened hole): Stage 2 initialised
    # (marker present), both durable copies then lost, and only real-uid evidence
    # [200000] survives → REFUSE. Evidence cannot prove the historic maximum, so
    # we must NOT init at base and reissue 200000. (v0.170.1-r1 wrongly recovered
    # to 200000 here; the marker now forces fail-closed.)
    counter = tmp_path / "uids.json"
    anchor = tmp_path / "uids.json.initialized"
    pw = tmp_path / "passwd"
    pw.write_text(
        f"casa-eng-{UID_BASE}:x:{UID_BASE}:{UID_BASE}::/h:/usr/sbin/nologin\n")
    a = UidAllocator(str(counter), passwd_path=str(pw),
                     group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    a.reconstruct([], [])                       # init → writes counter+anchor+marker
    a.allocate()
    os.remove(str(counter)); os.remove(str(anchor))   # both high-water copies lost
    b = UidAllocator(str(counter), passwd_path=str(pw),
                     group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    with pytest.raises(UidStateError):         # marker present + no copy → refuse
        b.reconstruct([], [])
    with pytest.raises(UidStateError):
        b.allocate()


def test_marker_present_both_copies_lost_no_evidence_refuses(tmp_path):
    # v0.170.1-r2: same, with NO evidence at all — marker present + both copies
    # gone → REFUSE (never silently reset to base).
    counter = tmp_path / "uids.json"
    anchor = tmp_path / "uids.json.initialized"
    a = UidAllocator(str(counter), passwd_path=str(tmp_path / "pw"),
                     group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    a.reconstruct([], [])
    os.remove(str(counter)); os.remove(str(anchor))
    b = UidAllocator(str(counter), passwd_path=str(tmp_path / "pw"),
                     group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    with pytest.raises(UidStateError):
        b.reconstruct([], [])


def test_gather_evidence_scans_the_outbox_root(tmp_path, monkeypatch):
    # S1 r7: the casa_core evidence gatherer MUST scan /data/plugin-outbox-eng —
    # a leftover per-uid outbox dir OWNER is folded (real-uid evidence). Reverting
    # the outbox root from the scan drops it — this is the folding red-case.
    import casa_core
    import plugin_outbox
    outbox_root = tmp_path / "plugin-outbox-eng"
    (outbox_root / "200000").mkdir(parents=True)   # A's leftover outbox dir
    monkeypatch.setattr(plugin_outbox, "ENGAGEMENT_OUTBOX_ROOT", str(outbox_root))

    class _Reg:
        def active_and_idle(self): return []
        def terminal_records(self): return []

    known, owners = casa_core._gather_reconstruct_evidence(
        _Reg(), data_dir=str(tmp_path / "no-engagements"))
    assert os.stat(str(outbox_root / "200000")).st_uid in owners


def test_first_stage2_upgrade_root_owned_dirs_initialises_not_refuses(tmp_path):
    # v0.170.1 REGRESSION (the N150 brick): first Stage-2 boot of a pre-Stage-2
    # install — engagement DIRS exist but are ROOT-owned (uid 0), records carry
    # UNALLOCATED_UID, no durable counter/anchor, NO marker, no casa-eng passwd,
    # no proc casa uid. Marker ABSENT → Stage 2 never initialised → reconstruct
    # INITIALISES at UID_BASE-1 (does NOT refuse), writes both copies + marker.
    # (v0.170.0 wrongly raised UidStateError here.)
    counter = tmp_path / "uids.json"
    anchor = tmp_path / "uids.json.initialized"
    marker = tmp_path / ".engagement-uids-initialized"
    a = UidAllocator(str(counter), passwd_path=str(tmp_path / "pw"),
                     group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    # dir_owner_uids = [0]: pre-Stage-2, root-owned workspace/ctl/outbox dirs
    # (uid 0 < UID_BASE → NOT real-uid evidence). records UNALLOCATED (not folded).
    a.reconstruct(known_uids=[], dir_owner_uids=[0, 0, 0])
    assert a.allocate() == UID_BASE             # migration can proceed
    assert counter.exists() and anchor.exists() and marker.exists()


def test_live_proc_evidence_with_marker_absent_initialises(tmp_path):
    # v0.170.1-r2: proc evidence but marker ABSENT — the marker is the fresh-vs-
    # loss authority, so this is treated as never-initialised → init at base.
    # (A proc uid without a marker means Stage 2 never wrote its marker; the
    # marker is written before any uid is ever handed out, so this is not a
    # reachable "allocated" state — init is correct and safe.)
    a = UidAllocator(str(tmp_path / "uids.json"), passwd_path=str(tmp_path / "pw"),
                     group_path=str(tmp_path / "gr"),
                     proc_scanner=lambda: {UID_BASE})
    a.reconstruct([], [])
    assert a.allocate() == UID_BASE


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


def test_single_file_loss_recovers_from_the_other_durable_copy(tmp_path):
    # S1 r6: a single-file loss is FULLY recovered from the other durable copy
    # — no dependence on (possibly incomplete) evidence.
    counter = tmp_path / "uids.json"
    a = UidAllocator(
        str(counter), passwd_path=str(tmp_path / "pw"),
        group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    a.reconstruct([], [])
    assert a.allocate() == UID_BASE            # both copies now hold UID_BASE
    os.remove(str(counter))                    # counter LOST, anchor survives
    b = UidAllocator(
        str(counter), passwd_path=str(tmp_path / "pw"),
        group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    b.reconstruct([], [])                       # recovers from the anchor
    assert b.allocate() == UID_BASE + 1        # continues, never reissues
    # And the counter copy was rewritten on the recovering reconstruct/allocate.
    assert counter.exists()


def test_invalid_durable_file_present_no_valid_copy_refuses(tmp_path):
    # S1 r6: a present-but-invalid durable file (stale-low) with no valid copy
    # and no evidence still REFUSES (prior-existence signal → not fresh).
    counter = tmp_path / "uids.json"; counter.write_text('{"high_water": 0}')
    a = UidAllocator(str(counter), passwd_path=str(tmp_path / "pw"),
                     group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    with pytest.raises(UidStateError):
        a.reconstruct([], [])


def test_both_durable_written_on_fresh_and_persist(tmp_path):
    # S1 r6: a genuine fresh install writes BOTH durable copies; both carry the
    # high-water after an allocate.
    counter = tmp_path / "uids.json"
    anchor = tmp_path / "uids.json.initialized"
    a = UidAllocator(str(counter), passwd_path=str(tmp_path / "pw"),
                     group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    a.reconstruct([], [])
    assert counter.exists() and anchor.exists()
    a.allocate()
    import json as _json
    assert _json.loads(counter.read_text())["high_water"] == UID_BASE
    assert _json.loads(anchor.read_text())["high_water"] == UID_BASE


def test_persist_tolerates_one_durable_write_failing(tmp_path, monkeypatch):
    # S1 r6 write policy: tolerate ONE durable write failing (the other carries
    # the high-water); poison only if BOTH fail. Here the anchor write fails but
    # the counter succeeds → allocation proceeds, counter holds the value.
    import engagement_uids as eu
    counter = tmp_path / "uids.json"
    anchor = tmp_path / "uids.json.initialized"
    a = eu.UidAllocator(str(counter), passwd_path=str(tmp_path / "pw"),
                        group_path=str(tmp_path / "gr"), proc_scanner=_NO_LIVE)
    a.reconstruct([], [])
    real = eu.atomic_write_json
    def _fail_anchor(path, data, **kw):
        if str(path).endswith(".initialized"):
            raise OSError("ENOSPC on anchor")
        return real(path, data, **kw)
    monkeypatch.setattr(eu, "atomic_write_json", _fail_anchor)
    assert a.allocate() == UID_BASE            # tolerated — not poisoned
    import json as _json
    assert _json.loads(counter.read_text())["high_water"] == UID_BASE


def test_proc_scan_failure_refuses_fail_closed(tmp_path):
    # S1 r2: if the /proc live-uid scan cannot run at all, reconstruct must
    # REFUSE (UidStateError), never proceed blind to which uids are live —
    # same fail-closed posture as a malformed counter.
    def _boom():
        raise OSError("/proc unavailable")
    a = UidAllocator(str(tmp_path / "uids.json"), proc_scanner=_boom)
    with pytest.raises(UidStateError):
        a.reconstruct(known_uids=[], dir_owner_uids=[])


def test_refold_live_uids_raises_high_water(tmp_path):
    # S1 r3: a survivor appearing AFTER the initial reconstruct is folded in by
    # the post-sweep refold, so a later allocate cannot reissue its uid.
    live = {"uids": set()}
    a = UidAllocator(
        str(tmp_path / "uids.json"),
        passwd_path=str(tmp_path / "passwd"), group_path=str(tmp_path / "group"),
        proc_scanner=lambda: set(live["uids"]),
    )
    a.reconstruct(known_uids=[], dir_owner_uids=[])   # clean at boot
    assert a._hw == UID_BASE - 1
    live["uids"] = {UID_BASE + 1}                      # survivor escapes later
    a.refold_live_uids()
    assert a.allocate() == UID_BASE + 2                # strictly above survivor


def test_refold_persisted_survives_reload(tmp_path):
    p = str(tmp_path / "uids.json")
    live = {"uids": set()}
    a = UidAllocator(p, passwd_path=str(tmp_path / "pw"),
                     group_path=str(tmp_path / "gr"),
                     proc_scanner=lambda: set(live["uids"]))
    a.reconstruct([], [])         # clean at boot (fresh)
    live["uids"] = {UID_BASE + 5}
    a.refold_live_uids()          # folds 200005, persists both durable copies
    b = UidAllocator(p, proc_scanner=_NO_LIVE); b.reconstruct([], [])
    assert b.allocate() == UID_BASE + 6   # persisted high-water carried over


def test_refold_unscannable_proc_refuses_fail_closed(tmp_path):
    def _boom():
        raise OSError("/proc unavailable")
    a = UidAllocator(str(tmp_path / "uids.json"), proc_scanner=_NO_LIVE)
    a.reconstruct([], [])
    a._proc_scanner = _boom
    with pytest.raises(UidStateError):
        a.refold_live_uids()


def test_refold_scan_failure_poisons_subsequent_allocate(tmp_path):
    # S1 r4 (Sol S1): a refold scan failure must not merely refuse legacy
    # backfill — it POISONS the allocator, so a later normal create()-path
    # allocate() ALSO refuses rather than handing out a uid from a stale
    # high-water (which could reissue a live survivor's uid).
    def _boom():
        raise OSError("/proc gone")
    a = UidAllocator(
        str(tmp_path / "uids.json"),
        passwd_path=str(tmp_path / "pw"), group_path=str(tmp_path / "gr"),
        proc_scanner=_NO_LIVE)
    a.reconstruct([], [])
    assert a.allocate() == UID_BASE        # proven-good before the failure
    a._proc_scanner = _boom
    with pytest.raises(UidStateError):
        a.refold_live_uids()
    # The create()-path allocate() now refuses too (poisoned) — no stale reissue.
    with pytest.raises(UidStateError):
        a.allocate()


def test_persist_failure_during_refold_is_uidstateerror_and_poisons(tmp_path, monkeypatch):
    # S1 r4 (Terra S2): a persist OSError (ENOSPC/EIO) must not escape as a bare
    # OSError — it is converted to UidStateError AND poisons the allocator, so
    # the replay call site's ``except UidStateError`` catches it and every later
    # allocate() refuses.
    import engagement_uids as eu
    live = {"uids": set()}
    a = eu.UidAllocator(
        str(tmp_path / "uids.json"),
        passwd_path=str(tmp_path / "pw"), group_path=str(tmp_path / "gr"),
        proc_scanner=lambda: set(live["uids"]))
    a.reconstruct([], [])                   # real persist OK
    live["uids"] = {UID_BASE + 1}           # refold will change the high-water
    def _no_disk(*args, **kwargs):
        raise OSError("ENOSPC")
    monkeypatch.setattr(eu, "atomic_write_json", _no_disk)
    with pytest.raises(UidStateError):      # NOT a bare OSError
        a.refold_live_uids()
    with pytest.raises(UidStateError):      # poisoned → create()-path refuses
        a.allocate()


def test_persist_failure_during_allocate_is_uidstateerror_and_poisons(tmp_path, monkeypatch):
    # S1 r4: the same fail-closed cut on the allocate() persist path — a persist
    # failure poisons and raises UidStateError, and the uid is NOT returned
    # (never hand out a uid that did not reach disk).
    import engagement_uids as eu
    a = eu.UidAllocator(
        str(tmp_path / "uids.json"),
        passwd_path=str(tmp_path / "pw"), group_path=str(tmp_path / "gr"),
        proc_scanner=_NO_LIVE)
    a.reconstruct([], [])
    monkeypatch.setattr(eu, "atomic_write_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("EIO")))
    with pytest.raises(UidStateError):
        a.allocate()
    # Restore disk; the allocator stays poisoned until a successful reconstruct.
    monkeypatch.undo()
    with pytest.raises(UidStateError):
        a.allocate()
    a.reconstruct([], [])                   # successful reconstruct clears poison
    assert a.allocate() == UID_BASE


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


def test_scan_proc_uids_folds_all_uid_fields_incl_fsuid(tmp_path):
    # S1 r4 (fsuid finding): DAC uses the FILESYSTEM uid, not the real uid. A
    # genuinely non-root survivor can hold a LOW real uid but a HIGH euid/suid/
    # fsuid (setpriv --ruid 65534 --euid 200000 ...). scan_proc_uids must fold
    # EVERY uid field (and Gid fields) so 200000 is seen and never reissued.
    from engagement_uids import scan_proc_uids
    proc = tmp_path / "proc"
    (proc / "111").mkdir(parents=True)
    (proc / "111" / "status").write_text(
        "Name:\treader\n"
        "Uid:\t65534\t200000\t200000\t200000\n"   # real low, euid/suid/fsuid high
        "Gid:\t65534\t200001\t200001\t200001\n")
    got = scan_proc_uids(str(proc))
    assert 200000 in got            # fsuid/euid folded despite low real uid
    assert 200001 in got            # Gid fields folded too (defense-in-depth)
    # A uniform-high real uid still works; a low id anywhere is ignored.
    (proc / "222").mkdir()
    (proc / "222" / "status").write_text(
        "Uid:\t200007\t200007\t200007\t200007\n")
    assert 200007 in scan_proc_uids(str(proc))


def test_scan_proc_uids_reads_per_thread_task_status(tmp_path):
    # S1 r5 (per-thread DiD): /proc/<pid> lists only the tgid leader, so a
    # per-thread setresuid/setfsuid worker (a non-leader <tid>) is invisible at
    # the process level. scan_proc_uids must enumerate /proc/<pid>/task/<tid>.
    from engagement_uids import scan_proc_uids
    proc = tmp_path / "proc"
    # Leader thread stays at a low uid; a worker THREAD (tid 777) dropped to
    # engagement uid 200000.
    (proc / "500" / "task" / "500").mkdir(parents=True)
    (proc / "500" / "task" / "500" / "status").write_text(
        "Uid:\t1000\t1000\t1000\t1000\n")
    (proc / "500" / "task" / "777").mkdir(parents=True)
    (proc / "500" / "task" / "777" / "status").write_text(
        "Uid:\t200000\t200000\t200000\t200000\n")
    assert 200000 in scan_proc_uids(str(proc))


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
