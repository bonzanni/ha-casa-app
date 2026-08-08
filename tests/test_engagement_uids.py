import json, os, pytest
from engagement_uids import UidAllocator, UidStateError, UID_BASE, UNALLOCATED_UID


def test_allocate_is_monotonic_and_never_reused(tmp_path):
    a = UidAllocator(str(tmp_path / "uids.json"))
    a.reconstruct(known_uids=[], dir_owner_uids=[])
    first = a.allocate(); second = a.allocate()
    assert first == UID_BASE and second == UID_BASE + 1


def test_reconstruct_takes_max_over_all_sources_not_liveness(tmp_path):
    a = UidAllocator(str(tmp_path / "uids.json"))
    a.reconstruct(known_uids=[UID_BASE + 5], dir_owner_uids=[UID_BASE + 9])
    assert a.allocate() == UID_BASE + 10   # never below any seen uid


def test_corrupt_counter_file_refuses(tmp_path):
    p = tmp_path / "uids.json"; p.write_text("{not json")
    a = UidAllocator(str(p))
    with pytest.raises(UidStateError):
        a.reconstruct(known_uids=[], dir_owner_uids=[])


def test_allocate_before_reconstruct_refuses(tmp_path):
    a = UidAllocator(str(tmp_path / "uids.json"))
    with pytest.raises(UidStateError):
        a.allocate()


def test_persist_survives_reload(tmp_path):
    p = str(tmp_path / "uids.json")
    a = UidAllocator(p); a.reconstruct([], []); a.allocate(); a.allocate()
    b = UidAllocator(p); b.reconstruct([], [])
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
