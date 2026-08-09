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
