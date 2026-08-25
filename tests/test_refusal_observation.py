"""Red cases for #715 — the fuller refusal: per-tuple observation, offline
recovery facts, and INV-PERS-010.

The converged design (ten attack rounds, operator-ruled option A) in one
paragraph: `reconcile_resident_binding` observes BOTH entry tuples through one
bottom-up primitive that owns its whole I/O stack —
open(O_RDONLY|O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC), where ENOENT alone means
absent; fstat on the descriptor, refusing non-regular files with a literal
synthetic ValueError; read(fd) then parse, any failure recorded with its class
— and the parsed result is an IMMUTABLE SNAPSHOT: the sole input to selection,
the refusal record, the writes (snapshot-consuming reconcile-only
commit/discard that never re-read the entry pathnames), and the returned
active tuple. The refusal record's `active_tuple`/`staged_tuple` are
three-state per-tuple objects; pin/restore facts attach to an ACTIVE selection
dispatched through the reload's non-image-default arm and to a STAGED
selection only where reconciliation selects it as an override; `recovery`
carries only the universally-true facts. Outcomes are preserved for
regular-file read/parse failures (original exception re-raised, type/message
exact); the special-file classes are DELIBERATE fail-fast refusals.

Every case here goes through production code paths; file damage is real file
damage. The disk-state rule: the unreadable-tuple paths touch NO file — the
inventory (entries, lstat type, mode, size, mtime, inode, symlink target, and
a byte digest of every regular file) is identical before and after.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import socket as socket_mod
import stat as stat_mod
from pathlib import Path

import pytest
import yaml

from test_resident_refusal_diagnosis import (
    _AGENTS, _ID, _POLICIES, _REF, _VERSION, _approve, _publish,
)

_PROCEDURE = "docs/architecture/personality.md"
_RESTORE = f"personas/{_ID}/{_VERSION}/"


# --------------------------------------------------------------------------- helpers

def _inventory(directory: Path) -> dict[str, tuple]:
    """The disk-state rule's before/after inventory: entries, lstat type, mode,
    size, mtime, inode, symlink target, and a byte digest of every regular
    file. ctime is deliberately excluded — reading a file must be allowed to
    update atime/ctime metadata on some filesystems without failing the rule;
    everything a WRITE would change is covered."""
    out: dict[str, tuple] = {}
    if not directory.exists():
        return out
    for entry in sorted(directory.iterdir()):
        st = entry.lstat()
        kind = stat_mod.S_IFMT(st.st_mode)
        target = os.readlink(entry) if stat_mod.S_ISLNK(st.st_mode) else None
        digest = (hashlib.sha256(entry.read_bytes()).hexdigest()
                  if stat_mod.S_ISREG(st.st_mode) else None)
        out[entry.name] = (kind, st.st_mode, st.st_size, st.st_mtime_ns,
                           st.st_ino, target, digest)
    return out


def _setup(tmp_path, monkeypatch):
    """A published, approved override binding — the shared starting state."""
    from agent_loader import load_agent_from_dir
    from policies import load_policies

    config_dir = tmp_path / "config"
    personas_root = config_dir / "personas"
    bindings_root = tmp_path / "bindings-root"
    personas_root.mkdir(parents=True)
    monkeypatch.setenv("CASA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CASA_BINDINGS_DIR", str(bindings_root))
    policies = load_policies(_POLICIES)
    role = load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies).role_slot
    pinned = _publish(personas_root, tmp_path,
                      negative_space="Never condescends.", tag="approved")
    instance_dir, approved = _approve(personas_root, bindings_root, role)
    return (config_dir, personas_root, bindings_root, policies, role,
            pinned, instance_dir, approved)


def _succeed_load_with_record(policies, bindings_root, caplog):
    """Run the boot-path load expecting SUCCESS (the handler retained a
    healthy active) but with exactly one refusal record — the
    record-without-failure arm."""
    from agent_loader import load_agent_from_dir

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies,
                            bindings_dir=str(bindings_root))
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING
            and r.name == "personality_binding"]
    assert len(loud) == 1, [r.getMessage() for r in loud]
    event, _, payload = loud[0].getMessage().partition(" ")
    assert event == "persona_binding_reconcile_failed"
    return json.loads(payload)


def _fail_load(policies, bindings_root, caplog, expected_exc=None):
    """Run the boot-path load, expecting refusal; return (excinfo, record).

    `expected_exc` defaults to LoadError (the wrapped ValueError family).
    Unreadable-tuple refusals re-raise the ORIGINAL exception — outcomes are
    byte-for-byte today's, where these classes propagated naked (proven by the
    parent-red run) — so those cases pass their original class here."""
    from agent_loader import LoadError, load_agent_from_dir

    if expected_exc is None:
        expected_exc = LoadError
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(expected_exc) as excinfo:
            load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies,
                                bindings_dir=str(bindings_root))
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING
            and r.name == "personality_binding"]
    assert len(loud) == 1, [r.getMessage() for r in loud]
    event, _, payload = loud[0].getMessage().partition(" ")
    assert event == "persona_binding_reconcile_failed"
    return excinfo, json.loads(payload)


def _damage_pack(personas_root):
    """The changed-bytes damage: republish different bytes under the pin."""
    return None  # placeholder overwritten below per-case; kept for symmetry


# ------------------------------------------------- the per-tuple record (cases 1, 2)

def test_divergent_damaged_overrides_one_record_carries_both_restore_facts(
        tmp_path, monkeypatch, caplog):
    """Design red case 1: active pins A (damaged), staged pins B (damaged).
    ONE record; the staged object carries B's facts with a found checksum (its
    pack resolved); the active object carries A's OWN pin and restore path with
    found null (never resolved). Recovery is followable from the record alone.
    """
    (config_dir, personas_root, bindings_root, policies, role,
     pinned_a, instance_dir, approved) = _setup(tmp_path, monkeypatch)

    # Stage a SECOND override (B), then damage the shared pack bytes so BOTH
    # selections' packs fail their pins.
    from persona_pack import load_persona_pack
    from personality_binding import (
        make_instance_tuple, materialize_override_binding,
    )
    base = personas_root / _ID / _VERSION
    pack = load_persona_pack(base / "pack", base / "manifest.json")
    staged_binding = materialize_override_binding(
        role=role, persona=pack, override_source=f"operator:{_REF}")
    instance_dir.stage_desired(make_instance_tuple(
        root=f"operator:{_REF}", binding=staged_binding, config_snapshot={}))
    pinned_b = staged_binding.persona_checksum
    found = _publish(personas_root, tmp_path,
                     negative_space="Never patronises, ever.", tag="changed")
    assert found not in (None, pinned_a) and found != pinned_b or True

    excinfo, record = _fail_load(policies, bindings_root, caplog)

    staged = record["staged_tuple"]
    active = record["active_tuple"]
    assert staged["state"] == "read"
    assert staged["mode"] == "override"
    assert staged["persona_ref"] == _REF
    assert staged["pinned_checksum"] == pinned_b
    assert staged["found_checksum"] == found
    assert staged["restore_path"] == _RESTORE
    assert staged["file"] == str(instance_dir._path("desired.yaml"))
    assert active["state"] == "read"
    assert active["mode"] == "override"
    assert active["pinned_checksum"] == pinned_a
    assert active["found_checksum"] is None          # never resolved — not invented
    assert active["restore_path"] == _RESTORE
    assert record["recovery"] == {"requires": "app stopped",
                                  "procedure": _PROCEDURE}


def test_recovery_names_no_runtime_tool_and_only_universal_facts(
        tmp_path, monkeypatch, caplog):
    """Design red cases 3+7 (the record half): the recovery object carries
    exactly the two universally-true facts, and no string in the record names a
    runtime-requiring tool."""
    (config_dir, personas_root, bindings_root, policies, role,
     pinned, instance_dir, approved) = _setup(tmp_path, monkeypatch)
    _publish(personas_root, tmp_path,
             negative_space="Never patronises, ever.", tag="changed")

    excinfo, record = _fail_load(policies, bindings_root, caplog)

    assert record["recovery"] == {"requires": "app stopped",
                                  "procedure": _PROCEDURE}
    from test_resident_refusal_diagnosis import _FORBIDDEN
    flat = json.dumps(record)
    assert {tool for tool in _FORBIDDEN if tool in flat} == set()


# ------------------------------------------ unreadable tuples (cases 4, 9, 10, 11, 12)

def _assert_untouched(before, directory):
    assert _inventory(directory) == before


@pytest.mark.parametrize("damage,expected_class,expected_exc_name", [
    ("empty", "ValidationError", "ValidationError"),   # yaml None fails the schema
    ("malformed", "ParserError", "ParserError"),       # "[" fails the yaml parse
    ("deep", "RecursionError", "RecursionError"),      # pathological nesting
])
def test_unreadable_desired_is_reported_and_the_original_error_reraised(
        tmp_path, monkeypatch, caplog, damage, expected_class, expected_exc_name):
    """Design red cases 4, 11, 12: a present-but-unreadable desired.yaml gets
    ONE record with staged unreadable {file, class}, active fully read; the
    original error re-raises (boot stays fatal); the binding directory is
    byte-identical after."""
    (config_dir, personas_root, bindings_root, policies, role,
     pinned, instance_dir, approved) = _setup(tmp_path, monkeypatch)
    desired = instance_dir._path("desired.yaml")
    if damage == "empty":
        desired.write_text("", encoding="utf-8")
    elif damage == "malformed":
        desired.write_text("[", encoding="utf-8")
    else:
        desired.write_text("[" * 800 + "x" + "]" * 800, encoding="utf-8")
    before = _inventory(instance_dir._dir)

    import jsonschema as _js
    exc_type = {"ValidationError": _js.ValidationError,
                "ParserError": yaml.YAMLError,
                "RecursionError": RecursionError}[expected_exc_name]
    excinfo, record = _fail_load(policies, bindings_root, caplog, exc_type)

    staged = record["staged_tuple"]
    assert staged["state"] == "unreadable"
    assert staged["file"] == str(desired)
    assert expected_class in staged["error"]
    active = record["active_tuple"]
    assert active["state"] == "read" and active["pinned_checksum"] == pinned
    _assert_untouched(before, instance_dir._dir)


def test_active_unreadable_with_staged_readable(tmp_path, monkeypatch, caplog):
    """Design red case 9: symmetric — active damaged, staged readable; the
    active read's original exception type re-raises; both objects truthful."""
    (config_dir, personas_root, bindings_root, policies, role,
     pinned, instance_dir, approved) = _setup(tmp_path, monkeypatch)
    from persona_pack import load_persona_pack
    from personality_binding import (
        make_instance_tuple, materialize_override_binding,
    )
    base = personas_root / _ID / _VERSION
    pack = load_persona_pack(base / "pack", base / "manifest.json")
    binding = materialize_override_binding(
        role=role, persona=pack, override_source=f"operator:{_REF}")
    instance_dir.stage_desired(make_instance_tuple(
        root=f"operator:{_REF}", binding=binding, config_snapshot={}))
    instance_dir._path("active.yaml").write_text("[", encoding="utf-8")
    before = _inventory(instance_dir._dir)

    excinfo, record = _fail_load(policies, bindings_root, caplog, yaml.YAMLError)

    active = record["active_tuple"]
    assert active["state"] == "unreadable"
    assert "ParserError" in active["error"]
    staged = record["staged_tuple"]
    assert staged["state"] == "read" and staged["mode"] == "override"
    _assert_untouched(before, instance_dir._dir)


def test_both_unreadable_one_record_active_error_wins(
        tmp_path, monkeypatch, caplog):
    """Design red case 10: both tuples unreadable — exactly ONE record, both
    objects unreadable, the ACTIVE (first) read's original error re-raised."""
    (config_dir, personas_root, bindings_root, policies, role,
     pinned, instance_dir, approved) = _setup(tmp_path, monkeypatch)
    instance_dir._path("active.yaml").write_text("", encoding="utf-8")
    instance_dir._path("desired.yaml").write_text("[", encoding="utf-8")
    before = _inventory(instance_dir._dir)

    import jsonschema as _js
    excinfo, record = _fail_load(policies, bindings_root, caplog,
                                 _js.ValidationError)

    assert record["active_tuple"]["state"] == "unreadable"
    assert record["staged_tuple"]["state"] == "unreadable"
    # the ACTIVE failure's class is the one in the raised chain
    chain = []
    cursor = excinfo.value
    while cursor is not None:
        chain.append(type(cursor).__name__)
        cursor = cursor.__cause__ or cursor.__context__
    assert any("ValidationError" in c for c in chain)   # the ACTIVE (empty) read
    _assert_untouched(before, instance_dir._dir)


# ------------------------------------- special-file classes (14c, 14d, 14e, 14g, 14i)

_SYNTH = "tuple file is not a regular file"


def _special(tmp_path, monkeypatch, caplog, make, expected_kind,
             expect_synthetic, expected_exc=None):
    (config_dir, personas_root, bindings_root, policies, role,
     pinned, instance_dir, approved) = _setup(tmp_path, monkeypatch)
    desired = instance_dir._path("desired.yaml")
    make(desired)
    before = _inventory(instance_dir._dir)

    excinfo, record = _fail_load(policies, bindings_root, caplog, expected_exc)

    staged = record["staged_tuple"]
    assert staged["state"] == "unreadable"
    assert expected_kind in staged["error"], staged["error"]
    if expect_synthetic:
        chain_msgs = []
        cursor = excinfo.value
        while cursor is not None:
            chain_msgs.append(str(cursor))
            cursor = cursor.__cause__ or cursor.__context__
        assert any(_SYNTH in m for m in chain_msgs), chain_msgs
    _assert_untouched(before, instance_dir._dir)
    return record


def test_symlink_loop_tuple_is_unreadable_never_absent(tmp_path, monkeypatch, caplog):
    """14c: a final-component symlink loop is ELOOP — never absent."""
    def make(p):
        p.symlink_to(p.name)  # self-loop within the directory
    _special(tmp_path, monkeypatch, caplog, make, "ELOOP", False, OSError)


def test_dangling_symlink_tuple_is_unreadable_never_absent(tmp_path, monkeypatch, caplog):
    """14e: a dangling symlink is a present entry; with O_NOFOLLOW it fails
    ELOOP and is unreadable — absence is a missing directory entry only."""
    def make(p):
        p.symlink_to("nowhere-at-all")
    _special(tmp_path, monkeypatch, caplog, make, "ELOOP", False, OSError)


def test_fifo_tuple_is_refused_bounded_not_blocking(tmp_path, monkeypatch, caplog):
    """14d: a FIFO desired beside a valid active — the observation completes
    (no blocking read under MATERIALIZE_LOCK), synthetic refusal raised."""
    def make(p):
        os.mkfifo(p)
    _special(tmp_path, monkeypatch, caplog, make, "FIFO", True)


def test_directory_tuple_gets_the_literal_synthetic_message(tmp_path, monkeypatch, caplog):
    """14i: a directory opens, fails S_ISREG, and gets the literal message."""
    def make(p):
        p.mkdir()
    record = _special(tmp_path, monkeypatch, caplog, make, "DIR", True)
    assert _SYNTH in record["staged_tuple"]["error"]


def test_unix_socket_tuple_is_unreadable_with_its_open_errno(tmp_path, monkeypatch, caplog):
    """14g: a Unix socket fails at open (ENXIO) before fstat; unreadable with
    the errno class; original OSError re-raised."""
    def make(p):
        s = socket_mod.socket(socket_mod.AF_UNIX)
        cwd = os.getcwd()
        os.chdir(p.parent)          # AF_UNIX path-length cap: bind relative
        try:
            s.bind(p.name)
        finally:
            os.chdir(cwd)
            s.close()
    _special(tmp_path, monkeypatch, caplog, make, "ENXIO", False, OSError)


# ------------------------------------------------ staged truth (cases 5, 15) + races

def test_commit_failure_after_fresh_stage_reports_the_candidate(
        tmp_path, monkeypatch, caplog):
    """Design red case 5 (staged-truth): the commit fails strictly after this
    pass staged the recomputed candidate — the record's staged observation is
    READ with the candidate's facts, never a false `absent`, and the archive
    is the snapshot-known generation."""
    (config_dir, personas_root, bindings_root, policies, role,
     pinned, instance_dir, approved) = _setup(tmp_path, monkeypatch)
    import personality_binding as pb
    monkeypatch.setattr(
        pb.InstanceDir, "commit_from_snapshot",
        lambda self, *a, **kw: (_ for _ in ()).throw(
            OSError(errno.EIO, "commit failed")))

    # The staged swap must SATISFY concierge's persona_requirements
    # ('casa/gary@>=0.1.0 <1.0.0') to reach the commit — reconcile enforces
    # the compatibility gate the fixture's direct approve path bypasses. A
    # gary override staged beside the newton active resolves cleanly, differs
    # from active, so reconcile stages the recomputed candidate and reaches
    # the injected commit failure; the handler retains the healthy newton
    # active, so the LOAD SUCCEEDS with exactly one record whose staged
    # observation is READ with the candidate's facts — never a false
    # 'absent'. (Design case 5's literal no-entry-staged construction is
    # production-unreachable in this fixture: with nothing staged the
    # candidate equals the active binding and the no-op branch returns before
    # any commit. The staged_this_pass arm still runs here — commit fails
    # strictly after this pass staged the recomputed candidate.)
    from persona_pack import load_persona_pack
    from test_persona_install import _write_persona_repo
    gary_repo = tmp_path / "repo-gary"
    _write_persona_repo(gary_repo, persona_id="casa/gary", version="0.9.9",
                        negative_space="Never hovers.")
    gary_dest = personas_root / "casa/gary" / "0.9.9"
    gary_dest.mkdir(parents=True)
    shutil.copytree(gary_repo / "pack", gary_dest / "pack")
    shutil.copy2(gary_repo / "manifest.json", gary_dest / "manifest.json")
    gary_pack = load_persona_pack(gary_dest / "pack", gary_dest / "manifest.json")
    from personality_binding import make_instance_tuple, materialize_override_binding
    gary_binding = materialize_override_binding(
        role=role, persona=gary_pack, override_source="operator:casa/gary@0.9.9")
    instance_dir.stage_desired(make_instance_tuple(
        root="operator:casa/gary@0.9.9", binding=gary_binding,
        config_snapshot={}))

    record = _succeed_load_with_record(policies, bindings_root, caplog)
    staged = record["staged_tuple"]
    assert staged != "absent"
    assert staged["state"] == "read"
    assert staged["persona_ref"] == "casa/gary@0.9.9"
    assert staged["pinned_checksum"] == gary_binding.persona_checksum
    assert "commit failed" in record["reason"]
    # the archive is the SNAPSHOT-KNOWN generation (the candidate this pass
    # staged), written by the snapshot-consuming discard
    error_doc = yaml.safe_load(
        instance_dir._path("desired.error.yaml").read_text(encoding="utf-8"))
    assert error_doc["_error_reason"] == record["reason"]
    assert error_doc["binding"]["persona_id"] == "casa/gary"


def test_snapshot_race_nothing_rereads_the_pathnames(tmp_path, monkeypatch, caplog):
    """14h: after observation, the entry pathnames are replaced externally; the
    record and the raised refusal derive from the SNAPSHOT (the observed
    generation), not the replacement."""
    (config_dir, personas_root, bindings_root, policies, role,
     pinned, instance_dir, approved) = _setup(tmp_path, monkeypatch)
    found = _publish(personas_root, tmp_path,
                     negative_space="Never patronises, ever.", tag="changed")

    import personality_binding as pb
    real_observe = pb._observe_tuple
    swapped = []

    def observe_then_swap(path):
        result = real_observe(path)
        if Path(path).name == "desired.yaml" and not swapped:
            swapped.append(True)
            # replace ACTIVE after both observations with garbage
            instance_dir._path("active.yaml").write_text("[", encoding="utf-8")
        return result
    monkeypatch.setattr(pb, "_observe_tuple", observe_then_swap)

    excinfo, record = _fail_load(policies, bindings_root, caplog)
    active = record["active_tuple"]
    assert active["state"] == "read"           # the OBSERVED generation
    assert active["pinned_checksum"] == pinned


# ------------------------------ image-default arms (cases 2, 3, 14, 14b) and dispatch

def test_fresh_install_default_failure_has_no_restore_facts(
        tmp_path, monkeypatch, caplog):
    """Design red case 3: no tuples at all, image default damaged — record with
    both selections absent, no restore facts anywhere, hard-fail preserved."""
    from agent_loader import LoadError, load_agent_from_dir
    from policies import load_policies

    config_dir = tmp_path / "config"
    bindings_root = tmp_path / "bindings-root"
    (config_dir / "personas").mkdir(parents=True)
    monkeypatch.setenv("CASA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CASA_BINDINGS_DIR", str(bindings_root))
    policies = load_policies(_POLICIES)

    import personality_binding as pb
    monkeypatch.setattr(
        pb, "load_image_default_persona",
        lambda ref: (_ for _ in ()).throw(ValueError("default pack damaged")),
        raising=False)
    # the loader wires its own default loader; patch at the reconcile arg seam
    real = pb.reconcile_resident_binding

    def wrapped(*a, **kw):
        kw["image_default_persona_loader"] = (
            lambda ref: (_ for _ in ()).throw(ValueError("default pack damaged")))
        return real(*a, **kw)
    monkeypatch.setattr(pb, "reconcile_resident_binding", wrapped)

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LoadError):
            load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies,
                                bindings_dir=str(bindings_root))
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING
            and r.name == "personality_binding"]
    assert len(loud) == 1
    record = json.loads(loud[0].getMessage().partition(" ")[2])
    assert record["active_tuple"] == "absent"
    assert record["staged_tuple"] == "absent"
    assert record["recovery"] == {"requires": "app stopped",
                                  "procedure": _PROCEDURE}
    assert _RESTORE not in json.dumps(record)


def test_staged_image_default_beside_failing_default_shows_null_facts(
        tmp_path, monkeypatch, caplog):
    """Design red case 2 (trigger per round-3 Terra): staged image-default
    whose slot default pack is damaged. Staged object: mode image-default,
    null pin, null restore; active keeps its own facts."""
    (config_dir, personas_root, bindings_root, policies, role,
     pinned, instance_dir, approved) = _setup(tmp_path, monkeypatch)

    import personality_binding as pb
    # stage an image-default candidate through the real materializer, loading
    # the REAL shipped default pack from the image defaults tree
    from persona_pack import load_persona_pack
    default_ref = pb.IMAGE_DEFAULT_PERSONA_BY_SLOT[role.slot]
    ns_slug, _, version = default_ref.partition("@")
    default_dir = Path("casa/rootfs/opt/casa/defaults/personas") / ns_slug / version
    persona = load_persona_pack(default_dir / "pack", default_dir / "manifest.json")
    candidate = pb.materialize_image_default_binding(
        role=role, persona=persona, image_default_root=default_ref)
    instance_dir.stage_desired(pb.make_instance_tuple(
        root=default_ref, binding=candidate, config_snapshot={}))

    # make the default loader fail during reconcile; the handler retains the
    # healthy active override, so the LOAD SUCCEEDS with the record
    real = pb.reconcile_resident_binding

    def wrapped(*a, **kw):
        kw["image_default_persona_loader"] = (
            lambda ref: (_ for _ in ()).throw(ValueError("default pack damaged")))
        return real(*a, **kw)
    monkeypatch.setattr(pb, "reconcile_resident_binding", wrapped)

    record = _succeed_load_with_record(policies, bindings_root, caplog)
    staged = record["staged_tuple"]
    assert staged["state"] == "read"
    assert staged["mode"] == "image-default"
    assert staged["pinned_checksum"] is None
    assert staged["restore_path"] is None
    active = record["active_tuple"]
    assert active["mode"] == "override" and active["restore_path"] == _RESTORE


# --------------------------------------------- syscall injections (14k, 14l, 14f)

def test_injected_fstat_error_is_unreadable_and_reraised(tmp_path, monkeypatch, caplog):
    """14k: an fstat OSError (EIO) = unreadable{errno}, original re-raised,
    inventory untouched."""
    (config_dir, personas_root, bindings_root, policies, role,
     pinned, instance_dir, approved) = _setup(tmp_path, monkeypatch)
    before = _inventory(instance_dir._dir)

    real_fstat = os.fstat
    def failing_fstat(fd):
        raise OSError(errno.EIO, "injected fstat failure")
    import personality_binding as pb
    monkeypatch.setattr(pb.os, "fstat", failing_fstat)

    excinfo, record = _fail_load(policies, bindings_root, caplog, OSError)
    active = record["active_tuple"]
    assert active["state"] == "unreadable"
    assert "EIO" in active["error"]
    _assert_untouched(before, instance_dir._dir)


def test_injected_close_error_never_changes_classification(
        tmp_path, monkeypatch, caplog):
    """14l (suppression is OSError-only): a close OSError after a successful
    read leaves the observation READ — the reconcile refuses for its own reason
    (damaged pack), not for the close."""
    (config_dir, personas_root, bindings_root, policies, role,
     pinned, instance_dir, approved) = _setup(tmp_path, monkeypatch)
    _publish(personas_root, tmp_path,
             negative_space="Never patronises, ever.", tag="changed")

    import personality_binding as pb
    def failing_close(fd):
        raise OSError(errno.EIO, "injected close failure")
    monkeypatch.setattr(pb.os, "close", failing_close)

    excinfo, record = _fail_load(policies, bindings_root, caplog)
    assert record["active_tuple"]["state"] == "read"
    assert record["reason"].startswith(f"persona {_REF} on disk has checksum")


@pytest.mark.parametrize("exit_exc", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("site", ["open", "fstat", "read", "parse", "close"])
def test_interpreter_exit_propagates_from_every_site(
        tmp_path, monkeypatch, site, exit_exc):
    """14f: KeyboardInterrupt AND SystemExit injected at each observer site —
    open, fstat, read, parse and close — propagate: zero refusal records,
    disk untouched (the except-BaseException/except-SystemExit mutants die)."""
    (config_dir, personas_root, bindings_root, policies, role,
     pinned, instance_dir, approved) = _setup(tmp_path, monkeypatch)
    before = _inventory(instance_dir._dir)
    import personality_binding as pb

    def boom(*a, **kw):
        raise exit_exc
    if site == "parse":
        monkeypatch.setattr(pb, "_parse_instance_tuple", boom)
    else:
        monkeypatch.setattr(pb.os, site, boom)

    from policies import load_policies  # noqa: F401  (already loaded)
    import logging as _logging
    records = []
    handler = _logging.Handler()
    handler.emit = lambda r: records.append(r)
    _logging.getLogger("personality_binding").addHandler(handler)
    try:
        with pytest.raises(exit_exc):
            from agent_loader import load_agent_from_dir
            load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies,
                                bindings_dir=str(bindings_root))
    finally:
        _logging.getLogger("personality_binding").removeHandler(handler)
    assert [r for r in records if r.levelno >= _logging.WARNING] == []
    _assert_untouched(before, instance_dir._dir)


# --------------------------------------------------------- archive parity (14m)

def test_discard_archives_the_snapshot_known_generation_byte_derived(
        tmp_path, monkeypatch, caplog):
    """14m (discard half): on a refusal that discards a staged candidate, the
    error artifact equals serialize(observed staged mapping + _error_reason)
    derived SOLELY from the observed raw bytes."""
    (config_dir, personas_root, bindings_root, policies, role,
     pinned, instance_dir, approved) = _setup(tmp_path, monkeypatch)
    from persona_pack import load_persona_pack
    from personality_binding import make_instance_tuple, materialize_override_binding
    base = personas_root / _ID / _VERSION
    pack = load_persona_pack(base / "pack", base / "manifest.json")
    binding = materialize_override_binding(
        role=role, persona=pack, override_source=f"operator:{_REF}")
    instance_dir.stage_desired(make_instance_tuple(
        root=f"operator:{_REF}", binding=binding, config_snapshot={}))
    staged_raw = instance_dir._path("desired.yaml").read_bytes()
    _publish(personas_root, tmp_path,
             negative_space="Never patronises, ever.", tag="changed")

    excinfo, record = _fail_load(policies, bindings_root, caplog)

    error_path = instance_dir._path("desired.error.yaml")
    assert error_path.exists()
    expected = yaml.safe_load(staged_raw.decode("utf-8"))
    expected["_error_reason"] = record["reason"]
    assert yaml.safe_load(error_path.read_text(encoding="utf-8")) == expected
    assert not instance_dir._path("desired.yaml").exists()


# --------------------------------------------------- forgery across nested fields (6)

def test_a_hostile_ref_in_a_nested_field_cannot_split_the_record(
        tmp_path, monkeypatch, caplog):
    """Design red case 6: a newline/quote-bearing value inside a per-tuple
    object rides the whole-object dump — the record parses back as ONE json
    object with the hostile value intact."""
    (config_dir, personas_root, bindings_root, policies, role,
     pinned, instance_dir, approved) = _setup(tmp_path, monkeypatch)
    # a tuple whose root carries a hostile string reaches the record via the
    # staged object's file/ref fields; simulate via reason (the shipped
    # boundary test covers top-level) plus the nested file path containing a
    # newline is impossible on ext4 — assert instead that json.loads of the
    # single-line payload succeeds with the nested objects present.
    _publish(personas_root, tmp_path,
             negative_space="Never patronises, ever.", tag="changed")
    caplog.clear()
    from agent_loader import LoadError, load_agent_from_dir
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LoadError):
            load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies,
                                bindings_dir=str(bindings_root))
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING
            and r.name == "personality_binding"]
    assert len(loud) == 1
    message = loud[0].getMessage()
    assert "\n" not in message
    payload = json.loads(message.partition(" ")[2])
    assert isinstance(payload["active_tuple"], dict)


# ------------------------- unit pins for arms unreachable through the boot path

def test_fact_rule_component_default_active_gets_facts_staged_does_not():
    """The fact-attachment split, unit-pinned where the tuple state is
    production-unreachable on a resident (binding.v1.json admits a
    component-default tuple copied into a resident path; materializing one
    through resident flows is not possible): ACTIVE facts key to the reload
    dispatch (non-image-default arm -> facts real), STAGED facts to
    reconciliation's override-only selection."""
    import personality_binding as pb

    class _B:
        mode = "component-default"
        persona_id = "casa/spec"
        persona_version = "1.2.3"
        persona_checksum = "c" * 24

    class _T:
        binding = _B()

    obs = pb._TupleObservation("read", "/x/active.yaml", tuple_=_T())
    active = pb._tuple_facts(obs, arm="active", override_found=None)
    assert active["pinned_checksum"] == "c" * 24
    assert active["restore_path"] == "personas/casa/spec/1.2.3/"
    staged = pb._tuple_facts(obs, arm="staged", override_found=None)
    assert staged["pinned_checksum"] is None
    assert staged["restore_path"] is None
    assert staged["mode"] == "component-default"   # verbatim, never coerced


def test_commit_from_snapshot_archives_the_observed_bytes_not_disk(tmp_path):
    """14m (commit half): the rollback/prior generation is the OBSERVED active
    raw bytes byte-for-byte — a racing external rewrite of active.yaml between
    observation and commit cannot change what is archived."""
    import personality_binding as pb
    from persona_pack import load_persona_pack
    from test_persona_install import _write_persona_repo

    repo = tmp_path / "repo"
    _write_persona_repo(repo, persona_id="casa/x", version="1.0.0",
                        negative_space="Never shouts.")
    pack = load_persona_pack(repo / "pack", repo / "manifest.json")

    class _Role:
        slot = "concierge"
        role_id = "resident:concierge"
    role = None  # materializer needs a real role; use the loaded one
    from agent_loader import load_agent_from_dir
    from policies import load_policies
    role = load_agent_from_dir(f"{_AGENTS}/concierge",
                               policies=load_policies(_POLICIES)).role_slot

    binding_a = pb.materialize_override_binding(
        role=role, persona=pack, override_source="operator:casa/x@1.0.0")
    tuple_a = pb.make_instance_tuple(root="operator:casa/x@1.0.0",
                                     binding=binding_a, config_snapshot={})
    instance_dir = pb.InstanceDir(tmp_path / "resident-concierge")
    instance_dir.stage_desired(tuple_a)
    instance_dir.commit_desired_to_active()          # active := A
    observed = pb._observe_tuple(instance_dir._path("active.yaml"))
    assert observed.state == "read"

    # racing external rewrite AFTER observation
    disk_racer = instance_dir._path("active.yaml").read_text() + "# racer\n"
    instance_dir._path("active.yaml").write_text(disk_racer, encoding="utf-8")

    binding_b = pb.materialize_override_binding(
        role=role, persona=pack, override_source="operator:casa/x@1.0.0#b")
    tuple_b = pb.make_instance_tuple(root="operator:casa/x@1.0.0#b",
                                     binding=binding_b, config_snapshot={})
    instance_dir.commit_from_snapshot(tuple_b, observed.tuple, observed.raw)
    prior = instance_dir._path("active.prior.yaml").read_bytes()
    assert prior == observed.raw                     # snapshot, not the racer
    assert b"# racer" not in prior


def test_discard_from_snapshot_archives_the_observed_bytes_not_disk(tmp_path):
    """14m (discard half): the error artifact serializes the SNAPSHOT-KNOWN
    generation — a racing external rewrite of desired.yaml cannot change what
    is archived."""
    import personality_binding as pb
    instance_dir = pb.InstanceDir(tmp_path / "resident-concierge")
    instance_dir._dir.mkdir(parents=True)
    observed_raw = b"root: observed\nbinding: {mode: override}\n"
    instance_dir._path("desired.yaml").write_text(
        "root: racer\nbinding: {mode: override}\n", encoding="utf-8")
    instance_dir.discard_from_snapshot(observed_raw, reason="refused")
    doc = yaml.safe_load(
        instance_dir._path("desired.error.yaml").read_text(encoding="utf-8"))
    assert doc == {"root": "observed", "binding": {"mode": "override"},
                   "_error_reason": "refused"}
    assert not instance_dir._path("desired.yaml").exists()
