"""``callback_spool.py`` — the ``/data/callbacks`` spool protocol
(dirs/ready/index layout, claim/TTL/publish, the mint contract,
sweep/recovery; INV-CB-002).

The protocol's two load-bearing properties are pinned here with real
concurrency, not just single-threaded sequences:

* **exactly-once consumption** — the claim is a ``link(2)`` publish-once, so
  two *processes* racing the same pending state produce exactly one winner;
* **never partially visible** — a result appears in ``results/`` only as a
  hard link to an already-written, already-fsynced inode, so a collector
  polling in a tight loop can never read a short or invalid record.

Time is constructed with ``os.utime`` (never ``time.sleep``): mtime is the
single clock, and every TTL/skew case is therefore deterministic.
"""
import errno
import json
import logging
import multiprocessing
import os
import stat
import time
from pathlib import Path

import pytest

import callback_attempts as ca
import callback_spool as cs
from callback_spool import (
    PENDING_TTL_S,
    RESTORE_GRACE_S,
    RESULT_TTL_S,
    SKEW_S,
    TEMP_TTL_S,
    CallbackSpool,
    index_key,
    mint,
    state_hash,
)

PLUGIN = "acme"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def spool(tmp_path):
    s = CallbackSpool(tmp_path / "callbacks")
    s.ensure_plugin_dirs(PLUGIN)
    try:
        yield s
    finally:
        s.close()


def _pdir(spool, plugin=PLUGIN) -> Path:
    return Path(spool.root) / plugin


def _pending(spool, plugin=PLUGIN) -> Path:
    return _pdir(spool, plugin) / "pending"


def _results(spool, plugin=PLUGIN) -> Path:
    return _pdir(spool, plugin) / "results"


def _claims(spool, plugin=PLUGIN) -> Path:
    return _pdir(spool, plugin) / ".claims"


def _utime(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def _record(h: str) -> dict:
    return {"v": 1, "plugin": PLUGIN, "effective": f"plg-{PLUGIN}--oauth",
            "received_at": 1234567890, "raw_query": f"code=abc&state={h[:8]}",
            "query": [["code", "abc"], ["state", h[:8]]]}


def _put(path: Path, mtime: float, text: str = "{}") -> Path:
    path.write_text(text)
    _utime(path, mtime)
    return path


@pytest.fixture
def fs_events(monkeypatch):
    """Ordered syscall recorder for the protocol-sequence pins.

    ``callback_spool.os`` IS the stdlib ``os`` module, so these wrappers are
    global for the duration of one test; each delegates to the real syscall
    and ``monkeypatch`` restores them at teardown. Directory fsyncs are
    recorded by resolving the FD through ``/proc/self/fd`` so the *target*
    directory is visible in the trace, not an opaque integer.
    """
    events: list[tuple] = []
    real_fsync, real_link, real_unlink = os.fsync, os.link, os.unlink

    def _fd_path(fd):
        try:
            return os.readlink(f"/proc/self/fd/{fd}")
        except OSError:  # pragma: no cover — /proc always present on Linux
            return f"fd:{fd}"

    def fsync(fd):
        events.append(("fsync", _fd_path(fd)))
        return real_fsync(fd)

    def link(src, dst, **kw):
        events.append(("link", os.fspath(src), os.fspath(dst)))
        return real_link(src, dst, **kw)

    def unlink(path, **kw):
        events.append(("unlink", os.fspath(path)))
        return real_unlink(path, **kw)

    monkeypatch.setattr(cs.os, "fsync", fsync)
    monkeypatch.setattr(cs.os, "link", link)
    monkeypatch.setattr(cs.os, "unlink", unlink)
    return events


def _idx(events, needle) -> int:
    """Index of the first matching event. A 3-tuple matches exactly; a
    2-tuple whose target contains ``/`` matches by path SUFFIX (fsync targets
    are resolved to absolute paths), otherwise exactly — so ``("unlink", h)``
    can never be satisfied by the earlier ``.tmp-<h>`` unlink."""
    kind, target = needle[0], needle[1]
    for i, ev in enumerate(events):
        if ev[0] != kind:
            continue
        if len(needle) == 3:
            if tuple(ev[1:]) == tuple(needle[1:]):
                return i
        elif "/" in target:
            if str(ev[1]).endswith(target):
                return i
        elif str(ev[1]) == target:
            return i
    raise AssertionError(f"event {needle!r} not in {events!r}")


def _idx_after(events, needle, after: int) -> int:
    """First match strictly after index *after* — the publish sequence unlinks
    the reserved temp name twice (defensively on entry, then after the link),
    so ordering assertions must name which one they mean."""
    return after + 1 + _idx(events[after + 1:], needle)


# ---------------------------------------------------------------------------
# directories / modes
# ---------------------------------------------------------------------------


def test_ensure_plugin_dirs_creates_0770_tree(spool):
    for sub in ("pending", "results", ".claims", "attempts"):
        d = _pdir(spool) / sub
        assert d.is_dir()
        assert stat.S_IMODE(d.stat().st_mode) == 0o770
    assert stat.S_IMODE(_pdir(spool).stat().st_mode) == 0o770


def test_ensure_plugin_dirs_is_idempotent(spool):
    h = state_hash("keepme")
    _put(_pending(spool) / f"{h}.json", time.time())
    spool.ensure_plugin_dirs(PLUGIN)
    assert (_pending(spool) / f"{h}.json").exists()


def test_ensure_plugin_dirs_never_rewrites_a_valid_token(spool):
    """A VALID ``.dir-id`` is minted exactly once per directory life: any
    later pass leaves it byte-identical (an in-flight claim's captured token
    must stay comparable for the claim's whole life)."""
    token_path = _pdir(spool) / ".dir-id"
    before = token_path.read_bytes()
    spool.ensure_plugin_dirs(PLUGIN)
    spool.ensure_plugin_dirs(PLUGIN)
    assert token_path.read_bytes() == before


def test_ensure_plugin_dirs_repairs_a_malformed_token(spool):
    """A POSITIVELY malformed token (wrong grammar/size, or dir-shaped) is
    retired and re-minted — and an in-flight claim carrying the old token
    then fails closed at discard/publish like any other identity drift."""
    token_path = _pdir(spool) / ".dir-id"
    token_path.write_text("not-a-token")
    spool.ensure_plugin_dirs(PLUGIN)
    minted = token_path.read_bytes()
    assert len(minted) == 32 and minted != b"not-a-token"


def test_ensure_plugin_dirs_reprobes_under_the_repair_lock(spool, monkeypatch):
    """A STALE pre-lock probe (a concurrent pass minted a valid token between
    the probe and the repair lock) must not retire that token: the repair
    re-probes under the exclusive lock and skips when it finds a valid one
    (red case for the round-2 review finding)."""
    token_path = _pdir(spool) / ".dir-id"
    before = token_path.read_bytes()
    real = cs._classify_dir_token
    calls = {"n": 0}

    def stale_once(dir_fd):
        calls["n"] += 1
        if calls["n"] == 1:
            return None                      # the stale pre-lock view
        return real(dir_fd)

    monkeypatch.setattr(cs, "_classify_dir_token", stale_once)
    spool.ensure_plugin_dirs(PLUGIN)

    assert calls["n"] >= 2, "repair must re-probe under the lock"
    assert token_path.read_bytes() == before, "valid token must survive"


def test_ensure_plugin_dirs_aborts_on_an_unknowable_token_state(spool, monkeypatch):
    """A transient I/O failure mid-probe (EIO, descriptor pressure, a short
    read) proves NOTHING about the token — the repair path must abort loudly
    rather than retire what may be a valid, live token (red case for the
    round-1 review finding: a repair keyed on the gate's collapsed None)."""
    token_path = _pdir(spool) / ".dir-id"
    before = token_path.read_bytes()

    def eio(*a, **k):
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(cs.os, "read", eio)
    with pytest.raises(OSError):
        spool.ensure_plugin_dirs(PLUGIN)
    monkeypatch.undo()

    assert token_path.read_bytes() == before, "token must survive the failure"


def test_ensure_plugin_dirs_refuses_unsafe_plugin_name(spool):
    with pytest.raises(ValueError):
        spool.ensure_plugin_dirs("../escape")


@pytest.mark.parametrize("reserved", [".index", ".hidden", ".", "..", "a/b", ""])
def test_ensure_plugin_dirs_refuses_reserved_and_dotted_names(spool, reserved):
    """A dot-prefixed spool dir would be created but then skipped by the
    plugin enumeration — never swept, recovered or GC'd."""
    with pytest.raises(ValueError):
        spool.ensure_plugin_dirs(reserved)


def test_no_plugin_scoped_operation_can_escape_the_root_with_dotdot(spool, tmp_path):
    """Red case for path traversal: every plugin-scoped entry point resolves
    its directory through the one guarded funnel, so ``..`` is refused rather
    than resolving above the pinned root."""
    outside = Path(spool.root).parent
    escape = "../" + outside.name          # non-component name
    for op in (lambda: spool.write_ready("..", {"v": 1}),
               lambda: spool.delete_ready(".."),
               lambda: spool.ensure_plugin_dirs(".."),
               lambda: spool.write_ready(escape, {"v": 1}),
               lambda: spool.delete_ready(escape)):
        with pytest.raises(ValueError):
            op()
    assert spool.claim("..", state_hash("s"), now=time.time()) is None
    assert spool.claim(escape, state_hash("s"), now=time.time()) is None

    assert not (outside / "ready.json").exists()
    assert list(outside.glob("*/ready.json")) == []


# ---------------------------------------------------------------------------
# mint (consumer contract; the reference helper lives here)
# ---------------------------------------------------------------------------


def test_mint_publishes_pending_file_0600(spool):
    p = mint(_pdir(spool), "state-one")
    assert p == _pending(spool) / f"{state_hash('state-one')}.json"
    # meta=None is STILL a v2 envelope, in the exact canonical byte form
    # (sorted keys, compact separators) casa's envelope reader re-derives.
    assert p.read_bytes() == b'{"meta":null,"v":2}'
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not list(_pending(spool).glob("*.part"))


def test_mint_writes_the_canonical_v2_envelope_with_meta(spool):
    """The pending payload is `{"v":2,"meta":<meta>}` serialized with the ONE
    canonical form (`canonical_marker_bytes`): sorted keys, compact
    separators, no ASCII-escaping — meta echoed value-preserving."""
    meta = {"kind": "renewal", "bank": "å-banken", "session_ref": "s-1"}
    p = mint(_pdir(spool), "state-meta", meta)
    assert p.read_bytes() == cs.canonical_marker_bytes({"v": 2, "meta": meta})
    assert p.read_bytes() == (
        '{"meta":{"bank":"å-banken","kind":"renewal","session_ref":"s-1"},'
        '"v":2}').encode("utf-8")


def test_mint_oversized_meta_refuses_before_any_file(spool):
    """An envelope whose canonical bytes exceed ENVELOPE_MAX_BYTES is refused
    with ValueError BEFORE any file exists: no final name, no `.part`
    residue in pending/."""
    meta = {"pad": "x" * ca.ENVELOPE_MAX_BYTES}
    with pytest.raises(ValueError):
        mint(_pdir(spool), "state-fat", meta)
    assert list(_pending(spool).iterdir()) == []


def test_mint_reuse_is_a_hard_error(spool):
    mint(_pdir(spool), "state-one")
    with pytest.raises(FileExistsError):
        mint(_pdir(spool), "state-one")
    # the loser leaves no residue behind
    assert not list(_pending(spool).glob("*.part"))


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


def test_claim_consumes_pending_and_preserves_mint_mtime(spool):
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now - 120)
    h = state_hash("s")

    claim = spool.claim(PLUGIN, h, now=now)

    assert claim is not None
    assert claim.plugin == PLUGIN and claim.state_hash == h
    assert claim.mtime == pytest.approx(now - 120, abs=0.01)
    assert not p.exists()
    assert (_claims(spool) / h).exists()
    # rename/link preserves the MINT mtime — the single clock
    assert (_claims(spool) / h).stat().st_mtime == pytest.approx(now - 120, abs=0.01)


def test_claim_of_never_minted_state_returns_none(spool):
    assert spool.claim(PLUGIN, state_hash("nope"), now=time.time()) is None


def test_claim_twice_returns_none_the_second_time(spool):
    now = time.time()
    mint(_pdir(spool), "s")
    h = state_hash("s")
    assert spool.claim(PLUGIN, h, now=now) is not None
    assert spool.claim(PLUGIN, h, now=now) is None


def test_claim_never_clobbers_an_existing_claim(spool):
    """Red case for publish-once on the CLAIM (a plain replacing rename
    survives every other race test here): a crash between the claim link and
    the pending unlink leaves BOTH names. A redirect arriving in that window
    must lose, leaving the original claim inode intact for the recovery pass
    — a replacing rename would destroy it and consume the pending twin."""
    now = time.time()
    h = state_hash("s")
    mint(_pdir(spool), "s")
    existing = _put(_claims(spool) / h, now - 60, '{"original": true}')
    ino = existing.stat().st_ino

    assert spool.claim(PLUGIN, h, now=now) is None

    assert existing.stat().st_ino == ino
    assert json.loads(existing.read_text()) == {"original": True}
    assert (_pending(spool) / f"{h}.json").exists(), "a loser consumes nothing"
    assert spool.in_flight() == set()


def test_claim_rejects_malformed_hash(spool):
    assert spool.claim(PLUGIN, "../../etc/passwd", now=time.time()) is None
    assert spool.claim(PLUGIN, "abc", now=time.time()) is None


def test_claim_unknown_plugin_returns_none(spool):
    assert spool.claim("ghost", state_hash("s"), now=time.time()) is None


def test_claim_of_expired_pending_returns_none_and_leaves_the_claim(spool):
    """Changed-contract pin (amendment 4): the request path performs no
    flow-retiring deletions. An expired state still loses (None) and its
    pending twin is consumed by the claim link, but the `.claims/<h>` entry
    is LEFT for the recovery pass to reap write-ahead — unlinking here would
    destroy the flow's last artifact with no durable record of why."""
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now - PENDING_TTL_S - 1)
    h = state_hash("s")

    assert spool.claim(PLUGIN, h, now=now) is None
    assert not p.exists()
    assert (_claims(spool) / h).exists(), "left for recovery, not unlinked"
    assert spool.in_flight() == set()


def test_claim_at_exactly_the_ttl_still_wins(spool):
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now - PENDING_TTL_S)
    assert spool.claim(PLUGIN, state_hash("s"), now=now) is not None


def test_claim_of_future_mtime_pending_fails_closed(spool):
    """A future-mtime state loses, and (amendment 4) its claim entry is
    LEFT in `.claims/` for recovery's write-ahead reap — same changed
    contract as the expired arm."""
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now + SKEW_S + 1)
    h = state_hash("s")

    assert spool.claim(PLUGIN, h, now=now) is None
    assert not p.exists()
    assert (_claims(spool) / h).exists(), "left for recovery, not unlinked"
    assert spool.in_flight() == set()


def test_claim_within_the_skew_allowance_still_wins(spool):
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now + SKEW_S)
    assert spool.claim(PLUGIN, state_hash("s"), now=now) is not None


def test_claim_adds_and_publish_clears_the_in_flight_set(spool):
    now = time.time()
    mint(_pdir(spool), "s")
    h = state_hash("s")
    claim = spool.claim(PLUGIN, h, now=now)
    assert cs.in_flight_key(PLUGIN, h) in spool.in_flight()
    spool.publish_result(claim, _record(h))
    assert spool.in_flight() == set()


def test_discard_claim_removes_it_and_clears_in_flight(spool):
    now = time.time()
    mint(_pdir(spool), "s")
    h = state_hash("s")
    claim = spool.claim(PLUGIN, h, now=now)

    spool.discard_claim(claim)

    assert list(_claims(spool).iterdir()) == []
    assert spool.in_flight() == set()


# ---------------------------------------------------------------------------
# hostile inodes at protocol names (the FD/type discipline)
# ---------------------------------------------------------------------------


def test_claim_of_a_symlinked_pending_never_captures_the_target(spool, tmp_path):
    """``link(2)`` must not follow: a symlink planted at a pending name would
    otherwise be hard-linked to its TARGET, pulling an arbitrary outside file
    into the spool as a "claimed state"."""
    secret = tmp_path / "outside-secret"
    secret.write_text("not yours")
    h = state_hash("s")
    (_pending(spool) / f"{h}.json").symlink_to(secret)

    assert spool.claim(PLUGIN, h, now=time.time()) is None

    assert secret.read_text() == "not yours"
    assert secret.stat().st_nlink == 1, "the target must not have been linked"
    # Amendment 4: the refused (symlink) claim entry is LEFT for recovery's
    # write-ahead reap; the refusal itself is what matters here.
    assert (_claims(spool) / h).is_symlink()


def test_claim_rejects_a_non_regular_pending_inode(spool):
    """A FIFO (or any non-regular inode) at a pending name is refused by the
    type gate, not merely by the symlink flag."""
    h = state_hash("s")
    os.mkfifo(_pending(spool) / f"{h}.json")

    assert spool.claim(PLUGIN, h, now=time.time()) is None

    # Amendment 4: refused, not unlinked — recovery reaps it write-ahead.
    assert stat.S_ISFIFO(os.lstat(_claims(spool) / h).st_mode)


def test_a_symlinked_plugin_dir_is_never_followed(spool, tmp_path):
    """Directory FDs are opened ``O_NOFOLLOW``: a symlink planted at a spool
    dir name must not redirect the protocol to a tree outside the root."""
    outside = tmp_path / "outside-spool"
    for sub in ("pending", "results", ".claims"):
        (outside / sub).mkdir(parents=True)
    mint(outside, "s")
    h = state_hash("s")
    (Path(spool.root) / "evil").symlink_to(outside)

    assert spool.claim("evil", h, now=time.time()) is None

    assert (outside / "pending" / f"{h}.json").exists(), "untouched"
    assert list((outside / ".claims").iterdir()) == []


def test_discard_claim_fails_closed_when_the_plugin_dir_was_replaced(spool):
    """Same identity gate as publish: after a removal + reinstall the name
    denotes a different directory, and a same-named claim there belongs to
    another flow."""
    import shutil

    h, claim = _claimed(spool)
    shutil.rmtree(_pdir(spool))
    spool.ensure_plugin_dirs(PLUGIN)
    other = _put(_claims(spool) / h, time.time(), '{"someone-else": true}')

    spool.discard_claim(claim)

    assert other.exists(), "a claim in the recreated dir is not ours to remove"


def test_discard_claim_fails_closed_when_the_replaced_dir_reuses_the_inode(spool):
    """ext4 hands a freed inode number straight back, so a recreated plugin
    dir can carry the SAME ``(st_dev, st_ino)`` as the directory the claim
    was taken from — the stat pair alone cannot prove identity (the CI
    runners' ``/tmp`` is ext4, where this reuse is the common case, not a
    fluke). Forge that worst case by grafting the recreated dir's stat pair
    onto the old claim: the gate must still refuse."""
    import dataclasses
    import shutil

    h, claim = _claimed(spool)
    shutil.rmtree(_pdir(spool))
    spool.ensure_plugin_dirs(PLUGIN)
    st = os.stat(_pdir(spool))
    reused = dataclasses.replace(claim, dir_dev=st.st_dev, dir_ino=st.st_ino)
    other = _put(_claims(spool) / h, time.time(), '{"someone-else": true}')

    spool.discard_claim(reused)

    assert other.exists(), "a claim in the recreated dir is not ours to remove"


# ---------------------------------------------------------------------------
# claim races — two threads AND two processes (INV-CB-002)
# ---------------------------------------------------------------------------


def test_claim_is_exactly_once_under_two_threads(spool):
    import threading

    now = time.time()
    hashes = [state_hash(f"t{i}") for i in range(40)]
    for i in range(40):
        mint(_pdir(spool), f"t{i}")

    won: dict[int, list[str]] = {0: [], 1: []}
    start = threading.Barrier(2)

    def worker(idx):
        start.wait()
        for h in hashes:
            if spool.claim(PLUGIN, h, now=now) is not None:
                won[idx].append(h)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allwon = won[0] + won[1]
    assert sorted(allwon) == sorted(hashes)
    assert len(allwon) == len(set(allwon))


def _claim_child(root, hashes, out_path, barrier):
    """fork child: race every hash, record the ones it won."""
    won = []
    s = CallbackSpool(root)
    now = time.time()
    barrier.wait()
    for h in hashes:
        if s.claim(PLUGIN, h, now=now) is not None:
            won.append(h)
    s.close()
    Path(out_path).write_text(json.dumps(won))
    os._exit(0)


def test_claim_is_exactly_once_under_two_processes(spool, tmp_path):
    hashes = [state_hash(f"p{i}") for i in range(60)]
    for i in range(60):
        mint(_pdir(spool), f"p{i}")

    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    outs = [tmp_path / "won0.json", tmp_path / "won1.json"]
    procs = [ctx.Process(target=_claim_child,
                         args=(str(spool.root), hashes, str(outs[i]), barrier))
             for i in (0, 1)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
        assert p.exitcode == 0

    allwon = json.loads(outs[0].read_text()) + json.loads(outs[1].read_text())
    assert sorted(allwon) == sorted(hashes), "every pending state must be claimed"
    assert len(allwon) == len(set(allwon)), "a state was claimed twice"
    assert list(_pending(spool).iterdir()) == []


# ---------------------------------------------------------------------------
# publish_result
# ---------------------------------------------------------------------------


def _claimed(spool, state="s"):
    mint(_pdir(spool), state)
    h = state_hash(state)
    return h, spool.claim(PLUGIN, h, now=time.time())


def test_publish_result_writes_intact_0600_record_and_clears_everything(spool):
    h, claim = _claimed(spool)
    rec = _record(h)

    assert spool.publish_result(claim, rec) is cs.PublishOutcome.PUBLISHED

    out = _results(spool) / f"{h}.json"
    # The record is augmented at publish (spec §4): the caller's keys are
    # preserved verbatim, plus meta (None for a v2 mint with no meta) and
    # minted_ts (the claim's preserved mint mtime). Record v stays 1.
    assert json.loads(out.read_text()) == dict(
        rec, meta=None, minted_ts=claim.mtime)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert list(_claims(spool).iterdir()) == [], "claim and temp must both be gone"


def test_publish_result_follows_the_exact_protocol_sequence(spool, fs_events):
    h, claim = _claimed(spool)
    del fs_events[:]  # only the publish sequence matters here

    assert spool.publish_result(claim, _record(h)) is cs.PublishOutcome.PUBLISHED

    i_link = _idx(fs_events, ("link", f".tmp-{h}", f"{h}.json"))
    i_tmp_fsync = _idx(fs_events, ("fsync", f".claims/.tmp-{h}"))
    i_res_fsync = _idx_after(fs_events, ("fsync", "/results"), i_link)
    i_tmp_unlink = _idx_after(fs_events, ("unlink", f".tmp-{h}"), i_link)
    i_claim_unlink = _idx(fs_events, ("unlink", h))
    claims_fsyncs = [i for i, e in enumerate(fs_events)
                     if e[0] == "fsync" and str(e[1]).endswith("/.claims")]

    # write+fsync temp -> link -> fsync results/ -> unlink temp ->
    # fsync .claims/ -> unlink claim -> fsync .claims/
    assert i_tmp_fsync < i_link < i_res_fsync < i_tmp_unlink < i_claim_unlink
    assert len([c for c in claims_fsyncs if i_tmp_unlink < c < i_claim_unlink]) >= 1
    assert len([c for c in claims_fsyncs if c > i_claim_unlink]) >= 1


def test_publish_result_when_a_result_already_exists_is_an_anomaly(spool):
    """EEXIST on the result link: a result DOES exist for the hash, so the
    outcome is PUBLISHED (the step-4 result_ready attempt is accurate and
    the flow converges through inference) — while today's cleanup and the
    never-clobber property are kept exactly."""
    h, claim = _claimed(spool)
    prior = _put(_results(spool) / f"{h}.json", time.time(), '{"prior": true}')

    assert spool.publish_result(claim, _record(h)) is cs.PublishOutcome.PUBLISHED

    assert json.loads(prior.read_text()) == {"prior": True}, "never clobbered"
    assert list(_claims(spool).iterdir()) == [], "claim and temp removed anyway"
    assert spool.in_flight() == set()
    att = json.loads((_attempts(spool) / f"{h}.json").read_text())
    assert att["status"] == "result_ready", "the step-4 attempt stands"


def test_publish_result_reclaims_a_stale_temp_left_by_a_crash(spool):
    h, claim = _claimed(spool)
    _put(_claims(spool) / f".tmp-{h}", time.time() - 5, "garbage-not-json")

    assert spool.publish_result(claim, _record(h)) is cs.PublishOutcome.PUBLISHED

    assert json.loads((_results(spool) / f"{h}.json").read_text()) == dict(
        _record(h), meta=None, minted_ts=claim.mtime)
    assert list(_claims(spool).iterdir()) == []


def test_publish_result_fails_closed_when_the_plugin_dir_was_replaced(spool):
    """A concurrent plugin removal + reinstall between claim and publish
    yields a DIFFERENT directory inode; the result must not land in it."""
    import shutil

    h, claim = _claimed(spool)
    shutil.rmtree(_pdir(spool))
    spool.ensure_plugin_dirs(PLUGIN)

    assert spool.publish_result(claim, _record(h)) \
        is cs.PublishOutcome.FAILED_UNRECORDED
    assert list(_results(spool).iterdir()) == []


def test_publish_result_fails_closed_when_the_replaced_dir_reuses_the_inode(spool):
    """Same worst case as the discard twin: a recreated dir carrying a
    recycled ``(st_dev, st_ino)``. Identity must not rest on the stat pair."""
    import dataclasses
    import shutil

    h, claim = _claimed(spool)
    shutil.rmtree(_pdir(spool))
    spool.ensure_plugin_dirs(PLUGIN)
    st = os.stat(_pdir(spool))
    reused = dataclasses.replace(claim, dir_dev=st.st_dev, dir_ino=st.st_ino)

    assert spool.publish_result(reused, _record(h)) \
        is cs.PublishOutcome.FAILED_UNRECORDED
    assert list(_results(spool).iterdir()) == []


def test_publish_result_write_failure_leaves_the_claim_for_recovery(spool, monkeypatch):
    """A transient write failure must not silently eat the flow: the claim
    stays, the in-flight entry does NOT (or recovery would skip that claim for
    the rest of the process's life), and the next boot pass restores it.

    The boom here is UNCONDITIONAL, so it fells the step-4 attempt write
    (its staged-replace goes through ``_write_new_file`` too) before any
    result-side work: that is the FAILED_UNRECORDED arm — nothing durable
    exists, so the caller must leave the claim."""
    h, claim = _claimed(spool)

    def boom(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cs, "_write_new_file", boom)
    assert spool.publish_result(claim, _record(h)) \
        is cs.PublishOutcome.FAILED_UNRECORDED
    monkeypatch.undo()

    assert (_claims(spool) / h).exists()
    assert spool.in_flight() == set()
    assert list(_results(spool).iterdir()) == []
    assert not (_claims(spool) / f".tmp-{h}").exists()
    assert spool.read_attempt(PLUGIN, h).state is cs.MarkerState.ABSENT

    report = spool.recovery_pass(now=time.time(), boot=True)
    assert report.restored == [(PLUGIN, h)]


def test_publish_result_records_an_unserializable_record_as_publish_failed(spool):
    """Amendment 2: an unserializable record goes through the same strict
    ``done/publish_failed`` recording as a staging failure — FAILED_RECORDED,
    with the meta still extracted from the claim envelope. The claim is LEFT
    (the HANDLER owns the discard on this outcome)."""
    mint(_pdir(spool), "s", meta={"flow": "authz"})
    h = state_hash("s")
    claim = spool.claim(PLUGIN, h, now=time.time())

    assert spool.publish_result(claim, {"bad": {1, 2}}) \
        is cs.PublishOutcome.FAILED_RECORDED

    att = json.loads((_attempts(spool) / f"{h}.json").read_text())
    assert (att["status"], att["outcome"]) == ("done", "publish_failed")
    assert att["meta"] == {"flow": "authz"}, "meta still extracted"
    assert att["minted_ts"] == claim.mtime
    assert (_claims(spool) / h).exists(), "claim LEFT — the handler decides"
    assert list(_results(spool).iterdir()) == []
    assert spool.in_flight() == set()


def test_publish_result_writes_the_attempt_before_the_result_link(spool, monkeypatch):
    """The spec §3.1 ordering pin (attempt-first publish): the instant the
    result name becomes visible a consumer may collect and ack, so the
    ``result_ready`` attempt file must already exist when the result link is
    attempted — never after. The spy wraps ``_link_once`` only AFTER the
    claim, so the one link it sees is the publish-time one."""
    meta = {"flow": "authz", "n": 1}
    p = mint(_pdir(spool), "s", meta=meta)
    mint_mtime = p.stat().st_mtime
    h = state_hash("s")
    claim = spool.claim(PLUGIN, h, now=time.time())
    assert claim is not None

    real = cs._link_once
    seen: dict[str, bool] = {}

    def spy(src, src_dir_fd, dst, dst_dir_fd):
        seen["attempt_exists"] = (_attempts(spool) / f"{h}.json").is_file()
        return real(src, src_dir_fd, dst, dst_dir_fd)

    monkeypatch.setattr(cs, "_link_once", spy)

    assert spool.publish_result(claim, _record(h)) is cs.PublishOutcome.PUBLISHED

    assert seen["attempt_exists"] is True, \
        "the attempt must be visible BEFORE the result name can exist"
    att = json.loads((_attempts(spool) / f"{h}.json").read_text())
    assert att["status"] == "result_ready"
    assert att["meta"] == meta
    assert att["minted_ts"] == claim.mtime == mint_mtime, \
        "minted_ts is the MINT clock, preserved through the claim"
    rec = json.loads((_results(spool) / f"{h}.json").read_text())
    assert rec["meta"] == meta
    assert rec["minted_ts"] == claim.mtime
    assert rec["v"] == 1, "the augmentation is additive; record v stays 1"


def test_publish_result_absent_claim_is_failed_unrecorded_and_writes_nothing(spool):
    """Step-1 presence probe: a genuinely ABSENT claim means an ack-teardown
    (or a concurrent path) consumed the flow underneath the handler — casa
    must write NOTHING (no attempt, no result) or the teardown would be
    un-torn, and report FAILED_UNRECORDED."""
    h, claim = _claimed(spool)
    (_claims(spool) / h).unlink()

    assert spool.publish_result(claim, _record(h)) \
        is cs.PublishOutcome.FAILED_UNRECORDED

    assert spool.read_attempt(PLUGIN, h).state is cs.MarkerState.ABSENT
    assert list(_results(spool).iterdir()) == []
    assert spool.in_flight() == set()


def test_publish_result_staging_failure_is_recorded_publish_failed(spool, monkeypatch):
    """A staging failure AFTER the step-4 attempt write goes through the
    strict ``done/publish_failed`` rewrite ⇒ FAILED_RECORDED. The claim is
    LEFT by the spool (the HANDLER owns the discard on this outcome), and no
    result nor temp survives. The boom is targeted at the ``.claims/.tmp-``
    temp name only, so the attempt writes (a ``.<name>.tmp-…`` staging
    grammar) are untouched."""
    h, claim = _claimed(spool)
    real = cs._write_new_file

    def boom(name, dir_fd, data):
        if name.startswith(cs.TEMP_PREFIX):
            raise OSError(errno.EIO, "injected staging fault")
        return real(name, dir_fd, data)

    monkeypatch.setattr(cs, "_write_new_file", boom)

    assert spool.publish_result(claim, _record(h)) \
        is cs.PublishOutcome.FAILED_RECORDED

    att = json.loads((_attempts(spool) / f"{h}.json").read_text())
    assert (att["status"], att["outcome"]) == ("done", "publish_failed")
    assert att["minted_ts"] == claim.mtime
    assert (_claims(spool) / h).exists(), "claim LEFT — the handler decides"
    assert list(_results(spool).iterdir()) == []
    assert not (_claims(spool) / f".tmp-{h}").exists()
    assert spool.in_flight() == set()


def test_publish_result_staging_and_strict_rewrite_failure_is_unrecorded(
        spool, monkeypatch):
    """Amendment 3: when the strict ``publish_failed`` rewrite ALSO fails,
    nothing is proven durable ⇒ FAILED_UNRECORDED and the claim survives for
    recovery. The attempt file may legitimately read ``result_ready`` OR
    ``publish_failed`` (visibility without proven durability), so only the
    claim's survival and the result's absence are asserted — never the
    record's absence."""
    h, claim = _claimed(spool)
    real = cs._write_new_file

    def boom(name, dir_fd, data):
        if name.startswith(cs.TEMP_PREFIX):
            raise OSError(errno.EIO, "injected staging fault")
        return real(name, dir_fd, data)

    def fsync_boom(fd, what):
        raise cs.FsyncFailed(errno.EIO, "injected fsync fault")

    monkeypatch.setattr(cs, "_write_new_file", boom)
    monkeypatch.setattr(cs, "_fsync_strict", fsync_boom)

    assert spool.publish_result(claim, _record(h)) \
        is cs.PublishOutcome.FAILED_UNRECORDED

    assert (_claims(spool) / h).exists(), "claim LEFT for recovery"
    assert list(_results(spool).iterdir()) == []
    assert spool.in_flight() == set()


def test_publish_result_malformed_envelope_degrades_to_meta_none(spool):
    """A claim whose inode carries a garbage envelope (a hand-written
    pending) still publishes — the state was already consumed, refusal buys
    nothing — with ``meta`` None in both the attempt and the record."""
    h = state_hash("s")
    (_pending(spool) / f"{h}.json").write_bytes(b"garbage, not json")
    claim = spool.claim(PLUGIN, h, now=time.time())
    assert claim is not None

    assert spool.publish_result(claim, _record(h)) is cs.PublishOutcome.PUBLISHED

    att = json.loads((_attempts(spool) / f"{h}.json").read_text())
    assert att["meta"] is None
    assert json.loads(
        (_results(spool) / f"{h}.json").read_text())["meta"] is None


def test_publish_result_oversized_envelope_degrades_to_meta_none(spool):
    """The envelope read is bounded to ENVELOPE_MAX_BYTES + 1: an oversized
    (hand-written — mint() refuses one) envelope is never read whole and
    degrades to ``meta`` None, publishing normally."""
    h = state_hash("s")
    big = json.dumps({"v": 2, "meta": "x" * (ca.ENVELOPE_MAX_BYTES + 64)})
    (_pending(spool) / f"{h}.json").write_text(big)
    claim = spool.claim(PLUGIN, h, now=time.time())
    assert claim is not None

    assert spool.publish_result(claim, _record(h)) is cs.PublishOutcome.PUBLISHED

    att = json.loads((_attempts(spool) / f"{h}.json").read_text())
    assert att["meta"] is None


def test_read_side_helpers(spool):
    h, claim = _claimed(spool)
    assert spool.list_results(PLUGIN) == []
    assert spool.has_result(PLUGIN, h) is False

    spool.publish_result(claim, _record(h))

    assert spool.list_results(PLUGIN) == [h]
    assert spool.has_result(PLUGIN, h) is True
    assert spool.has_result("ghost", h) is False
    assert spool.has_result(PLUGIN, "not-a-hash") is False
    assert spool.plugins() == [PLUGIN]


def test_result_mtime_is_its_final_write_time_not_the_mint_time(spool):
    """Each TTL runs off its own file's mtime: a long
    authorization flow must not expire its result on arrival."""
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now - PENDING_TTL_S + 60)      # minted 29 minutes ago
    h = state_hash("s")
    claim = spool.claim(PLUGIN, h, now=now)

    assert spool.publish_result(claim, _record(h)) is cs.PublishOutcome.PUBLISHED

    res_mtime = (_results(spool) / f"{h}.json").stat().st_mtime
    assert res_mtime == pytest.approx(time.time(), abs=5)
    assert res_mtime - claim.mtime > 60


# ---------------------------------------------------------------------------
# partial non-exposure — a collector racing the publisher (multiprocess)
# ---------------------------------------------------------------------------

_PAD = "x" * 8_000


def _whole_record(raw: bytes, h: str) -> bool:
    """True iff *raw* is the COMPLETE published record: it parses (a
    truncated JSON body cannot), and carries the exact payload the publisher
    wrote plus the publish-time augmentation (``meta``/``minted_ts``, whose
    minted_ts value the reader cannot know in advance)."""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return False
    return (isinstance(obj, dict)
            and obj.get("v") == 1 and obj.get("h") == h
            and obj.get("pad") == _PAD and obj.get("meta") is None
            and isinstance(obj.get("minted_ts"), float))


def _publisher_child(root, states):
    s = CallbackSpool(root)
    for st in states:
        mint(Path(root) / PLUGIN, st)
        h = state_hash(st)
        claim = s.claim(PLUGIN, h, now=time.time())
        s.publish_result(claim, {"v": 1, "h": h, "pad": _PAD})
    s.close()
    os._exit(0)


def _reader_child(root, states, out_path):
    """Spin on the NEXT expected result name — so every observation lands in
    the publisher's write window for that exact file — and require the very
    first successful read to be the complete record. Any short, empty or
    half-written observation is a violation: a name may only appear once its
    content is already whole."""
    resdir = Path(root) / PLUGIN / "results"
    bad, reads, verified = [], 0, 0
    for st in states:
        h = state_hash(st)
        path = resdir / f"{h}.json"
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                raw = path.read_bytes()
            except OSError:
                continue                     # not published yet — keep spinning
            reads += 1
            if _whole_record(raw, h):
                verified += 1
            else:
                bad.append(f"{h[:8]}: partial observation ({len(raw)} bytes)")
            break
        else:
            bad.append(f"{h[:8]}: never appeared")
    Path(out_path).write_text(json.dumps({"bad": bad, "reads": reads,
                                          "verified": verified}))
    os._exit(0)


def test_a_racing_collector_never_observes_a_partial_result(spool, tmp_path):
    states = [f"race{i}" for i in range(200)]
    out = tmp_path / "reader.json"

    ctx = multiprocessing.get_context("fork")
    reader = ctx.Process(target=_reader_child,
                         args=(str(spool.root), states, str(out)))
    writer = ctx.Process(target=_publisher_child, args=(str(spool.root), states))
    reader.start()
    writer.start()
    writer.join(60)
    reader.join(60)
    assert writer.exitcode == 0 and reader.exitcode == 0

    report = json.loads(out.read_text())
    assert report["bad"] == []
    assert report["verified"] == len(states)
    assert report["reads"] == len(states)


# ---------------------------------------------------------------------------
# recovery_pass
# ---------------------------------------------------------------------------


def test_boot_recovery_restores_a_young_unresulted_claim_with_its_mtime(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - 300)

    report = spool.recovery_pass(now=now, boot=True)

    restored = _pending(spool) / f"{h}.json"
    assert restored.exists()
    assert restored.stat().st_mtime == pytest.approx(now - 300, abs=0.01)
    assert list(_claims(spool).iterdir()) == []
    assert report.restored == [(PLUGIN, h)]


def test_boot_recovery_unlinks_an_orphan_temp_before_restoring_its_claim(
        spool, fs_events):
    """Crash after the temp write, before the publish: the deterministic
    ``.tmp-<hash>`` name must be free again before the claim goes back to
    pending, or the retry finds the name occupied (spec r8 fold)."""
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - 300)
    _put(_claims(spool) / f".tmp-{h}", now - 300, "half-written")
    del fs_events[:]

    report = spool.recovery_pass(now=now, boot=True)

    assert not (_claims(spool) / f".tmp-{h}").exists()
    assert (_pending(spool) / f"{h}.json").exists()
    assert report.temps_cleared == 1
    i_unlink_tmp = _idx(fs_events, ("unlink", f".tmp-{h}"))
    i_restore = _idx(fs_events, ("link", h, f"{h}.json"))
    assert i_unlink_tmp < i_restore
    # the temp's removal is durable before the claim is republished
    assert any(e[0] == "fsync" and str(e[1]).endswith("/.claims")
               for e in fs_events[i_unlink_tmp:i_restore])


def test_boot_recovery_reports_a_claim_with_a_result_for_nudge_then_removes_it(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - 300)
    _put(_results(spool) / f"{h}.json", now - 10)

    report = spool.recovery_pass(now=now, boot=True)

    assert report.nudges == [(PLUGIN, h)]
    assert report.restored == []
    assert list(_claims(spool).iterdir()) == []
    assert not (_pending(spool) / f"{h}.json").exists(), "never re-mint a done flow"


def test_recovery_nudges_a_stale_claim_whose_result_was_already_published(spool):
    """A published result outranks the claim's age. The claim's mtime is the
    MINT time, so a slow authorization (minted 31 min ago, result written
    seconds before the crash) would be "stale" by that clock — dropping it
    loses the nudge while the credential sits live in results/ for its own
    15-minute TTL."""
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - PENDING_TTL_S - 60)
    result = _put(_results(spool) / f"{h}.json", now - 300)

    report = spool.recovery_pass(now=now, boot=True)

    assert report.nudges == [(PLUGIN, h)]
    assert report.dropped == []
    assert result.exists(), "the live result must not be stranded"
    assert list(_claims(spool).iterdir()) == []


def test_recovery_ignores_a_non_regular_result_when_deciding_a_nudge(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - 300)
    os.mkfifo(_results(spool) / f"{h}.json")

    report = spool.recovery_pass(now=now, boot=True)

    assert report.nudges == []
    assert report.restored == [(PLUGIN, h)], "no real result ⇒ restore the flow"


def test_recovery_drops_a_stale_claim(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - PENDING_TTL_S - 1)

    report = spool.recovery_pass(now=now, boot=True)

    assert list(_claims(spool).iterdir()) == []
    assert not (_pending(spool) / f"{h}.json").exists()
    assert report.dropped == [(PLUGIN, h)]


def test_recovery_drops_a_future_mtime_claim(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now + SKEW_S + 1)

    spool.recovery_pass(now=now, boot=True)

    assert list(_claims(spool).iterdir()) == []
    assert not (_pending(spool) / f"{h}.json").exists()


def test_recovery_removes_unparseable_claims_dir_residue(spool):
    now = time.time()
    _put(_claims(spool) / "not-a-hash", now - 10)

    report = spool.recovery_pass(now=now, boot=True)

    assert list(_claims(spool).iterdir()) == []
    assert report.anomalies


def test_periodic_recovery_skips_an_in_flight_claim_that_boot_would_restore(spool):
    now = time.time()
    p = mint(_pdir(spool), "s")
    _utime(p, now - 300)
    h = state_hash("s")
    spool.claim(PLUGIN, h, now=now)          # live handler: hash is in flight

    spool.recovery_pass(now=now, boot=False)
    assert (_claims(spool) / h).exists(), "a live claim must not be restored"
    assert not (_pending(spool) / f"{h}.json").exists()

    report = spool.recovery_pass(now=now, boot=True)
    assert report.restored == [(PLUGIN, h)]


def test_periodic_recovery_skips_an_in_flight_orphan_temp(spool):
    now = time.time()
    mint(_pdir(spool), "s")
    h = state_hash("s")
    spool.claim(PLUGIN, h, now=now)
    tmp = _put(_claims(spool) / f".tmp-{h}", now, "being-written")

    spool.recovery_pass(now=now, boot=False)

    assert tmp.exists(), "a live writer's temp must not be unlinked under it"


def test_periodic_recovery_skips_a_claim_younger_than_the_restore_grace(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - (RESTORE_GRACE_S - 10))

    spool.recovery_pass(now=now, boot=False)
    assert (_claims(spool) / h).exists()

    report = spool.recovery_pass(now=now, boot=True)
    assert report.restored == [(PLUGIN, h)]


def test_recovery_completes_the_deletion_a_durable_outcome_authorized(
        spool, monkeypatch):
    """Red case (Sol 1): a staging failure records ``done/publish_failed``
    STRICTLY and returns FAILED_RECORDED — the handler may then die before
    its discard. Boot recovery must NOT link that claim back into
    ``pending/``: casa already recorded this flow's end, so the deletion was
    authorized and only interrupted. Restoring re-mints a consumed state and
    reopens a terminal attempt (INV-CB-002)."""
    now = time.time()
    h, claim = _claimed(spool, "failed-publish")
    real = cs._write_new_file

    def boom(name, dir_fd, data):
        if name.startswith(cs.TEMP_PREFIX):
            raise OSError(errno.EIO, "injected staging fault")
        return real(name, dir_fd, data)

    monkeypatch.setattr(cs, "_write_new_file", boom)
    assert spool.publish_result(claim, _record(h)) \
        is cs.PublishOutcome.FAILED_RECORDED
    monkeypatch.undo()
    assert (_claims(spool) / h).exists(), "the crash: no discard ran"

    report = spool.recovery_pass(now=now, boot=True)

    assert not (_pending(spool) / f"{h}.json").exists(), \
        "a flow whose END is on record is never re-minted"
    assert list(_claims(spool).iterdir()) == []
    assert report.restored == [] and report.dropped == []
    assert report.completed_terminal == [(PLUGIN, h)]
    rec = _attempt_of(spool, h)
    assert (rec["status"], rec["outcome"]) == ("done", "publish_failed"), \
        "the recorded outcome is untouched — recovery only finished the job"


def test_recovery_still_restores_a_claim_whose_attempt_is_open(spool):
    """The converse of the arm above: only a DONE record completes the
    deletion. An open record (the ordinary crash between claim and publish)
    still restores the flow — a stricter rule would eat live authorizations."""
    now = time.time()
    h = state_hash("open-attempt")
    _put(_claims(spool) / h, now - 300)
    assert spool.write_attempt(PLUGIN, h, ca.new_attempt(
        state_hash=h, minted_ts=now - 300, status="awaiting_redirect",
        now=now)) is True

    report = spool.recovery_pass(now=now, boot=True)

    assert (_pending(spool) / f"{h}.json").exists()
    assert report.restored == [(PLUGIN, h)]
    assert report.completed_terminal == []


def test_recovery_restore_does_not_clobber_a_republished_pending(spool):
    now = time.time()
    h = state_hash("s")
    _put(_claims(spool) / h, now - 300)
    live = _put(_pending(spool) / f"{h}.json", now, '{"live": true}')

    spool.recovery_pass(now=now, boot=True)

    assert json.loads(live.read_text()) == {"live": True}
    assert list(_claims(spool).iterdir()) == []


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def test_sweep_deletes_expired_and_future_entries_in_all_name_classes(spool):
    now = time.time()
    old_pending = _put(_pending(spool) / f"{state_hash('a')}.json", now - PENDING_TTL_S - 1)
    fut_pending = _put(_pending(spool) / f"{state_hash('b')}.json", now + SKEW_S + 1)
    old_part = _put(_pending(spool) / f"{state_hash('c')}.json.part", now - TEMP_TTL_S - 1)
    old_result = _put(_results(spool) / f"{state_hash('d')}.json", now - RESULT_TTL_S - 1)
    fut_result = _put(_results(spool) / f"{state_hash('e')}.json", now + SKEW_S + 1)
    old_collect = _put(_results(spool) / f".collect-{state_hash('f')}-abcd",
                       now - RESULT_TTL_S - 1)
    old_tmp = _put(_claims(spool) / f".tmp-{state_hash('g')}", now - TEMP_TTL_S - 1)

    report = spool.sweep(now=now)

    for p in (old_pending, fut_pending, old_part, old_result, fut_result,
              old_collect, old_tmp):
        assert not p.exists(), f"{p.name} should have been swept"
    assert report.total == 7


def test_sweep_keeps_everything_still_inside_its_own_ttl(spool):
    now = time.time()
    keep = [
        _put(_pending(spool) / f"{state_hash('a')}.json", now - PENDING_TTL_S + 60),
        _put(_pending(spool) / f"{state_hash('c')}.json.part", now - 10),
        _put(_results(spool) / f"{state_hash('d')}.json", now - RESULT_TTL_S + 60),
        _put(_results(spool) / f".collect-{state_hash('f')}-abcd", now - 30),
        _put(_claims(spool) / f".tmp-{state_hash('g')}", now - 10),
    ]

    report = spool.sweep(now=now)

    assert all(p.exists() for p in keep)
    assert report.total == 0


def test_sweep_does_not_touch_bare_claims(spool):
    """Claims are the recovery pass's business — a sweep that deleted a young
    claim would silently eat an in-flight authorization."""
    now = time.time()
    # Older than the temp/`.part` TTL (so a class confusion in the sweep WOULD
    # delete it) but still well inside the pending TTL, i.e. exactly the claim
    # the recovery pass must be allowed to restore.
    claim = _put(_claims(spool) / state_hash("s"), now - (TEMP_TTL_S + 600))

    spool.sweep(now=now)

    assert claim.exists()


def test_sweep_removes_unparseable_and_non_regular_entries(spool):
    now = time.time()
    junk = _put(_pending(spool) / "not-a-hash.json", now)
    subdir = _pending(spool) / "a-directory"
    subdir.mkdir()

    report = spool.sweep(now=now)

    assert not junk.exists()
    assert not subdir.exists()
    assert len(report.anomalies) == 2


def test_sweep_caps_pending_at_256_oldest_first(spool):
    now = time.time()
    made = []
    for i in range(300):
        made.append(_put(_pending(spool) / f"{state_hash(str(i))}.json", now - 300 + i))

    report = spool.sweep(now=now)

    left = sorted(p.name for p in _pending(spool).iterdir())
    assert len(left) == 256
    survivors = {p.name for p in made[-256:]}
    assert set(left) == survivors, "the 44 OLDEST must be the ones deleted"
    assert report.capped == [f"{PLUGIN}/pending"]


def test_sweep_counts_part_files_toward_the_pending_cap(spool):
    """A consumer looping on a failing publish leaves `.part` files in the same
    directory; a cap that ignored them would never bound /data."""
    now = time.time()
    for i in range(150):
        _put(_pending(spool) / f"{state_hash(str(i))}.json", now - 100 + i)
    for i in range(150):
        _put(_pending(spool) / f"{state_hash('p' + str(i))}.json.part", now - 60)

    report = spool.sweep(now=now)

    assert len(list(_pending(spool).iterdir())) == 256
    assert report.capped == [f"{PLUGIN}/pending"]


def test_sweep_removes_stale_ready_and_index_staging_residue(spool):
    """`_replace_json` residue from a crash between staging and the rename is
    swept nowhere else — it would otherwise live forever."""
    now = time.time()
    spool.write_index_entry("/artifacts/acme", {"plugin": PLUGIN})
    ready_tmp = _put(_pdir(spool) / ".ready.json.tmp-99-deadbeef", now - TEMP_TTL_S - 1)
    index_tmp = _put(Path(spool.root) / ".index" / ".k.json.tmp-99-deadbeef",
                     now - TEMP_TTL_S - 1)
    fresh = _put(_pdir(spool) / ".ready.json.tmp-99-cafe", now - 5)
    spool.write_ready(PLUGIN, {"v": 1})
    entry = Path(spool.root) / ".index" / f"{index_key('/artifacts/acme')}.json"

    report = spool.sweep(now=now)

    assert not ready_tmp.exists() and not index_tmp.exists()
    assert fresh.exists(), "a temp inside its age window belongs to a live writer"
    assert (_pdir(spool) / "ready.json").exists(), "never the published marker"
    assert entry.exists(), "never a published index entry"
    assert report.deleted_temps == 2


def test_sweep_caps_results_at_256_oldest_first(spool):
    now = time.time()
    for i in range(260):
        _put(_results(spool) / f"{state_hash(str(i))}.json", now - 300 + i)

    report = spool.sweep(now=now)

    assert len(list(_results(spool).iterdir())) == 256
    assert report.capped == [f"{PLUGIN}/results"]


# ---------------------------------------------------------------------------
# gc_orphan_dirs (gated GC)
# ---------------------------------------------------------------------------


def _quiesce(path: Path, when: float) -> None:
    """Age a whole spool dir tree (deepest first, so a parent's mtime is not
    bumped by a child's utime)."""
    for p in sorted(path.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        _utime(p, when)
    _utime(path, when)


DAY = 24 * 3600


def test_gc_is_a_noop_when_the_registry_did_not_load_valid(spool):
    """Red case: an unreadable registry must never vaporize spool dirs — a
    membership set built from a failed load would delete EVERY plugin's
    in-flight authorizations."""
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)
    _quiesce(_pdir(spool), now - 5 * DAY)

    assert spool.gc_orphan_dirs(registry_valid=False, member_plugins=set(), now=now) == []

    assert _pdir(spool, "ghost").is_dir()
    assert _pdir(spool).is_dir()


def test_gc_removes_a_quiescent_orphan_dir(spool):
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)

    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins={PLUGIN},
                                   now=now)

    assert removed == ["ghost"]
    assert not _pdir(spool, "ghost").exists()
    assert _pdir(spool).is_dir(), "a registry member is never touched"


def test_gc_skips_a_dir_that_was_active_within_24h(spool):
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    _quiesce(_pdir(spool, "ghost"), now - 3 * 3600)

    assert spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                now=now) == []
    assert _pdir(spool, "ghost").is_dir()


def test_gc_skips_a_dir_holding_an_entry_younger_than_the_pending_ttl(spool):
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)
    _put(_pdir(spool, "ghost") / "results" / f"{state_hash('x')}.json", now - 60)
    _utime(_pdir(spool, "ghost") / "results", now - 5 * DAY)
    _utime(_pdir(spool, "ghost"), now - 5 * DAY)

    assert spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                now=now) == []
    assert _pdir(spool, "ghost").is_dir()


def test_gc_never_touches_the_index_dir(spool):
    now = time.time()
    spool.write_index_entry("/opt/artifacts/acme", {"plugin": PLUGIN})
    _quiesce(Path(spool.root) / ".index", now - 5 * DAY)

    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                   now=now)

    assert ".index" not in removed
    assert (Path(spool.root) / ".index").is_dir()


# ---------------------------------------------------------------------------
# ready.json / .index ordering helpers
# ---------------------------------------------------------------------------


def test_write_ready_publishes_atomically_and_fsyncs_the_dir(spool, fs_events):
    payload = {"v": 1, "base_url": "https://x.example",
               "callbacks": {"oauth": {"effective": "plg-acme--oauth"}}}

    spool.write_ready(PLUGIN, payload)

    ready = _pdir(spool) / "ready.json"
    assert json.loads(ready.read_text()) == payload
    assert stat.S_IMODE(ready.stat().st_mode) == 0o600
    assert not [p for p in _pdir(spool).iterdir() if p.name.startswith(".ready")]
    assert any(e[0] == "fsync" and str(e[1]).endswith(f"/{PLUGIN}")
               for e in fs_events), "the marker's directory entry must be durable"


def test_write_ready_replaces_a_previous_marker(spool):
    spool.write_ready(PLUGIN, {"v": 1, "callbacks": {}})
    spool.write_ready(PLUGIN, {"v": 1, "callbacks": {"oauth": {}}})
    assert json.loads((_pdir(spool) / "ready.json").read_text())["callbacks"]


def test_delete_ready_removes_and_fsyncs_before_the_overlay_swap(spool, fs_events):
    spool.write_ready(PLUGIN, {"v": 1})
    del fs_events[:]

    spool.delete_ready(PLUGIN)

    assert not (_pdir(spool) / "ready.json").exists()
    i_unlink = _idx(fs_events, ("unlink", "ready.json"))
    assert any(e[0] == "fsync" and str(e[1]).endswith(f"/{PLUGIN}")
               for e in fs_events[i_unlink:]), "unrouting fsyncs the deletion"


def test_delete_ready_is_idempotent(spool):
    spool.delete_ready(PLUGIN)
    spool.delete_ready("never-existed")


def test_index_entry_roundtrip_and_delete(spool, fs_events):
    payload = {"plugin": PLUGIN, "ready": {"v": 1}}
    art = "/opt/artifacts/acme"

    spool.write_index_entry(art, payload)

    entry = Path(spool.root) / ".index" / f"{index_key(art)}.json"
    assert json.loads(entry.read_text()) == payload
    assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    assert any(e[0] == "fsync" and str(e[1]).endswith("/.index") for e in fs_events)

    del fs_events[:]
    spool.delete_index_entry(art)
    assert not entry.exists()
    assert any(e[0] == "fsync" and str(e[1]).endswith("/.index") for e in fs_events)


def test_creating_the_index_dir_fsyncs_the_root(spool, fs_events):
    """The directory entry must be durable before an entry inside it is —
    otherwise a power crash can keep the entry's inode and lose the directory
    that names it."""
    root = os.path.realpath(spool.root)

    spool.write_index_entry("/artifacts/acme", {"plugin": PLUGIN})

    assert any(e[0] == "fsync" and str(e[1]) == root for e in fs_events)


def test_delete_index_entry_is_idempotent(spool):
    spool.delete_index_entry("/nowhere")


def test_index_key_resolves_symlinks(tmp_path):
    real = tmp_path / "real-artifact"
    real.mkdir()
    link = tmp_path / "linked-artifact"
    link.symlink_to(real)

    assert index_key(str(link)) == index_key(str(real))
    assert index_key(str(real)) != index_key(str(tmp_path / "other"))
    assert len(index_key(str(real))) == 64


def test_state_hash_is_sha256_hex():
    import hashlib
    assert state_hash("abc") == hashlib.sha256(b"abc").hexdigest()


# ---------------------------------------------------------------------------
# closed instance fails closed (never a dir_fd=None, CWD-relative syscall)
# ---------------------------------------------------------------------------


def test_operations_do_not_leak_file_descriptors(spool):
    """Every path here opens directory FDs by hand; one missed ``close`` on an
    error branch would exhaust the process's FDs over an uptime."""
    def cycle(i):
        now = time.time()
        mint(_pdir(spool), f"fd{i}")
        h = state_hash(f"fd{i}")
        claim = spool.claim(PLUGIN, h, now=now)
        spool.publish_result(claim, _record(h))
        spool.write_ready(PLUGIN, {"v": 1})
        spool.write_index_entry(f"/artifacts/{i}", {"plugin": PLUGIN})
        spool.delete_index_entry(f"/artifacts/{i}")
        spool.delete_ready(PLUGIN)
        spool.sweep(now=now)
        spool.recovery_pass(now=now, boot=True)
        spool.gc_orphan_dirs(registry_valid=True, member_plugins={PLUGIN}, now=now)
        spool.claim("ghost", h, now=now)          # error branches too
        spool.has_result(PLUGIN, h)
        spool.list_results(PLUGIN)

    cycle(0)                                       # warm-up (lazy imports etc.)
    before = len(os.listdir("/proc/self/fd"))
    for i in range(1, 25):
        cycle(i)
    assert len(os.listdir("/proc/self/fd")) <= before


def test_a_closed_spool_refuses_every_operation(tmp_path):
    s = CallbackSpool(tmp_path / "callbacks")
    s.ensure_plugin_dirs(PLUGIN)
    s.close()
    s.close()  # idempotent

    assert s.claim(PLUGIN, state_hash("s"), now=time.time()) is None
    with pytest.raises(cs.SpoolClosed):
        s.ensure_plugin_dirs(PLUGIN)
    with pytest.raises(cs.SpoolClosed):
        s.write_ready(PLUGIN, {"v": 1})


# ---------------------------------------------------------------------------
# durable published-marker inventory (published_plugins / index_keys /
# delete_index_key) — the reconciler's on-disk truth
# ---------------------------------------------------------------------------


def test_published_plugins_lists_only_dirs_with_a_ready_marker(spool):
    spool.ensure_plugin_dirs("other")
    assert spool.published_plugins() == []          # dirs exist, no markers yet
    spool.write_ready(PLUGIN, {"v": 1})
    assert spool.published_plugins() == [PLUGIN]     # only the marked dir


def test_index_keys_lists_published_keys_only(spool, tmp_path):
    art = tmp_path / "store" / "acme" / "art-1"
    art.mkdir(parents=True)
    assert spool.index_keys() == []
    spool.write_index_entry(str(art), {"v": 1})
    key = index_key(str(art))
    assert spool.index_keys() == [key]


def test_delete_index_key_retires_one_entry(spool, tmp_path):
    art = tmp_path / "store" / "acme" / "art-1"
    art.mkdir(parents=True)
    spool.write_index_entry(str(art), {"v": 1})
    key = index_key(str(art))
    entry = Path(spool.root) / ".index" / f"{key}.json"
    assert entry.is_file()

    spool.delete_index_key(key)
    assert not entry.exists()
    assert spool.index_keys() == []
    spool.delete_index_key(key)                      # idempotent — no raise


def test_delete_index_key_refuses_a_non_hash_key(spool):
    with pytest.raises(ValueError):
        spool.delete_index_key("../escape")


def test_durable_inventory_methods_are_empty_on_a_closed_spool(tmp_path):
    s = CallbackSpool(tmp_path / "callbacks")
    s.ensure_plugin_dirs(PLUGIN)
    s.write_ready(PLUGIN, {"v": 1})
    s.close()
    assert s.published_plugins() == []
    assert s.index_keys() == []
    with pytest.raises(cs.SpoolClosed):
        s.delete_index_key("0" * 64)


# ---------------------------------------------------------------------------
# three-state marker reader (read_marker / read_index_marker) — the durable
# on-disk truth the reconciler drives its paired transaction from. ABSENT and
# INVALID are DISTINCT: a stale-but-unreadable marker is republished, never
# mistaken for absent.
# ---------------------------------------------------------------------------


def test_read_marker_absent_when_no_file(spool):
    m = spool.read_marker(PLUGIN)
    assert m.state is cs.MarkerState.ABSENT and m.payload is None
    # A plugin dir that does not exist at all is ABSENT, not INVALID.
    assert spool.read_marker("never-made").state is cs.MarkerState.ABSENT


def test_read_marker_present_returns_payload(spool):
    payload = {"v": 1, "base_url": "https://x.example", "callbacks": {}}
    spool.write_ready(PLUGIN, payload)
    m = spool.read_marker(PLUGIN)
    assert m.state is cs.MarkerState.PRESENT
    assert m.payload == payload


def test_canonical_marker_bytes_is_sorted_compact_utf8():
    """The shared canonical form: sorted keys, most-compact separators, no
    ASCII-escaping, UTF-8 encoded."""
    b = cs.canonical_marker_bytes({"b": 1, "a": "é"})
    assert b == '{"a":"é","b":1}'.encode("utf-8")


def test_written_marker_is_byte_identical_to_canonical(spool):
    """Writer and compare share the helper: a marker casa writes is byte-for-byte
    canonical_marker_bytes(payload), and the reader exposes those exact raw bytes
    — so a second immediate pass sees it unchanged (no churn)."""
    payload = {"v": 1, "base_url": "https://x.example", "callbacks": {}}
    spool.write_ready(PLUGIN, payload)
    on_disk = (_pdir(spool) / "ready.json").read_bytes()
    assert on_disk == cs.canonical_marker_bytes(payload)
    m = spool.read_marker(PLUGIN)
    assert m.raw == cs.canonical_marker_bytes(payload)


def test_read_index_marker_roundtrip(spool, tmp_path):
    art = tmp_path / "store" / "acme" / "art-1"
    art.mkdir(parents=True)
    assert spool.read_index_marker(str(art)).state is cs.MarkerState.ABSENT
    payload = {"v": 1, "plugin_dir": PLUGIN}
    spool.write_index_entry(str(art), payload)
    m = spool.read_index_marker(str(art))
    assert m.state is cs.MarkerState.PRESENT and m.payload == payload


def test_read_marker_of_a_fifo_is_invalid_and_never_blocks(spool):
    """A FIFO at ready.json (a swapped-in pipe) must be INVALID, and the read
    must return IMMEDIATELY — O_NONBLOCK + the S_ISREG gate mean it is never
    opened for a blocking read. If this hangs, the test times out."""
    os.mkfifo(_pdir(spool) / "ready.json")
    m = spool.read_marker(PLUGIN)
    assert m.state is cs.MarkerState.INVALID and m.payload is None


def test_read_marker_of_an_oversized_file_is_invalid(spool):
    (_pdir(spool) / "ready.json").write_bytes(
        b'{"v":1,"pad":"' + b"p" * (cs.MARKER_STATE_MAX_BYTES + 16) + b'"}')
    assert spool.read_marker(PLUGIN).state is cs.MarkerState.INVALID


def test_read_marker_of_garbage_json_is_invalid(spool):
    (_pdir(spool) / "ready.json").write_text("{not valid json", encoding="utf-8")
    assert spool.read_marker(PLUGIN).state is cs.MarkerState.INVALID


def test_read_marker_of_a_non_object_is_invalid(spool):
    (_pdir(spool) / "ready.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert spool.read_marker(PLUGIN).state is cs.MarkerState.INVALID


def test_read_marker_of_a_symlink_is_invalid(spool, tmp_path):
    """O_NOFOLLOW: a symlinked ready.json is INVALID (never followed to an
    outside inode), not ABSENT."""
    target = tmp_path / "outside.json"
    target.write_text('{"v": 1}', encoding="utf-8")
    os.symlink(target, _pdir(spool) / "ready.json")
    assert spool.read_marker(PLUGIN).state is cs.MarkerState.INVALID


def test_read_marker_of_a_directory_is_invalid(spool):
    os.mkdir(_pdir(spool) / "ready.json")
    assert spool.read_marker(PLUGIN).state is cs.MarkerState.INVALID


def test_read_index_marker_of_a_fifo_is_invalid_and_never_blocks(spool, tmp_path):
    art = tmp_path / "store" / "acme" / "art-1"
    art.mkdir(parents=True)
    spool.write_index_entry(str(art), {"v": 1})       # creates the .index dir
    entry = Path(spool.root) / cs.INDEX_DIR / f"{index_key(str(art))}.json"
    entry.unlink()
    os.mkfifo(entry)
    assert spool.read_index_marker(str(art)).state is cs.MarkerState.INVALID


def test_read_marker_of_deeply_nested_json_is_invalid_and_never_raises(spool):
    """The three-state reader is TOTAL: a body of 60k opening brackets fits the
    64 KiB size cap but makes ``json.loads`` raise ``RecursionError`` (not a
    ``ValueError``) — the reader must still return INVALID, never propagate."""
    (_pdir(spool) / "ready.json").write_bytes(b"[" * 60000)
    m = spool.read_marker(PLUGIN)
    assert m.state is cs.MarkerState.INVALID and m.payload is None


# ---------------------------------------------------------------------------
# type-safe marker retirement + total enumeration (a non-regular ready.json /
# index entry must be ENUMERATED and RETIRED, never left to block republish)
# ---------------------------------------------------------------------------


def _make_dir(p):  os.mkdir(p)
def _make_fifo(p): os.mkfifo(p)
def _make_symlink(p): os.symlink("/nonexistent-target", p)


_NON_REGULAR = [
    pytest.param(_make_dir, id="dir"),
    pytest.param(_make_fifo, id="fifo"),
    pytest.param(_make_symlink, id="symlink"),
]


@pytest.mark.parametrize("maker", _NON_REGULAR)
def test_published_plugins_enumerates_a_non_regular_ready_marker(spool, maker):
    """An invalid ready.json (dir/FIFO/symlink) is an orphan that MUST be
    enumerated for retirement — omitting it (the old S_ISREG gate) would leave
    the invalid marker to survive forever and block republication."""
    maker(_pdir(spool) / "ready.json")
    assert spool.published_plugins() == [PLUGIN]


@pytest.mark.parametrize("maker", _NON_REGULAR)
def test_delete_ready_retires_a_marker_of_any_type(spool, maker):
    """A raw unlink FAILS on a directory-shaped marker; the type-aware retire
    removes any type and reports the entry now-absent."""
    maker(_pdir(spool) / "ready.json")
    assert spool.delete_ready(PLUGIN) is True
    assert not os.path.lexists(_pdir(spool) / "ready.json")   # incl. no symlink
    assert spool.published_plugins() == []


def test_delete_ready_of_an_absent_marker_reports_absent(spool):
    assert spool.delete_ready(PLUGIN) is True                 # nothing there


def test_delete_ready_surfaces_a_failed_removal(spool, monkeypatch):
    """A genuinely-failing removal must NOT be reported as absent — the caller
    surfaces it rather than assuming both-absent."""
    (_pdir(spool) / "ready.json").write_text("{}")
    monkeypatch.setattr(cs, "_remove_entry", lambda *a, **k: False)
    assert spool.delete_ready(PLUGIN) is False                # not swallowed
    assert (_pdir(spool) / "ready.json").exists()             # survived


def _lstat_raising(errnum, *, on_call=None):
    """Wrap the real ``os.lstat`` so a lstat of ``ready.json`` raises *errnum*
    (on the Nth such call, if *on_call* is given; else every call)."""
    real = os.lstat
    seen = {"n": 0}

    def flaky(path, *, dir_fd=None):
        if path == "ready.json":
            seen["n"] += 1
            if on_call is None or seen["n"] == on_call:
                raise OSError(errnum, os.strerror(errnum))
        return real(path, dir_fd=dir_fd)

    return flaky


def test_retire_pre_removal_metadata_error_is_not_read_as_absent(
    spool, monkeypatch,
):
    """A non-ENOENT failure on the PRE-removal probe (here EIO) must NOT be
    treated as 'already absent' — retirement reports FAILURE and the marker,
    never even removed, survives."""
    (_pdir(spool) / "ready.json").write_text("{}")
    monkeypatch.setattr(cs.os, "lstat", _lstat_raising(errno.EIO))
    assert spool.delete_ready(PLUGIN) is False
    assert (_pdir(spool) / "ready.json").exists()             # untouched


def test_retire_confirmation_metadata_error_reports_failure(spool, monkeypatch):
    """When the POST-removal RE-CONFIRMATION lstat raises EACCES, removal
    success cannot be confirmed, so retirement reports FAILURE (not a silent
    false success) — the reconcile then surfaces callback_spool_error."""
    (_pdir(spool) / "ready.json").write_text("{}")
    # First lstat (pre-removal probe) succeeds; the second (confirmation) raises.
    monkeypatch.setattr(cs.os, "lstat",
                        _lstat_raising(errno.EACCES, on_call=2))
    assert spool.delete_ready(PLUGIN) is False


def test_retire_confirmation_enoent_is_success(spool, monkeypatch):
    """A real removal whose confirming lstat sees ENOENT (the entry is gone) is
    reported as success — ENOENT is the ONLY 'absent' outcome."""
    (_pdir(spool) / "ready.json").write_text("{}")
    # Force the confirmation lstat to raise FileNotFoundError explicitly, even
    # though the real removal already made it ENOENT — success either way.
    monkeypatch.setattr(cs.os, "lstat",
                        _lstat_raising(errno.ENOENT, on_call=2))
    assert spool.delete_ready(PLUGIN) is True
    assert not (_pdir(spool) / "ready.json").exists()


def test_marker_lstat_is_three_valued(spool):
    """Unit pin: ENOENT ⇒ None (absent), any other OSError ⇒ _LSTAT_ERROR
    (unknown), a real entry ⇒ its stat_result (present)."""
    pfd = os.open(_pdir(spool), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert cs._marker_lstat("ready.json", pfd) is None      # ENOENT
        (_pdir(spool) / "ready.json").write_text("{}")
        st = cs._marker_lstat("ready.json", pfd)
        assert st is not None and st is not cs._LSTAT_ERROR      # present
    finally:
        os.close(pfd)


@pytest.mark.parametrize("maker", _NON_REGULAR)
def test_index_keys_enumerates_and_delete_retires_a_non_regular_entry(
    spool, tmp_path, maker,
):
    art = tmp_path / "store" / "acme" / "art-1"
    art.mkdir(parents=True)
    spool.write_index_entry(str(art), {"v": 1})       # creates the .index dir
    key = index_key(str(art))
    entry = Path(spool.root) / cs.INDEX_DIR / f"{key}.json"
    entry.unlink()
    maker(entry)                                      # corrupt into a non-regular
    assert key in spool.index_keys()                  # still enumerated
    assert spool.delete_index_key(key) is True
    assert not os.path.lexists(entry)
    assert spool.index_keys() == []


def test_delete_index_entry_retires_a_directory_shaped_entry(spool, tmp_path):
    art = tmp_path / "store" / "acme" / "art-1"
    art.mkdir(parents=True)
    spool.write_index_entry(str(art), {"v": 1})
    entry = Path(spool.root) / cs.INDEX_DIR / f"{index_key(str(art))}.json"
    entry.unlink()
    os.mkdir(entry)                                   # directory-shaped entry
    assert spool.delete_index_entry(str(art)) is True
    assert not entry.exists()


# ---------------------------------------------------------------------------
# attempts ledger plumbing (attempts/ dir, reserved dot-root grammar,
# attempt-file I/O, strict fsync) — the durable per-flow record's substrate
# ---------------------------------------------------------------------------


def _attempts(spool, plugin=PLUGIN) -> Path:
    return _pdir(spool, plugin) / "attempts"


def _attempt_rec(h: str, now: float, status: str = "result_ready") -> dict:
    return ca.new_attempt(state_hash=h, minted_ts=now - 5.0, status=status,
                          now=now)


def test_plugin_dirs_excludes_every_dotted_root_entry(spool, tmp_path):
    """EVERY dot-prefixed root entry is reserved (`.index`, `.removals`, and
    anything future) — excluded from plugin enumeration, and therefore from
    sweep, recovery and orphan GC alike."""
    art = tmp_path / "store" / "acme" / "art-1"
    art.mkdir(parents=True)
    spool.write_index_entry(str(art), {"v": 1})       # creates .index
    (Path(spool.root) / ".removals").mkdir()
    (Path(spool.root) / ".future-reserved").mkdir()
    spool.ensure_plugin_dirs("beta")
    assert spool.plugins() == [PLUGIN, "beta"]


def test_gc_never_removes_a_dotted_root_dir(spool):
    """Retention pin: `.removals` (a quiescent, registry-absent dot dir) must
    SURVIVE orphan GC even when an equally-quiescent plugin dir is removed —
    a removal record outliving its plugin dir is the whole point of it."""
    now = time.time()
    removals = Path(spool.root) / ".removals"
    removals.mkdir()
    (removals / "acme-deadbeef.json").write_text("{}")
    _quiesce(removals, now - 5 * DAY)
    spool.ensure_plugin_dirs("ghost")
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)

    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                   now=now)

    assert "ghost" in removed, "an equally-quiescent plugin dir IS removed"
    assert removals.is_dir()
    assert (removals / "acme-deadbeef.json").is_file()


def test_write_and_read_attempt_round_trip(spool):
    now = time.time()
    h = state_hash("flow-1")
    rec = _attempt_rec(h, now)
    assert spool.write_attempt(PLUGIN, h, rec) is True
    m = spool.read_attempt(PLUGIN, h)
    assert m.state is cs.MarkerState.PRESENT
    assert m.raw == cs.canonical_marker_bytes(rec)
    assert ca.validate_attempt(m.payload) == rec


def test_write_attempt_creates_the_attempts_dir_when_missing(spool):
    """A pre-upgrade plugin dir has no attempts/; the first write creates it
    (0770, chmod-via-fd) rather than failing until the next reconcile."""
    _attempts(spool).rmdir()
    now = time.time()
    h = state_hash("flow-2")
    assert spool.write_attempt(PLUGIN, h, _attempt_rec(h, now)) is True
    assert stat.S_IMODE(_attempts(spool).stat().st_mode) == 0o770
    assert spool.read_attempt(PLUGIN, h).state is cs.MarkerState.PRESENT


def test_attempt_io_refuses_bad_grammar(spool):
    now = time.time()
    rec = _attempt_rec(state_hash("x"), now)
    assert spool.write_attempt(PLUGIN, "not-a-hash", rec) is False
    assert spool.write_attempt("../escape", state_hash("x"), rec) is False
    assert spool.read_attempt(PLUGIN, "not-a-hash").state is cs.MarkerState.ABSENT
    assert spool.read_attempt("never-made", state_hash("x")).state \
        is cs.MarkerState.ABSENT


def test_read_attempt_of_a_fifo_is_invalid_and_never_blocks(spool):
    h = state_hash("fifo-flow")
    os.mkfifo(_attempts(spool) / f"{h}.json")
    m = spool.read_attempt(PLUGIN, h)
    assert m.state is cs.MarkerState.INVALID and m.payload is None


def test_read_attempt_of_an_oversized_file_is_invalid(spool):
    h = state_hash("fat-flow")
    (_attempts(spool) / f"{h}.json").write_bytes(
        b'{"v":1,"pad":"' + b"p" * (cs.MARKER_STATE_MAX_BYTES + 16) + b'"}')
    assert spool.read_attempt(PLUGIN, h).state is cs.MarkerState.INVALID


def test_list_attempts_returns_only_valid_records(spool):
    """A malformed (possibly consumer-scribbled) attempt file is never
    returned as truth — it is named by list_invalid_attempts so the caller
    re-derives it from live artifacts."""
    now = time.time()
    h_good, h_bad, h_fifo = (state_hash(s) for s in ("good", "bad", "worse"))
    rec = _attempt_rec(h_good, now)
    assert spool.write_attempt(PLUGIN, h_good, rec)
    (_attempts(spool) / f"{h_bad}.json").write_text('{"not":"an attempt"}')
    os.mkfifo(_attempts(spool) / f"{h_fifo}.json")

    assert spool.list_attempts(PLUGIN) == [(h_good, rec)]
    assert spool.list_invalid_attempts(PLUGIN) == sorted([h_bad, h_fifo])


def test_list_ack_tokens_accepts_only_the_exact_grammar(spool):
    h = state_hash("acked")
    att = _attempts(spool)
    (att / f".ack-{h}").write_text("")
    (att / ".ack-short").write_text("")                # not 64 hex
    (att / f".ack-{h[:-1]}g").write_text("")           # 64 chars, non-hex
    (att / f".ack-{h}.json").write_text("")            # trailing garbage
    (att / f"{h}.json").write_text("{}")               # an attempt, not a token
    assert spool.list_ack_tokens(PLUGIN) == [h]


def test_collect_held_hashes_grammar_dedupe_sorted(spool):
    h1, h2 = sorted(state_hash(s) for s in ("held-a", "held-b"))
    res = _results(spool)
    (res / f".collect-{h1}-{'a' * 32}").write_text("x")
    (res / f".collect-{h1}-{'b' * 32}").write_text("x")   # same hash: dedupe
    (res / f".collect-{h2}-{'c' * 32}").write_text("x")
    (res / ".collect-nothex-uuid").write_text("x")        # not 64 hex
    (res / f".collect-{h2}").write_text("x")              # no '-<uuid>' part
    assert spool.collect_held_hashes(PLUGIN) == [h1, h2]


def test_write_attempt_strict_staging_fsync_failure_keeps_previous(
        spool, monkeypatch):
    """Amendment-3 arm 1: a strict fsync failure of the STAGED file aborts
    BEFORE the rename — write_attempt returns False, the previous record is
    intact and readable, and the staged temp is cleaned."""
    now = time.time()
    h = state_hash("strict-1")
    old = _attempt_rec(h, now, status="awaiting_redirect")
    assert spool.write_attempt(PLUGIN, h, old) is True

    real = cs._fsync_strict
    calls = {"n": 0}

    def fail_first(fd, what):
        calls["n"] += 1
        if calls["n"] == 1:
            raise cs.FsyncFailed(errno.EIO, "injected")
        return real(fd, what)

    monkeypatch.setattr(cs, "_fsync_strict", fail_first)
    new = ca.terminalize(old, "expired", now=now)

    assert spool.write_attempt(PLUGIN, h, new, strict=True) is False

    m = spool.read_attempt(PLUGIN, h)
    assert m.state is cs.MarkerState.PRESENT
    assert m.raw == cs.canonical_marker_bytes(old), "previous record intact"
    residue = [n for n in os.listdir(_attempts(spool))
               if n.startswith(".") and ".tmp-" in n]
    assert residue == [], "staged temp cleaned"


def test_write_attempt_strict_dir_fsync_failure_new_record_visible(
        spool, monkeypatch):
    """Amendment-3 arm 2 (semantics pin): a strict fsync failure of the
    DIRECTORY (after the rename) still returns False — the caller skips its
    dependent deletion — but the NEW record may be, and here is, VISIBLE.
    That is correct: a visible terminal record beside a live artifact is the
    provisional state, re-derived next pass."""
    now = time.time()
    h = state_hash("strict-2")
    old = _attempt_rec(h, now, status="awaiting_redirect")
    assert spool.write_attempt(PLUGIN, h, old) is True

    real = cs._fsync_strict
    calls = {"n": 0}

    def fail_second(fd, what):
        calls["n"] += 1
        if calls["n"] == 2:
            raise cs.FsyncFailed(errno.EIO, "injected")
        return real(fd, what)

    monkeypatch.setattr(cs, "_fsync_strict", fail_second)
    new = ca.terminalize(old, "expired", now=now)

    assert spool.write_attempt(PLUGIN, h, new, strict=True) is False
    assert calls["n"] == 2, "strict write = staged-file fsync + dir fsync"

    m = spool.read_attempt(PLUGIN, h)
    assert m.state is cs.MarkerState.PRESENT
    assert m.raw == cs.canonical_marker_bytes(new), "new record IS visible"


def test_newest_mtime_still_sees_content_in_attempts(spool):
    """Depth pin: the orphan-GC quiescence scan (depth 3) covers the new
    attempts/ subdir — a young attempt file keeps its dir non-quiescent."""
    now = time.time()
    _quiesce(_pdir(spool), now - 5 * DAY)
    assert spool._newest_mtime(PLUGIN) < now - 4 * DAY
    _put(_attempts(spool) / f"{state_hash('young')}.json", now - 60)
    _utime(_attempts(spool), now - 5 * DAY)
    _utime(_pdir(spool), now - 5 * DAY)
    assert spool._newest_mtime(PLUGIN) >= now - 61


def test_attempt_apis_are_empty_on_a_closed_spool(tmp_path):
    now = time.time()
    h = state_hash("closed")
    s = CallbackSpool(tmp_path / "callbacks")
    s.ensure_plugin_dirs(PLUGIN)
    assert s.write_attempt(PLUGIN, h, _attempt_rec(h, now)) is True
    s.close()
    assert s.read_attempt(PLUGIN, h).state is cs.MarkerState.ABSENT
    assert s.write_attempt(PLUGIN, h, _attempt_rec(h, now)) is False
    assert s.list_attempts(PLUGIN) == []
    assert s.list_invalid_attempts(PLUGIN) == []
    assert s.list_ack_tokens(PLUGIN) == []
    assert s.collect_held_hashes(PLUGIN) == []


# ---------------------------------------------------------------------------
# consumer reference helpers — collect / ack (the §7 pickup contract,
# executable beside mint())
# ---------------------------------------------------------------------------


def test_collect_renames_then_reads_and_never_unlinks(spool):
    """collect = rename results/<h>.json -> .collect-<h>-<uuid>, read AFTER
    the rename. The held file SURVIVES the call — the consumer keeps it until
    ack; nothing is ever unlinked here."""
    h, claim = _claimed(spool, "collect-me")
    rec = _record(h)
    assert spool.publish_result(claim, rec) is cs.PublishOutcome.PUBLISHED

    got, held = cs.collect(_pdir(spool), h)

    # publish augments the stored record with the meta/minted_ts transport keys
    # (spec §4); everything the caller supplied survives verbatim.
    assert {k: got[k] for k in rec} == rec
    assert not (_results(spool) / f"{h}.json").exists(), "base name gone"
    assert held.parent == _results(spool)
    assert held.name.startswith(f".collect-{h}-")
    assert held.is_file(), "held file still exists after collect returns"
    assert json.loads(held.read_text()) == got
    assert spool.collect_held_hashes(PLUGIN) == [h], \
        "the held name satisfies the .collect-<h>-<uuid> grammar"


def test_collect_of_an_absent_result_raises_filenotfound(spool):
    """The attempt-first publish ordering opens a window where the attempt is
    visible before the result link lands: ENOENT here is RETRYABLE, never
    ackable — it propagates untouched to the caller's retry loop."""
    with pytest.raises(FileNotFoundError):
        cs.collect(_pdir(spool), state_hash("never-published"))


def test_collect_refuses_a_malformed_hash(spool):
    with pytest.raises(ValueError):
        cs.collect(_pdir(spool), "not-a-hash")


def test_ack_renames_the_attempt_to_a_durable_token(spool):
    now = time.time()
    h = state_hash("ack-flow")
    assert spool.write_attempt(PLUGIN, h, _attempt_rec(h, now)) is True

    assert cs.ack(_pdir(spool), h) is True

    att = _attempts(spool)
    assert (att / f".ack-{h}").exists()
    assert not (att / f"{h}.json").exists()
    assert spool.list_ack_tokens(PLUGIN) == [h]


def test_ack_is_idempotent_on_enoent(spool):
    """A second ack finds the source absent: ENOENT means already acked (or
    already settled) — True, no error, and the token is untouched."""
    now = time.time()
    h = state_hash("ack-twice")
    assert spool.write_attempt(PLUGIN, h, _attempt_rec(h, now)) is True
    assert cs.ack(_pdir(spool), h) is True

    assert cs.ack(_pdir(spool), h) is True

    assert (_attempts(spool) / f".ack-{h}").exists(), "token still there"
    # a never-written attempt is equally "already settled"
    assert cs.ack(_pdir(spool), state_hash("never-attempted")) is True


def test_ack_fsync_failure_propagates(spool, monkeypatch):
    """The ack witness must be crash-durable (spec §7): an fsync failure of
    the attempts dir RAISES to the caller — the consumer must not treat an
    unwitnessed ack as settled. (The rename may already have happened; that
    is fine — the next ack call re-witnesses.)"""
    now = time.time()
    h = state_hash("ack-unwitnessed")
    assert spool.write_attempt(PLUGIN, h, _attempt_rec(h, now)) is True

    def boom(fd, what):
        raise cs.FsyncFailed(errno.EIO, "injected")

    monkeypatch.setattr(cs, "_fsync_strict", boom)
    with pytest.raises(cs.FsyncFailed):
        cs.ack(_pdir(spool), h)


def test_ack_retried_after_a_lost_witness_rewitnesses_the_rename(
        spool, monkeypatch):
    """Red case (Sol 2): the first ack's RENAME succeeds and its fsync then
    fails, so the retry finds the source ENOENT. Returning True there with no
    fsync reports an UNWITNESSED rename as settled — the consumer moves on
    and a power loss rolls the rename back. The ENOENT arm must witness."""
    now = time.time()
    h = state_hash("ack-lost-witness")
    assert spool.write_attempt(PLUGIN, h, _attempt_rec(h, now)) is True

    real = cs._fsync_strict
    calls = {"n": 0}

    def fail_first(fd, what):
        calls["n"] += 1
        if calls["n"] == 1:
            raise cs.FsyncFailed(errno.EIO, "injected")
        return real(fd, what)

    monkeypatch.setattr(cs, "_fsync_strict", fail_first)

    with pytest.raises(cs.FsyncFailed):
        cs.ack(_pdir(spool), h)
    assert (_attempts(spool) / f".ack-{h}").exists(), "the rename DID happen"

    assert cs.ack(_pdir(spool), h) is True

    assert calls["n"] == 2, "the ENOENT arm fsyncs — True means witnessed"


def test_strict_write_proves_the_plugin_parent_durable_on_every_call(
        spool, monkeypatch):
    """Red case (Sol 3 = Terra 1): ``attempts/`` is created on demand, so the
    directory ENTRY that names it was fsynced best-effort by the creating
    call only — every later call sees ``created=False`` and never retries it.
    A silent failure there (reachable on every v0.146 upgrade) loses the whole
    ledger across a power loss while the deletions its records authorized
    survive. A strict write proves the parent on EVERY call, or returns
    False."""
    now = time.time()
    h = state_hash("parent-durability")
    rec = _attempt_rec(h, now)
    assert spool.write_attempt(PLUGIN, h, rec) is True   # attempts/ exists now

    real = cs._fsync_strict
    seen: list[str] = []

    def fail_parent(fd, what):
        seen.append(what)
        if what == PLUGIN:
            raise cs.FsyncFailed(errno.EIO, "injected")
        return real(fd, what)

    monkeypatch.setattr(cs, "_fsync_strict", fail_parent)

    assert spool.write_attempt(PLUGIN, h, ca.terminalize(rec, "expired",
                                                         now=now),
                               strict=True) is False
    assert seen == ["staged attempt", cs.ATTEMPTS_DIR, PLUGIN], \
        "staged file, then attempts/, then the dir that NAMES attempts/"


def test_a_parent_fsync_failure_defers_the_deletion_it_would_authorize(
        spool, monkeypatch):
    """The same defect at its write-ahead site, on the upgrade path that
    reaches it: a plugin dir with no ``attempts/`` yet (v0.146), a sweep that
    creates one, and a parent fsync that fails. The expired pending must
    SURVIVE the pass — otherwise the deletion is kept while the whole
    directory entry can be lost (INV-CB-007) — and converge on the next."""
    now = time.time()
    state = "upgrade-flow"
    h = state_hash(state)
    p = mint(_pdir(spool), state)
    _utime(p, now - PENDING_TTL_S - 1)
    _attempts(spool).rmdir()                 # a pre-upgrade plugin dir

    real = cs._fsync_strict

    def fail_parent(fd, what):
        if what == PLUGIN:
            raise cs.FsyncFailed(errno.EIO, "injected")
        return real(fd, what)

    monkeypatch.setattr(cs, "_fsync_strict", fail_parent)

    first = spool.sweep(now=now)

    assert p.exists(), "the artifact outlives an unprovable directory entry"
    assert first.deleted_pending == 0 and first.skipped_undurable == 1

    monkeypatch.undo()
    second = spool.sweep(now=now)

    assert not p.exists() and second.deleted_pending == 1
    assert _attempt_of(spool, h)["outcome"] == "expired"


# ---------------------------------------------------------------------------
# write-ahead outcomes (INV-CB-007) — casa never deletes a credential-bearing
# artifact before the flow's terminal outcome is DURABLY on its attempt file,
# and never records an outcome for residue that names no flow.
# ---------------------------------------------------------------------------


def _attempt_of(spool, h, plugin=PLUGIN) -> dict:
    """The schema-valid attempt record for *h* — the assertion helper for
    every write-ahead site (a record that will not validate is not a record)."""
    m = spool.read_attempt(plugin, h)
    assert m.state is cs.MarkerState.PRESENT, "an attempt record must exist"
    rec = ca.validate_attempt(m.payload)
    assert rec is not None, "the record must be schema-valid"
    return rec


def test_expired_pending_records_expired_with_the_derived_mint(spool):
    """The pending inode IS the derivation source: its mtime is the mint
    clock and its v2 envelope carries the consumer's binding — both survive
    the file that carried them."""
    now = time.time()
    state, meta = "expiring-flow", {"flow": "gmail", "n": 3}
    h = state_hash(state)
    p = mint(_pdir(spool), state, meta=meta)
    _utime(p, now - PENDING_TTL_S - 1)
    minted = p.stat().st_mtime

    report = spool.sweep(now=now)

    assert not p.exists()
    assert report.deleted_pending == 1 and report.skipped_undurable == 0
    rec = _attempt_of(spool, h)
    assert rec["status"] == "done" and rec["outcome"] == "expired"
    assert rec["ended_ts"] == now
    assert rec["minted_ts"] == minted, "the mint clock, not the sweep clock"
    assert rec["meta"] == meta
    assert rec["claimed"] is False


def test_expired_result_records_expired_unread_unclaimed(spool):
    """A published result that nobody collected: `expired_unread`, and
    `claimed` stays False — no rename ever happened."""
    now = time.time()
    h, claim = _claimed(spool, "uncollected")
    assert spool.publish_result(claim, _record(h)) is cs.PublishOutcome.PUBLISHED
    r = _results(spool) / f"{h}.json"
    _utime(r, now - RESULT_TTL_S - 1)

    report = spool.sweep(now=now)

    assert not r.exists() and report.deleted_results == 1
    rec = _attempt_of(spool, h)
    assert rec["status"] == "done" and rec["outcome"] == "expired_unread"
    assert rec["claimed"] is False
    assert rec["minted_ts"] == claim.mtime, \
        "the publish-time record is reused, not re-derived"


def test_aged_collect_records_expired_unread_claimed(spool):
    """A hold that aged out is the coarse-label residual: the consumer
    renamed (so `claimed`) but never acked, and casa cannot say whether the
    payload was ever read."""
    now = time.time()
    h = state_hash("held-flow")
    held = _put(_results(spool) / f".collect-{h}-{'a' * 32}",
                now - RESULT_TTL_S - 1, '{"v": 1}')

    report = spool.sweep(now=now)

    assert not held.exists() and report.deleted_collect == 1
    rec = _attempt_of(spool, h)
    assert rec["status"] == "done" and rec["outcome"] == "expired_unread"
    assert rec["claimed"] is True
    assert rec["minted_ts"] is None and rec["meta"] is None, \
        "a hold is a NAME — casa never opens it, so nothing else is knowable"


def test_collect_entries_get_their_own_cap_oldest_first(spool):
    """A rename-happy consumer must not hold unbounded credential-bearing
    inodes: `.collect-*` entries stay out of MAX_RESULTS and carry their own
    cap, each eviction recorded exactly as its TTL age-out would be."""
    now = time.time()
    made = []
    for i in range(cs.MAX_COLLECT + 1):
        h = state_hash(f"held-{i}")
        made.append((h, _put(_results(spool) / f".collect-{h}-{'a' * 32}",
                             now - 100 + i)))

    report = spool.sweep(now=now)

    oldest_h, oldest_p = made[0]
    assert not oldest_p.exists()
    assert all(p.exists() for _h, p in made[1:])
    assert len(list(_results(spool).iterdir())) == cs.MAX_COLLECT
    assert report.deleted_collect_capped == 1
    assert report.capped == [f"{PLUGIN}/results/{cs.COLLECT_PREFIX}*"]
    rec = _attempt_of(spool, oldest_h)
    assert rec["outcome"] == "expired_unread" and rec["claimed"] is True


def test_pending_cap_evicts_an_attempt_less_entry_before_an_open_one(spool):
    """The §9 ladder: cap pressure destroys what casa knows LEAST about
    first. The oldest entry here is the one with an open attempt — plain
    oldest-first would take it; the ladder takes the oldest attempt-less one
    instead."""
    now = time.time()
    made = []
    for i in range(cs.MAX_PENDING + 1):
        h = state_hash(f"cap-{i}")
        made.append((h, _put(_pending(spool) / f"{h}.json", now - 300 + i)))
    open_h, open_p = made[0]
    assert spool.write_attempt(PLUGIN, open_h, ca.new_attempt(
        state_hash=open_h, minted_ts=now - 300, status="awaiting_redirect",
        now=now))
    victim_h, victim_p = made[1]

    report = spool.sweep(now=now)

    assert open_p.exists(), "an open attempt is evicted LAST"
    assert not victim_p.exists()
    assert report.deleted_capped == 1
    assert report.capped == [f"{PLUGIN}/pending"]
    rec = _attempt_of(spool, victim_h)
    assert rec["status"] == "done" and rec["outcome"] == "evicted"


def test_malformed_collect_names_are_residue_bounded_by_the_result_cap(spool):
    """Red case (Terra 3): the sweep read the bare ``.collect-`` PREFIX as
    "a collect entry" (excluded from MAX_RESULTS) while only grammar-valid
    names entered the MAX_COLLECT list — so a consumer scribbling thousands
    of ``.collect-junk`` files evaded BOTH bounds. The single grammar
    decides: a name that does not parse is residue — counted toward
    MAX_RESULTS, aged on TEMP_TTL_S, and recorded NOWHERE."""
    now = time.time()
    for i in range(cs.MAX_RESULTS + 1):
        _put(_results(spool) / f".collect-junk-{i}", now - 300 + i)
    aged = _put(_results(spool) / ".collect-scribble", now - TEMP_TTL_S - 1)

    report = spool.sweep(now=now)

    assert not aged.exists(), "residue ages out on the residue TTL"
    assert len(list(_results(spool).iterdir())) == cs.MAX_RESULTS, \
        "malformed prefixed names count toward MAX_RESULTS"
    assert report.deleted_capped == 1
    assert report.capped == [f"{PLUGIN}/results"]
    assert os.listdir(_attempts(spool)) == [], \
        "a name that parses to no flow is given no flow's outcome"


def test_the_cap_ladder_takes_residue_before_any_real_flow(spool):
    """Red case (Sol 6, spec §9): `.part` residue and a real attempt-less
    flow used to rank identically (mtime only), so a NEWER `.part` survived
    while an older genuine flow was destroyed and recorded ``evicted``.
    Residue is rank 0; mtime ranks only WITHIN a rank."""
    now = time.time()
    made = []
    for i in range(cs.MAX_PENDING):
        h = state_hash(f"ladder-{i}")
        made.append((h, _put(_pending(spool) / f"{h}.json", now - 300 + i)))
    part = _put(_pending(spool) / f"{state_hash('newest')}.json"
                f"{cs.PART_SUFFIX}", now - 10)

    report = spool.sweep(now=now)

    assert not part.exists(), "the NEWEST residue outranks the OLDEST flow"
    assert all(p.exists() for _h, p in made)
    assert report.deleted_capped == 1
    assert os.listdir(_attempts(spool)) == [], "residue is recorded NOWHERE"


def test_a_part_file_evicted_by_the_cap_is_recorded_nowhere(spool):
    """Amendment-7 pin: a `.part` names no minted state (the mint publishes
    by link, so only the final name was ever a flow) — cap-evicting one
    creates NO attempt file and no outcome."""
    now = time.time()
    part_h = state_hash("residue")
    part = _put(_pending(spool) / f"{part_h}.json{cs.PART_SUFFIX}",
                now - TEMP_TTL_S + 10)       # oldest LIVE entry, not yet aged
    for i in range(cs.MAX_PENDING):
        h = state_hash(f"live-{i}")
        _put(_pending(spool) / f"{h}.json", now - TEMP_TTL_S + 20 + i)

    report = spool.sweep(now=now)

    assert not part.exists(), "the oldest live entry is the cap's victim"
    assert len(list(_pending(spool).iterdir())) == cs.MAX_PENDING
    assert report.deleted_capped == 1
    assert spool.read_attempt(PLUGIN, part_h).state is cs.MarkerState.ABSENT
    assert os.listdir(_attempts(spool)) == [], "residue is recorded NOWHERE"


def test_a_hash_named_anomaly_is_recorded_expired_before_removal(spool):
    """Amendment-15 pin: a non-regular inode wearing a hash name still names
    a flow, so its removal is a retirement — recorded — not residue disposal."""
    now = time.time()
    h = state_hash("anomalous")
    p = _results(spool) / f"{h}.json"
    os.mkfifo(p)

    report = spool.sweep(now=now)

    assert not os.path.lexists(p)
    assert report.deleted_anomalous == 1
    rec = _attempt_of(spool, h)
    assert rec["status"] == "done" and rec["outcome"] == "expired"


def test_recovery_stale_claim_drop_records_expired(spool):
    """The claim inode is the pending inode (link preserved both mtime and
    bytes): dropping it records the mint clock and the binding it carried."""
    now = time.time()
    h = state_hash("stale-claim")
    claim = _put(_claims(spool) / h, now - PENDING_TTL_S - 1,
                 '{"v": 2, "meta": {"x": 1}}')
    minted = claim.stat().st_mtime

    report = spool.recovery_pass(now=now, boot=True)

    assert not claim.exists()
    assert report.dropped == [(PLUGIN, h)]
    rec = _attempt_of(spool, h)
    assert rec["status"] == "done" and rec["outcome"] == "expired"
    assert rec["minted_ts"] == minted and rec["meta"] == {"x": 1}


def test_recovery_non_regular_claim_records_expired(spool):
    now = time.time()
    h = state_hash("weird-claim")
    os.mkfifo(_claims(spool) / h)

    spool.recovery_pass(now=now, boot=True)

    assert not os.path.lexists(_claims(spool) / h)
    assert _attempt_of(spool, h)["outcome"] == "expired"


def test_recovery_restore_to_pending_records_no_outcome(spool):
    """A CUSTODY TRANSFER, not a retirement: the flow continues in pending/
    under its own TTL, so nothing terminal may be written — a terminal record
    beside a live artifact is exactly the state the design forbids leaving."""
    now = time.time()
    h = state_hash("restored")
    _put(_claims(spool) / h, now - 120)

    report = spool.recovery_pass(now=now, boot=True)

    assert report.restored == [(PLUGIN, h)]
    assert (_pending(spool) / f"{h}.json").exists()
    assert spool.read_attempt(PLUGIN, h).state is cs.MarkerState.ABSENT


def test_recovery_result_custody_transfer_records_no_outcome(spool):
    """Same rule on the other custody arm: a claim whose result was already
    published hands the flow to results/, and only reports a nudge."""
    now = time.time()
    h = state_hash("published-claim")
    _put(_claims(spool) / h, now - PENDING_TTL_S - 1)
    _put(_results(spool) / f"{h}.json", now - 10)

    report = spool.recovery_pass(now=now, boot=True)

    assert report.nudges == [(PLUGIN, h)]
    assert spool.read_attempt(PLUGIN, h).state is cs.MarkerState.ABSENT


def test_an_outcome_that_will_not_go_durable_defers_its_deletion(spool,
                                                                 monkeypatch):
    """The write-ahead PAIR, pinned: with the strict fsync failing, the
    expired pending SURVIVES the pass and is counted as deferred — a crash
    can never take the credential-bearing artifact and the only record of why
    together. With durability restored the next pass completes it.

    Note what is deliberately NOT asserted: that no record is visible after
    the failure. A strict dir-fsync failure can leave the new record visible
    (amendment 3); what must hold is that the ARTIFACT survives and the pass
    CONVERGES."""
    now = time.time()
    state = "undurable"
    h = state_hash(state)
    p = mint(_pdir(spool), state, meta={"flow": "x"})
    _utime(p, now - PENDING_TTL_S - 1)

    def boom(fd, what):
        raise cs.FsyncFailed(errno.EIO, "injected")

    monkeypatch.setattr(cs, "_fsync_strict", boom)

    first = spool.sweep(now=now)

    assert p.exists(), "the artifact outlives an unrecordable outcome"
    assert first.deleted_pending == 0
    assert first.skipped_undurable == 1

    monkeypatch.undo()
    second = spool.sweep(now=now)

    assert not p.exists()
    assert second.deleted_pending == 1 and second.skipped_undurable == 0
    rec = _attempt_of(spool, h)
    assert rec["status"] == "done" and rec["outcome"] == "expired"
    assert rec["meta"] == {"flow": "x"}


def test_a_result_deletion_lost_to_a_collector_reopens_the_attempt(
        spool, monkeypatch):
    """The consumer's collect rename wins the race between the write-ahead
    record and the unlink. The terminal label is PROVISIONAL: the surviving
    hold witnesses the contradiction and the attempt is rewritten open in the
    same pass, keeping the binding the artifacts can no longer supply."""
    now = time.time()
    meta = {"flow": "raced"}
    state = "raced-flow"
    h = state_hash(state)
    mint(_pdir(spool), state, meta=meta)
    claim = spool.claim(PLUGIN, h, now=now)
    assert spool.publish_result(claim, _record(h)) is cs.PublishOutcome.PUBLISHED
    _utime(_results(spool) / f"{h}.json", now - RESULT_TTL_S - 1)

    real = cs._unlink_quiet

    def racing_unlink(name, dir_fd):
        if name == f"{h}.json":
            os.rename(name, f".collect-{h}-{'a' * 32}",
                      src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            return False                     # the sweep lost the race
        return real(name, dir_fd)

    monkeypatch.setattr(cs, "_unlink_quiet", racing_unlink)

    spool.sweep(now=now)

    rec = _attempt_of(spool, h)
    assert rec["status"] == "result_ready", "reopened from the surviving hold"
    assert rec["outcome"] is None and rec["claimed"] is True
    assert rec["meta"] == meta, "the binding survives the re-derivation"


def test_sweep_attempts_dir_retires_residue_but_not_records_or_receipts(spool):
    """Amendment-8 hygiene: `attempts/` has exactly two legitimate name
    classes, and both own their own lifecycle. Everything else — this
    module's staged-replace temps after a crash, or a name nothing in the
    protocol writes — ages out on TEMP_TTL_S with no outcome."""
    now = time.time()
    att = _attempts(spool)
    h_live, h_acked = state_hash("kept"), state_hash("acked")
    rec = _attempt_rec(h_live, now)
    assert spool.write_attempt(PLUGIN, h_live, rec) is True
    _utime(att / f"{h_live}.json", now - TEMP_TTL_S - 1)   # aged, still a record
    token = _put(att / f".ack-{h_acked}", now - TEMP_TTL_S - 1, "")
    stale_tmp = _put(att / f".{h_live}.json.tmp-99-deadbeef",
                     now - TEMP_TTL_S - 1)
    junk = _put(att / "scribble.txt", now - TEMP_TTL_S - 1)
    fresh_tmp = _put(att / f".{h_live}.json.tmp-99-cafe", now - 5)

    report = spool.sweep(now=now)

    assert not stale_tmp.exists() and not junk.exists()
    assert fresh_tmp.exists(), "a temp inside its window belongs to a live writer"
    assert token.exists(), "an ack token is the consumer's receipt in flight"
    assert _attempt_of(spool, h_live) == rec, "a valid record is untouched"
    assert report.deleted_temps == 2


def test_derive_attempt_of_a_collect_only_flow_never_opens_the_held_file(
        spool, monkeypatch):
    """The one derivation source casa may only READ THE NAME of. A flow known
    solely by a hold materializes as `result_ready, claimed` with no mint
    clock and no binding — and the held file is never opened."""
    now = time.time()
    h = state_hash("held-alone")
    (_results(spool) / f".collect-{h}-{'a' * 32}").write_text('{"code": "x"}')
    fds = {sub: os.open(_pdir(spool) / sub, os.O_RDONLY | os.O_DIRECTORY)
           for sub in ("pending", "results", ".claims")}
    opened: list[str] = []
    real_open = os.open

    def spy(path, *a, **kw):
        opened.append(os.fspath(path) if not isinstance(path, int) else "")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(cs.os, "open", spy)
    try:
        rec = spool._derive_attempt(PLUGIN, h, pend_fd=fds["pending"],
                                    results_fd=fds["results"],
                                    claims_fd=fds[".claims"], now=now)
    finally:
        monkeypatch.undo()
        for fd in fds.values():
            os.close(fd)

    assert rec["status"] == "result_ready" and rec["claimed"] is True
    assert rec["meta"] is None and rec["minted_ts"] is None
    assert rec["outcome"] is None
    assert not any(cs.COLLECT_PREFIX in name for name in opened), \
        "casa never opens a consumer-held file"


# ---------------------------------------------------------------------------
# attempts_pass — materialization (§3.3), re-derivation (§6), receipt
# inference (§6, five normative probes) and the §9 bounds. The pass is casa's
# standing reconcile of the ledger against the artifacts, which are always
# authoritative: the attempt file is a DERIVED record and every contradiction
# is resolved in the artifacts' favour.
# ---------------------------------------------------------------------------


def _publish(spool, state, meta=None):
    """mint -> claim -> publish, returning ``(hash, claim)`` — a flow whose
    result is live and whose attempt file publish_result already wrote."""
    mint(_pdir(spool), state, meta=meta)
    h = state_hash(state)
    claim = spool.claim(PLUGIN, h, now=time.time())
    assert spool.publish_result(claim, _record(h)) is cs.PublishOutcome.PUBLISHED
    return h, claim


def _terminal(spool, h, outcome, *, ended: float, claimed: bool = False,
              status: str = "result_ready") -> dict:
    """Seed a terminal attempt file whose ``ended_ts`` is *ended*."""
    rec = ca.terminalize(
        ca.new_attempt(state_hash=h, minted_ts=ended - 10, status=status,
                       now=ended),
        outcome, now=ended, claimed=claimed)
    assert spool.write_attempt(PLUGIN, h, rec) is True
    return rec


def test_attempts_pass_materializes_a_pending_flow_from_its_envelope(spool):
    """A minted flow that no scan has seen yet: the pending inode is the
    derivation source — its mtime is the mint clock, its v2 envelope the
    consumer's binding."""
    now = time.time()
    state, meta = "mat-pending", {"kind": "renewal"}
    h = state_hash(state)
    p = mint(_pdir(spool), state, meta=meta)

    report = spool.attempts_pass(now=now, boot=True)

    assert report.materialized == 1
    rec = _attempt_of(spool, h)
    assert rec["status"] == "awaiting_redirect" and rec["outcome"] is None
    assert rec["meta"] == meta
    assert rec["minted_ts"] == p.stat().st_mtime
    assert rec["claimed"] is False


def test_attempts_pass_materializes_a_claimed_flow_from_the_claim_inode(spool):
    """The claim IS the pending inode (link preserved mtime and bytes), so a
    flow known only by its claim materializes with the same two facts."""
    now = time.time()
    h = state_hash("mat-claim")
    c = _put(_claims(spool) / h, now - 30, '{"v": 2, "meta": {"x": 1}}')

    report = spool.attempts_pass(now=now, boot=True)

    assert report.materialized == 1
    rec = _attempt_of(spool, h)
    assert rec["status"] == "awaiting_redirect"
    assert rec["meta"] == {"x": 1} and rec["minted_ts"] == c.stat().st_mtime


def test_attempts_pass_materializes_from_a_result_record(spool):
    """A pre-upgrade (or crash-lost) ledger entry for a published flow is
    rebuilt from the RECORD's transport keys — never from the result file's
    own mtime, which is the publish time, not the mint time."""
    now = time.time()
    state, meta = "mat-result", {"bank": "b"}
    h, claim = _publish(spool, state, meta=meta)
    os.unlink(_attempts(spool) / f"{h}.json")

    report = spool.attempts_pass(now=now, boot=True)

    assert report.materialized == 1
    rec = _attempt_of(spool, h)
    assert rec["status"] == "result_ready" and rec["claimed"] is False
    assert rec["meta"] == meta and rec["minted_ts"] == claim.mtime


def test_attempts_pass_materializes_a_hold_without_opening_it(spool,
                                                              monkeypatch):
    """A flow known SOLELY by a consumer-held name: `result_ready, claimed`
    with no mint clock and no binding — and the held file is never opened
    (it belongs to its holder; casa only reads the name)."""
    now = time.time()
    h = state_hash("mat-hold")
    (_results(spool) / f".collect-{h}-{'a' * 32}").write_text('{"code": "x"}')
    opened: list[str] = []
    real_open = os.open

    def spy(path, *a, **kw):
        if not isinstance(path, int):
            opened.append(os.fspath(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr(cs.os, "open", spy)
    try:
        report = spool.attempts_pass(now=now, boot=True)
    finally:
        monkeypatch.undo()

    assert report.materialized == 1
    rec = _attempt_of(spool, h)
    assert rec["status"] == "result_ready" and rec["claimed"] is True
    assert rec["meta"] is None and rec["minted_ts"] is None
    assert not any(cs.COLLECT_PREFIX in name for name in opened)


def test_attempts_pass_infers_collected_from_five_confirmed_absences(spool):
    """The receipt signal (§6): the result name is confirmed ABSENT, no hold
    remains, no claim, no pending — and casa did not itself delete it, which
    an OPEN attempt proves, since every casa deletion is write-ahead-terminal
    BEFORE it happens. Only then is the flow recorded `collected`."""
    now = time.time()
    h, _claim = _publish(spool, "collected-flow")
    os.unlink(_results(spool) / f"{h}.json")

    report = spool.attempts_pass(now=now, boot=True)

    assert report.collected == 1
    rec = _attempt_of(spool, h)
    assert rec["status"] == "done" and rec["outcome"] == "collected"
    assert rec["ended_ts"] == now
    assert rec["next_nudge_ts"] is None, "a collected flow never nudges"


def _trace_inference_probes(monkeypatch, seq: list, unknown=None):
    """Record the §6 probe sequence of the INFERENCE phase alone, optionally
    forcing one probe UNKNOWN.

    Probes are named ``("<dir>", "<entry>")`` and ``("collect", "*")``; the
    recorder is armed only while ``_infer_receipts`` runs, because the
    re-derivation phase probes the same names earlier in the pass and the
    order under test is the inference's."""
    real_probe = cs.CallbackSpool._probe
    real_collect = cs.CallbackSpool._probe_collect
    real_infer = cs.CallbackSpool._infer_receipts
    armed: list[bool] = []

    def _where(dir_fd) -> str:
        try:
            return os.path.basename(os.readlink(f"/proc/self/fd/{dir_fd}"))
        except OSError:                      # pragma: no cover
            return ""

    def probe(name, dir_fd):
        if not armed:
            return real_probe(name, dir_fd)
        key = (_where(dir_fd), name)
        seq.append(key)
        return None if key == unknown else real_probe(name, dir_fd)

    def probe_collect(h, results_fd):
        if not armed:
            return real_collect(h, results_fd)
        seq.append(("collect", "*"))
        return None if unknown == ("collect", "*") \
            else real_collect(h, results_fd)

    def infer(self, *a, **kw):
        armed.append(True)
        try:
            return real_infer(self, *a, **kw)
        finally:
            armed.pop()

    monkeypatch.setattr(cs.CallbackSpool, "_probe", staticmethod(probe))
    monkeypatch.setattr(cs.CallbackSpool, "_probe_collect",
                        staticmethod(probe_collect))
    monkeypatch.setattr(cs.CallbackSpool, "_infer_receipts", infer)


def test_the_five_probes_run_in_order_and_any_unknown_defers_at_its_place(
        spool, monkeypatch):
    """The normative §6 probe ORDER, and the three-state discipline of EVERY
    probe (Sol 6 — the happy-path pin alone would survive a reordered or
    absence-assuming inference).

    Probes 1-4 are I/O and run in the spec's order, short-circuiting: forcing
    any one UNKNOWN stops the inference exactly there — the later probes are
    never consulted and nothing is settled, because `collected` is only ever
    recorded from CONFIRMED absences. Probe 5 ("casa did not itself delete
    the artifacts") is structural: an OPEN attempt IS its proof, since every
    casa deletion writes its terminal outcome first — so a record that is
    already `done` never reaches a probe at all."""
    now = time.time()
    h, _claim = _publish(spool, "probe-order")
    os.unlink(_results(spool) / f"{h}.json")
    order = [("results", f"{h}.json"), ("collect", "*"),
             (".claims", h), ("pending", f"{h}.json")]

    for i, target in enumerate(order):
        seq: list = []
        _trace_inference_probes(monkeypatch, seq, unknown=target)
        report = spool.attempts_pass(now=now, boot=True)
        monkeypatch.undo()

        assert report.collected == 0, target
        assert _attempt_of(spool, h)["status"] == "result_ready", target
        assert seq == order[:i + 1], target

    # Every probe confirmed absent: the flow settles, having consulted all
    # four in order.
    seq = []
    _trace_inference_probes(monkeypatch, seq)
    report = spool.attempts_pass(now=now, boot=True)
    monkeypatch.undo()

    assert report.collected == 1 and seq == order
    assert _attempt_of(spool, h)["outcome"] == "collected"

    # Probe 5: the settled record is terminal, so the arm is unreachable for
    # it — no probe runs, and nothing is re-settled.
    seq = []
    _trace_inference_probes(monkeypatch, seq)
    again = spool.attempts_pass(now=now + 1, boot=True)
    monkeypatch.undo()

    assert again.collected == 0 and seq == []
    assert _attempt_of(spool, h)["outcome"] == "collected"


def test_inference_is_blocked_by_a_live_claim(spool):
    """Probe 3: attempt-first publishing makes "attempt says result_ready,
    claim still live, result never linked" a real crash state — the claim's
    presence routes the flow to recovery instead of mislabelling it."""
    now = time.time()
    h, _claim = _publish(spool, "blocked-claim")
    os.unlink(_results(spool) / f"{h}.json")
    _put(_claims(spool) / h, now - 10)

    report = spool.attempts_pass(now=now, boot=True)

    assert report.collected == 0
    assert _attempt_of(spool, h)["status"] == "result_ready"


def test_inference_is_blocked_by_a_live_pending(spool):
    """Probe 4: recovery may have RESTORED a crashed claim to pending, and a
    live pending means the flow rewound, not completed."""
    now = time.time()
    h, _claim = _publish(spool, "blocked-pending")
    os.unlink(_results(spool) / f"{h}.json")
    _put(_pending(spool) / f"{h}.json", now - 10, '{"v": 2, "meta": null}')

    report = spool.attempts_pass(now=now, boot=True)

    assert report.collected == 0
    assert _attempt_of(spool, h)["outcome"] is None


def test_a_result_ready_attempt_with_a_live_pending_is_rewound(spool):
    """Terra r3 pin: the re-derivation rule does not merely BLOCK the
    inference — it rewrites the record back to `awaiting_redirect`, because
    the artifacts say the flow is waiting for a redirect again."""
    now = time.time()
    h, _claim = _publish(spool, "rewound")
    os.unlink(_results(spool) / f"{h}.json")
    _put(_pending(spool) / f"{h}.json", now - 10, '{"v": 2, "meta": null}')

    report = spool.attempts_pass(now=now, boot=True)

    assert report.reopened == 1 and report.collected == 0
    rec = _attempt_of(spool, h)
    assert rec["status"] == "awaiting_redirect" and rec["outcome"] is None


def test_a_live_hold_raises_claimed_on_the_open_attempt(spool):
    """Spec §6: while a hold lives the attempt stays OPEN, but the `claimed`
    flag is what "tells its next life the payload may or may not have been
    seen" — so it must be raised the moment the hold exists, not only when
    the flow finally terminalizes. A successor consumer reading
    `result_ready, claimed: false` would call `collect()`, take the ENOENT
    (retryable, never ackable, §7) and retry until the hold ages out instead
    of learning its predecessor already renamed the result.

    The raise is a MINIMAL update, not a rebuild: the worker's durable
    schedule state (§8) survives, so a redelivery budget is never reset by a
    reconcile."""
    now = time.time()
    h, _claim = _publish(spool, "hold-raises-claimed")
    assert spool.update_attempt_nudge(PLUGIN, h, nudges=2,
                                      next_nudge_ts=now + 300) is True
    _rec, held = cs.collect(_pdir(spool), h)

    report = spool.attempts_pass(now=now, boot=True)

    assert report.claimed_raised == 1 and report.collected == 0
    rec = _attempt_of(spool, h)
    assert rec["status"] == "result_ready" and rec["outcome"] is None
    assert rec["claimed"] is True
    assert (rec["nudges"], rec["next_nudge_ts"]) == (2, now + 300), \
        "raising the flag must not rebuild the record from artifacts"
    assert held.exists(), "the held file belongs to its holder"

    again = spool.attempts_pass(now=now, boot=True)
    assert again.claimed_raised == 0, "the raise is not re-applied"
    assert _attempt_of(spool, h)["claimed"] is True


def test_a_result_ready_attempt_without_a_hold_keeps_claimed_false(spool):
    """The companion pin: `claimed` follows the hold witness and nothing
    else. A published result nobody has renamed leaves the flag alone —
    only a `.collect-<h>-*` entry proves the rename happened."""
    now = time.time()
    h, _claim = _publish(spool, "no-hold-no-claim")

    report = spool.attempts_pass(now=now, boot=True)

    assert report.claimed_raised == 0
    rec = _attempt_of(spool, h)
    assert rec["status"] == "result_ready" and rec["claimed"] is False


def test_inference_is_blocked_by_a_live_hold(spool):
    """Probe 2: while a `.collect-*` entry exists the attempt stays open —
    "claimed, unconfirmed": the consumer renamed but may have died before
    reading, and only its ack settles the flow."""
    now = time.time()
    h, _claim = _publish(spool, "blocked-hold")
    os.rename(_results(spool) / f"{h}.json",
              _results(spool) / f".collect-{h}-{'a' * 32}")

    report = spool.attempts_pass(now=now, boot=True)

    assert report.collected == 0
    assert _attempt_of(spool, h)["status"] == "result_ready"


def test_a_terminal_attempt_beside_a_live_result_is_reopened(spool):
    """Amendment 6: a terminal record coexisting with ANY live artifact of
    its hash is PROVISIONAL — the artifact witnesses the contradiction and
    the record is rewritten open."""
    now = time.time()
    h, claim = _publish(spool, "reopen-result")
    _terminal(spool, h, "expired_unread", ended=now - 5)

    report = spool.attempts_pass(now=now, boot=True)

    assert report.reopened == 1
    rec = _attempt_of(spool, h)
    assert rec["status"] == "result_ready" and rec["outcome"] is None
    assert rec["ended_ts"] is None
    assert rec["minted_ts"] == claim.mtime


def test_a_terminal_attempt_beside_a_live_hold_is_reopened_claimed(spool):
    """Same rule through the other witness: a surviving hold reopens the
    record AND asserts `claimed` — the rename provably happened."""
    now = time.time()
    h, _claim = _publish(spool, "reopen-hold")
    os.rename(_results(spool) / f"{h}.json",
              _results(spool) / f".collect-{h}-{'b' * 32}")
    _terminal(spool, h, "expired", ended=now - 5)

    report = spool.attempts_pass(now=now, boot=True)

    assert report.reopened == 1
    rec = _attempt_of(spool, h)
    assert rec["status"] == "result_ready" and rec["outcome"] is None
    assert rec["claimed"] is True


def test_an_unknown_result_probe_defers_the_inference(spool, monkeypatch):
    """Proved absence, not assumed absence: the probes are three-state, and
    ANY unknown (EIO/EACCES/…) defers the whole inference to the next pass —
    `collected` is only ever recorded from confirmed ENOENTs."""
    now = time.time()
    h, _claim = _publish(spool, "unknowable")
    os.unlink(_results(spool) / f"{h}.json")
    real = cs._marker_lstat

    def flaky(name, dir_fd):
        try:
            where = os.readlink(f"/proc/self/fd/{dir_fd}")
        except OSError:                      # pragma: no cover
            where = ""
        if name == f"{h}.json" and where.endswith("/results"):
            return cs._LSTAT_ERROR
        return real(name, dir_fd)

    monkeypatch.setattr(cs, "_marker_lstat", flaky)
    report = spool.attempts_pass(now=now, boot=True)
    monkeypatch.undo()

    assert report.collected == 0
    assert _attempt_of(spool, h)["status"] == "result_ready"


def test_a_periodic_pass_skips_an_in_flight_hash_a_boot_pass_does_not(spool):
    """Same rule as recovery: a handler between claim and publish runs in
    THIS process, so a periodic pass must not settle its flow from an
    artifact state the handler is still building. A boot pass has no live
    handlers by construction."""
    now = time.time()
    h, _claim = _publish(spool, "still-in-flight")
    os.unlink(_results(spool) / f"{h}.json")
    spool._in_flight.add(cs.in_flight_key(PLUGIN, h))

    periodic = spool.attempts_pass(now=now, boot=False)

    assert periodic.collected == 0
    assert _attempt_of(spool, h)["status"] == "result_ready"

    at_boot = spool.attempts_pass(now=now, boot=True)

    assert at_boot.collected == 1
    assert _attempt_of(spool, h)["outcome"] == "collected"


def test_an_invalid_attempt_beside_a_live_result_is_rewritten_open(spool):
    """A consumer-scribbled record is never truth: it is rewritten from the
    artifacts that survive, never treated as an ack and never a reason to
    delete anything."""
    now = time.time()
    h, claim = _publish(spool, "scribbled")
    (_attempts(spool) / f"{h}.json").write_text('{"scribbled": true}')

    report = spool.attempts_pass(now=now, boot=True)

    rec = _attempt_of(spool, h)
    assert rec["status"] == "result_ready" and rec["outcome"] is None
    assert rec["minted_ts"] == claim.mtime, "rebuilt from the result record"
    assert report.materialized == 1 and report.anomalies == []
    assert (_results(spool) / f"{h}.json").exists(), "no artifact was deleted"


def test_an_invalid_attempt_with_no_artifacts_is_retired_with_an_anomaly(
        spool):
    """Fail-closed direction: with nothing left to re-derive from, the
    unreadable file is RETIRED and the loss recorded as an anomaly — never
    read as a receipt, never given a terminal outcome it cannot justify."""
    now = time.time()
    h = state_hash("lost")
    (_attempts(spool) / f"{h}.json").write_text("not json at all")

    report = spool.attempts_pass(now=now, boot=True)

    assert spool.read_attempt(PLUGIN, h).state is cs.MarkerState.ABSENT
    assert report.anomalies and report.materialized == 0
    assert report.collected == 0
    assert os.listdir(_attempts(spool)) == [], "not turned into an ack token"


def _forge_identity(spool, h: str, **fields) -> dict:
    """Rewrite `attempts/<h>.json` so it embeds ANOTHER flow's `state_hash`
    (plus any other field), the way a scribbling consumer or a mis-merged
    backup would. Everything else about the record stays schema-perfect."""
    forged = dict(_attempt_of(spool, h), **fields)
    (_attempts(spool) / f"{h}.json").write_bytes(
        cs.canonical_marker_bytes(forged))
    return forged


def test_a_record_naming_another_flow_is_invalid_and_re_derived(spool):
    """Red case (Sol 4): the FILENAME is the flow's identity. A schema-perfect
    `attempts/<A>.json` whose `state_hash` is B is not A's record — it is a
    record of nothing — so it must never be readable as truth, and the worker
    surface must never carry B's identity under A's name. It is INVALID, and
    the re-derivation rule rebuilds A's record from A's artifacts."""
    now = time.time()
    h, claim = _publish(spool, "identity-bound")
    other = state_hash("some-other-flow")
    _forge_identity(spool, h, state_hash=other, meta={"b": "victim"})

    assert spool.list_attempts(PLUGIN) == [], "never truth"
    assert spool.list_invalid_attempts(PLUGIN) == [h]
    assert spool.update_attempt_nudge(PLUGIN, h, nudges=1) is False, \
        "no worker bookkeeping merges into another flow's record"

    report = spool.attempts_pass(now=now, boot=True)

    assert report.materialized == 1
    rec = _attempt_of(spool, h)
    assert rec["state_hash"] == h and rec["meta"] is None
    assert rec["minted_ts"] == claim.mtime, "rebuilt from A's own artifacts"


def _scribble_attempt(spool, h: str, rec: dict, plugin=PLUGIN) -> None:
    """Write `attempts/<h>.json` the way a scribbling consumer would — with
    json's DEFAULT `allow_nan=True`, so the file can carry the non-standard
    `NaN`/`Infinity` literals that casa's own canonical writer refuses and
    its marker reader nonetheless parses."""
    (_attempts(spool, plugin) / f"{h}.json").write_text(json.dumps(rec))


def test_a_non_finite_meta_is_invalid_and_the_nudge_budget_advances(spool):
    """Red case (re-review 2): `_safe_meta` returned every SCALAR unexamined,
    so a scribbled `meta: NaN` type-checked its way onto the worker's read
    surface. The dispatch that followed was accepted by the bus — and the
    bookkeeping write behind it was then refused by the `allow_nan=False`
    writer, so `nudges`/`next_nudge_ts` never advanced and the attempt stayed
    due on every pass: INV-CB-008's bounded redelivery broken through the one
    field a consumer authors.

    A record the writer cannot re-emit is INVALID instead — never truth, no
    bookkeeping merged into it — so the re-derivation rule rebuilds it from
    the live artifacts and the budget then ADVANCES."""
    now = time.time()
    h, claim = _publish(spool, "non-finite-meta")
    _scribble_attempt(spool, h, dict(_attempt_of(spool, h),
                                     meta=float("nan")))

    assert spool.list_attempts(PLUGIN) == [], "never truth"
    assert spool.list_invalid_attempts(PLUGIN) == [h]
    assert spool.update_attempt_nudge(PLUGIN, h, nudges=1) is False, \
        "no bookkeeping merges into a record no writer can emit"

    report = spool.attempts_pass(now=now, boot=True)

    assert report.materialized == 1
    rec = _attempt_of(spool, h)
    assert rec["meta"] is None and rec["minted_ts"] == claim.mtime
    assert rec["nudges"] == 0
    # The write the non-finite record would have blocked forever goes through.
    assert spool.update_attempt_nudge(PLUGIN, h, nudges=1,
                                      last_nudge_ts=now) is True
    assert _attempt_of(spool, h)["nudges"] == 1


def test_a_forged_identity_never_reaches_a_write_ahead_outcome(spool):
    """The same binding at the sharpest boundary: the record a DELETION
    depends on. Deriving the write-ahead outcome from a file that names
    another flow would stamp B's identity and B's binding onto the outcome
    written under A's name — the consumer's read surface for a credential
    casa is about to destroy."""
    now = time.time()
    h, _claim = _publish(spool, "forged-write-ahead")
    other = state_hash("forged-victim")
    _forge_identity(spool, h, state_hash=other, meta={"b": "victim"})
    _utime(_results(spool) / f"{h}.json", now - RESULT_TTL_S - 10)

    report = spool.sweep(now=now)

    assert report.deleted_results == 1
    rec = _attempt_of(spool, h)
    assert rec["state_hash"] == h, "the outcome is recorded for THIS flow"
    assert rec["outcome"] == "expired_unread" and rec["meta"] is None


def test_terminal_attempts_age_out_at_the_retention_bound(spool):
    """An attempt is durable until ack OR the retention bound — the file IS
    the record being retired at its own bound, so no write-ahead applies."""
    now = time.time()
    old_h, fresh_h = state_hash("old-record"), state_hash("fresh-record")
    _terminal(spool, old_h, "expired_unread",
              ended=now - ca.ATTEMPT_RETENTION_S - 1)
    _terminal(spool, fresh_h, "expired_unread", ended=now - 60)

    report = spool.attempts_pass(now=now, boot=True)

    assert report.aged_out == 1
    assert spool.read_attempt(PLUGIN, old_h).state is cs.MarkerState.ABSENT
    assert _attempt_of(spool, fresh_h)["outcome"] == "expired_unread"


def test_the_attempt_cap_deletes_the_oldest_terminal_first(spool, monkeypatch):
    """§9 eviction order: oldest-`ended_ts` TERMINAL files first. An ack
    token is the consumer's receipt in flight — never counted, never
    evicted."""
    now = time.time()
    monkeypatch.setattr(cs, "MAX_ATTEMPTS", 3)
    hashes = []
    for i in range(4):
        h = state_hash(f"cap-att-{i}")
        _terminal(spool, h, "expired", ended=now - 100 + i)
        hashes.append(h)
    # A receipt the ack phase DEFERS (its flow is in flight), so the token is
    # still there when the bounds run: what this pins is the cap ladder, which
    # neither counts nor evicts receipts — consuming one is the other rule.
    receipt = state_hash("receipt")
    token = _put(_attempts(spool) / f"{cs.ACK_PREFIX}{receipt}", now, "")
    spool._in_flight.add(cs.in_flight_key(PLUGIN, receipt))

    report = spool.attempts_pass(now=now, boot=False)

    assert report.acks_consumed == 0
    assert report.capped == 1
    assert spool.read_attempt(PLUGIN, hashes[0]).state is cs.MarkerState.ABSENT
    assert all(spool.read_attempt(PLUGIN, h).state is cs.MarkerState.PRESENT
               for h in hashes[1:])
    assert token.exists(), "a receipt is never counted toward the cap"


def test_the_open_attempt_valve_is_strict_and_skips_an_undurable_eviction(
        spool, monkeypatch):
    """Amendment 10: the pathological-overflow valve terminalizes `evicted`
    STRICTLY first and removes the file only when that is proven durable —
    so what the cap destroys is always a terminal record, never an open flow
    whose outcome was lost with it. The next pass converges."""
    now = time.time()
    monkeypatch.setattr(cs, "MAX_ATTEMPTS", 1)
    hashes = []
    for i in range(2):
        h = state_hash(f"valve-{i}")
        assert spool.write_attempt(PLUGIN, h, ca.new_attempt(
            state_hash=h, minted_ts=now - 100 + i,
            status="awaiting_redirect", now=now)) is True
        hashes.append(h)
    real = cs._fsync_strict

    def boom(fd, what):
        raise cs.FsyncFailed(errno.EIO, "injected")

    monkeypatch.setattr(cs, "_fsync_strict", boom)
    first = spool.attempts_pass(now=now, boot=True)

    assert first.skipped_undurable == 1 and first.capped == 0
    assert spool.read_attempt(PLUGIN, hashes[0]).state is cs.MarkerState.PRESENT

    monkeypatch.setattr(cs, "_fsync_strict", real)
    second = spool.attempts_pass(now=now, boot=True)

    assert second.capped == 1 and second.skipped_undurable == 0
    assert spool.read_attempt(PLUGIN, hashes[0]).state is cs.MarkerState.ABSENT
    assert spool.read_attempt(PLUGIN, hashes[1]).state is cs.MarkerState.PRESENT


def test_a_confirmed_absent_result_can_never_be_recreated(spool):
    """Amendment-15 refutation pin — why `collected` can never be a false
    positive. Results are publish-once (`link(2)`'s EEXIST) and NO casa path
    recreates `results/<h>.json`: the pending was consumed by the first
    claim and the claim itself is gone, so a replayed redirect loses at
    claim() and never reaches publish_result. Once casa has observed the
    base name absent, a consumer rename of it is impossible — "collected
    recorded while the consumer holds a credential casa doesn't know about"
    would require the file to reappear, which nothing can do."""
    now = time.time()
    h, _claim = _publish(spool, "once-only")
    os.unlink(_results(spool) / f"{h}.json")

    assert spool.attempts_pass(now=now, boot=True).collected == 1

    assert spool.claim(PLUGIN, h, now=now) is None, "the pending is consumed"
    assert not (_results(spool) / f"{h}.json").exists()
    assert _attempt_of(spool, h)["outcome"] == "collected"


def test_attempts_pass_is_a_noop_on_a_closed_spool(tmp_path):
    s = CallbackSpool(tmp_path / "callbacks")
    s.ensure_plugin_dirs(PLUGIN)
    s.close()
    report = s.attempts_pass(now=time.time(), boot=True)
    assert (report.materialized, report.collected, report.reopened,
            report.aged_out, report.capped) == (0, 0, 0, 0, 0)


def test_an_unknown_pending_probe_defers_both_the_rewind_and_the_inference(
        spool, monkeypatch):
    """Probe 4's independent binding. A live pending normally REWINDS the
    record (which alone would also stop the inference) — but when the
    pending's state is UNKNOWN the rewind cannot fire either, and only the
    probe's own three-state discipline keeps the flow from being settled
    `collected` while a pending may exist."""
    now = time.time()
    h, _claim = _publish(spool, "unknown-pending")
    os.unlink(_results(spool) / f"{h}.json")
    _put(_pending(spool) / f"{h}.json", now - 10, '{"v": 2, "meta": null}')
    real = cs._marker_lstat

    def flaky(name, dir_fd):
        try:
            where = os.readlink(f"/proc/self/fd/{dir_fd}")
        except OSError:                      # pragma: no cover
            where = ""
        if name == f"{h}.json" and where.endswith("/pending"):
            return cs._LSTAT_ERROR
        return real(name, dir_fd)

    monkeypatch.setattr(cs, "_marker_lstat", flaky)
    report = spool.attempts_pass(now=now, boot=True)
    monkeypatch.undo()

    assert report.collected == 0 and report.reopened == 0
    assert _attempt_of(spool, h)["status"] == "result_ready"


# ---------------------------------------------------------------------------
# ack consumption (§7) — the consumer's receipt supersedes every record, so
# consuming an `.ack-<h>` token retires EVERY artifact of that hash and the
# token itself strictly LAST, only after a three-state proof that they are
# gone. The ordering (result before the `.collect-*` enumeration) is what
# makes a collector rename racing the teardown converge rather than strand
# bytes, and every deferral arm (in-flight, UNKNOWN probe, failed fsync)
# KEEPS the token — a rename is durable, so the next pass finishes the job.
# ---------------------------------------------------------------------------


def _unlink_spy(monkeypatch, seq, *, after=None):
    """Record every ``_unlink_quiet`` as ``(<dir basename>, <entry name>)``.

    The teardown's whole point is an ORDER, and the same entry name appears
    in three directories (``<h>.json`` is a pending, a result and an attempt),
    so the recorder resolves each dir FD through ``/proc/self/fd``. *after* is
    an optional ``(dir, name) -> None`` hook fired immediately AFTER the real
    unlink — how a racing collector is injected at an exact instant.
    """
    real = cs._unlink_quiet

    def spy(name, dir_fd):
        try:
            where = os.readlink(f"/proc/self/fd/{dir_fd}")
        except OSError:                      # pragma: no cover
            where = ""
        where = os.path.basename(where)
        seq.append((where, name))
        out = real(name, dir_fd)
        if after is not None:
            after(where, name)
        return out

    monkeypatch.setattr(cs, "_unlink_quiet", spy)


def test_ack_consumption_tears_down_every_artifact_with_the_token_last(
        spool, monkeypatch):
    """The full-flow teardown in its normative order: pending, claim, claim
    temp and result FIRST, then the `.collect-*` enumeration, then the attempt
    file — and the token only after all of it is proven gone. Nothing is
    recorded: the ack IS the record (INV-CB-007 arm (a))."""
    now = time.time()
    h, _claim = _publish(spool, "acked-flow")
    hold = _results(spool) / f".collect-{h}-{'c' * 32}"
    hold.write_text('{"code": "x"}')
    assert cs.ack(_pdir(spool), h) is True
    seq: list[tuple[str, str]] = []
    _unlink_spy(monkeypatch, seq)

    report = spool.attempts_pass(now=now, boot=True)
    monkeypatch.undo()

    assert report.acks_consumed == 1 and report.acks_deferred == 0
    assert not (_results(spool) / f"{h}.json").exists() and not hold.exists()
    assert os.listdir(_attempts(spool)) == [], "no record survives the receipt"
    assert os.listdir(_pending(spool)) == []
    assert os.listdir(_claims(spool)) == []
    i_pend = seq.index(("pending", f"{h}.json"))
    i_claim = seq.index((".claims", h))
    i_temp = seq.index((".claims", f"{cs.TEMP_PREFIX}{h}"))
    i_result = seq.index(("results", f"{h}.json"))
    i_hold = seq.index(("results", hold.name))
    i_attempt = seq.index(("attempts", f"{h}.json"))
    i_token = seq.index(("attempts", f"{cs.ACK_PREFIX}{h}"))
    assert i_pend < i_claim < i_temp < i_result < i_hold < i_attempt < i_token
    assert i_token == len(seq) - 1, "the token is the LAST thing removed"


def test_a_premature_ack_leaves_no_orphan_to_re_materialize(spool):
    """Terra r2: a consumer that acks while the result is still live must not
    leave an orphan — without the teardown the result would be materialized
    into a fresh attempt and nudged forever. The artifacts die with the ack,
    and a second pass has nothing to rebuild."""
    now = time.time()
    h, _claim = _publish(spool, "premature-ack")
    assert cs.ack(_pdir(spool), h) is True

    first = spool.attempts_pass(now=now, boot=True)

    assert first.acks_consumed == 1
    assert not (_results(spool) / f"{h}.json").exists()

    second = spool.attempts_pass(now=now + 1, boot=True)

    assert second.materialized == 0 and second.collected == 0
    assert second.acks_consumed == 0
    assert os.listdir(_attempts(spool)) == []
    assert spool.read_attempt(PLUGIN, h).state is cs.MarkerState.ABSENT


def test_an_ack_on_an_open_attempt_aborts_the_pending(spool):
    """The consumer-abort verb: acking an `awaiting_redirect` attempt kills
    the pending state instead of letting it expire noisily later."""
    now = time.time()
    state = "aborted-flow"
    h = state_hash(state)
    mint(_pdir(spool), state, meta={"kind": "renewal"})
    assert spool.attempts_pass(now=now, boot=True).materialized == 1
    assert _attempt_of(spool, h)["status"] == "awaiting_redirect"
    assert cs.ack(_pdir(spool), h) is True

    report = spool.attempts_pass(now=now, boot=True)

    assert report.acks_consumed == 1
    assert not (_pending(spool) / f"{h}.json").exists()
    assert os.listdir(_attempts(spool)) == []


def test_a_teardown_fsync_failure_keeps_the_token_until_it_completes(
        spool, monkeypatch):
    """Power-loss convergence: an unprovable teardown must never consume the
    receipt. The artifacts may already be gone, may roll back — the durable
    token is what guarantees the next pass finishes, so it survives every
    failed strict fsync."""
    now = time.time()
    h, _claim = _publish(spool, "undurable-teardown")
    (_results(spool) / f".collect-{h}-{'d' * 32}").write_text("{}")
    assert cs.ack(_pdir(spool), h) is True
    real = cs._fsync_strict

    def boom(fd, what):
        raise cs.FsyncFailed(errno.EIO, "injected")

    monkeypatch.setattr(cs, "_fsync_strict", boom)
    first = spool.attempts_pass(now=now, boot=True)

    assert first.acks_consumed == 0 and first.acks_deferred == 1
    assert (_attempts(spool) / f"{cs.ACK_PREFIX}{h}").exists()

    # The pass that DOES consume the token must witness durability itself —
    # every artifact directory, strictly — not inherit it from the pass whose
    # fsyncs failed. Pass 1 unlinked artifacts it could not prove durable, so
    # a pass 2 that fsyncs only what IT changed (nothing) would delete the
    # receipt on the strength of no witness at all (Sol 1).
    witnessed: list[str] = []

    def witness(fd, what):
        witnessed.append(what)
        return real(fd, what)

    monkeypatch.setattr(cs, "_fsync_strict", witness)
    second = spool.attempts_pass(now=now + 1, boot=True)

    assert second.acks_consumed == 1 and second.acks_deferred == 0
    assert set(witnessed) >= {cs.PENDING_DIR, cs.CLAIMS_DIR, cs.RESULTS_DIR,
                              cs.ATTEMPTS_DIR}, \
        "every artifact dir is proven durable BEFORE the token is consumed"
    assert os.listdir(_attempts(spool)) == []
    assert os.listdir(_results(spool)) == []


def test_a_teardown_pass_that_changed_nothing_still_proves_its_dirs_durable(
        spool, monkeypatch):
    """Red case (Sol 1): the token may only be consumed on a witness that
    covers EVERY pass's deletions, not just this one's.

    Pass 1 unlinks the result and the `results/` fsync fails, so the token
    correctly survives. Pass 2 finds the name already absent (page cache),
    unlinks nothing — and a teardown that fsyncs only what it CHANGED
    therefore proves nothing, sees six confirmed ENOENTs and deletes the
    receipt. A power loss then rolls pass 1's unlink back and an unreceipted
    credential artifact resurrects with no token left to drive its teardown.
    While the fault persists the token must stay; when it clears, the pass
    that consumes the token is the one that proved the directories durable."""
    now = time.time()
    h, _claim = _publish(spool, "unproven-earlier-pass")
    assert cs.ack(_pdir(spool), h) is True
    real = cs._fsync_strict

    def only_results(fd, what):
        if what == cs.RESULTS_DIR:
            raise cs.FsyncFailed(errno.EIO, "injected")
        return real(fd, what)

    monkeypatch.setattr(cs, "_fsync_strict", only_results)
    first = spool.attempts_pass(now=now, boot=True)

    assert first.acks_consumed == 0 and first.acks_deferred == 1
    assert not (_results(spool) / f"{h}.json").exists(), \
        "pass 1 DID unlink — only its durability is unproven"

    second = spool.attempts_pass(now=now + 1, boot=True)

    assert second.acks_consumed == 0 and second.acks_deferred == 1, \
        "a pass with nothing to unlink still re-proves every directory"
    assert (_attempts(spool) / f"{cs.ACK_PREFIX}{h}").exists()

    monkeypatch.setattr(cs, "_fsync_strict", real)
    third = spool.attempts_pass(now=now + 2, boot=True)

    assert third.acks_consumed == 1
    assert os.listdir(_attempts(spool)) == []


def test_an_unknown_probe_keeps_the_ack_token(spool, monkeypatch):
    """Proved absence, not assumed absence — on the teardown's proof step
    too: an artifact whose presence cannot be READ is never counted as gone,
    so the token stays and the next pass re-proves it."""
    now = time.time()
    h, _claim = _publish(spool, "unknowable-teardown")
    assert cs.ack(_pdir(spool), h) is True
    real = cs._marker_lstat

    def flaky(name, dir_fd):
        try:
            where = os.readlink(f"/proc/self/fd/{dir_fd}")
        except OSError:                      # pragma: no cover
            where = ""
        if name == f"{h}.json" and where.endswith("/results"):
            return cs._LSTAT_ERROR
        return real(name, dir_fd)

    monkeypatch.setattr(cs, "_marker_lstat", flaky)
    first = spool.attempts_pass(now=now, boot=True)
    monkeypatch.undo()

    assert first.acks_consumed == 0 and first.acks_deferred == 1
    assert (_attempts(spool) / f"{cs.ACK_PREFIX}{h}").exists()

    second = spool.attempts_pass(now=now + 1, boot=True)

    assert second.acks_consumed == 1
    assert os.listdir(_attempts(spool)) == []


def test_a_collector_racing_the_teardown_is_caught_by_the_enumeration(
        spool, monkeypatch):
    """Sol r6's ordering rule, at the instant it exists for: a consumer's
    `collect` rename lands right after the result unlink. Because the
    `.collect-*` enumeration runs AFTER that unlink, the bytes the rename
    moved are seen and removed in the SAME pass — the token is consumed with
    nothing stranded."""
    now = time.time()
    h, _claim = _publish(spool, "raced-teardown")
    assert cs.ack(_pdir(spool), h) is True
    raced = _results(spool) / f".collect-{h}-{'e' * 32}"
    seq: list[tuple[str, str]] = []
    fired: list[bool] = []

    def after(where, name):
        if where == "results" and name == f"{h}.json" and not fired:
            fired.append(True)
            raced.write_text('{"code": "x"}')

    _unlink_spy(monkeypatch, seq, after=after)
    report = spool.attempts_pass(now=now, boot=True)
    monkeypatch.undo()

    assert fired, "the race must have been injected"
    assert report.acks_consumed == 1
    assert not raced.exists()
    assert os.listdir(_results(spool)) == []
    assert os.listdir(_attempts(spool)) == []
    assert seq.index(("results", raced.name)) \
        > seq.index(("results", f"{h}.json"))


def test_a_periodic_pass_defers_an_in_flight_ack_a_boot_pass_consumes_it(
        spool):
    """Terra r3 guard (a): a consumer can abort while a handler sits between
    claim and publish, and that handler is building artifact state in THIS
    process. The teardown defers — the token is durable — and the next pass
    kills even the result the racing publisher managed to publish."""
    now = time.time()
    h, _claim = _publish(spool, "in-flight-ack")
    assert cs.ack(_pdir(spool), h) is True
    spool._in_flight.add(cs.in_flight_key(PLUGIN, h))

    periodic = spool.attempts_pass(now=now, boot=False)

    assert periodic.acks_consumed == 0 and periodic.acks_deferred == 0
    assert (_attempts(spool) / f"{cs.ACK_PREFIX}{h}").exists()
    assert (_results(spool) / f"{h}.json").exists()

    at_boot = spool.attempts_pass(now=now + 1, boot=True)

    assert at_boot.acks_consumed == 1
    assert os.listdir(_results(spool)) == []
    assert os.listdir(_attempts(spool)) == []


def test_an_attempt_resurrected_by_a_replace_dies_with_the_token(spool):
    """Amendment 15 / spec §7's reason for ack-by-RENAME: casa's staged
    replace can re-create `attempts/<h>.json` after the consumer acked it.
    An unlink-based ack would be silently undone; the token survives the
    replace and the teardown removes the resurrected record with it."""
    now = time.time()
    h, claim = _publish(spool, "resurrected")
    assert cs.ack(_pdir(spool), h) is True
    resurrected = ca.new_attempt(state_hash=h, minted_ts=claim.mtime,
                                 status="result_ready", now=now)
    assert spool.write_attempt(PLUGIN, h, resurrected) is True
    assert (_attempts(spool) / f"{h}.json").exists()

    report = spool.attempts_pass(now=now, boot=True)

    assert report.acks_consumed == 1
    assert os.listdir(_attempts(spool)) == [], \
        "the resurrected record dies with the token it could not erase"
    assert os.listdir(_results(spool)) == []


# ---------------------------------------------------------------------------
# removal records (`.removals/`) — spec §10, the documented INV-CB-007
# exception: the per-flow ledger dies with the plugin, so the abort's durable
# record is a root-level removal record, and the purge NEVER happens without
# it (plan amendment 11).
# ---------------------------------------------------------------------------


def _removals(spool) -> Path:
    return Path(spool.root) / ".removals"


def _removal_files(spool) -> list[str]:
    d = _removals(spool)
    if not d.is_dir():
        return []
    return sorted(n for n in os.listdir(d) if not n.startswith("."))


def _read_removal(spool, name: str) -> dict:
    return json.loads((_removals(spool) / name).read_text())


def _removal_rec(*, plugin: str = PLUGIN, count: int = 1,
                 reason: str = "remove", ts: float, noted: bool = False,
                 noted_ts: "float | None" = None) -> dict:
    return {"v": 1, "plugin": plugin, "count": count, "reason": reason,
            "ts": ts, "noted": noted, "noted_ts": noted_ts}


def _put_removal(spool, name: str, rec) -> Path:
    d = _removals(spool)
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_bytes(rec if isinstance(rec, bytes)
                  else cs.canonical_marker_bytes(rec))
    return p


def _fail_strict(monkeypatch) -> None:
    """Every strict fsync fails — the "record will not go durable" arm."""
    def boom(fd, what):
        raise cs.FsyncFailed(errno.EIO, "injected")
    monkeypatch.setattr(cs, "_fsync_strict", boom)


def test_remove_plugin_records_the_unsettled_union_then_purges(spool):
    """A live pending is one unsettled flow: the abort record goes down
    durably FIRST, then the dir is purged."""
    h = state_hash("live-1")
    _put(_pending(spool) / f"{h}.json", time.time())

    assert spool.remove_plugin(PLUGIN) is True

    assert not _pdir(spool).exists()
    names = _removal_files(spool)
    assert len(names) == 1 and names[0].startswith(f"{PLUGIN}-")
    rec = _read_removal(spool, names[0])
    assert rec["v"] == 1 and rec["plugin"] == PLUGIN
    assert rec["count"] == 1 and rec["reason"] == "remove"
    assert rec["noted"] is False and rec["noted_ts"] is None
    assert abs(rec["ts"] - time.time()) < 60


def test_remove_plugin_records_a_terminal_unacked_attempt(spool):
    """Sol r3 pin: a TERMINAL attempt nobody acked is an outcome the consumer
    has not read yet — it counts toward the union exactly like an open one."""
    now = time.time()
    h = state_hash("terminal-1")
    rec = ca.terminalize(_attempt_rec(h, now), "expired", now=now)
    assert spool.write_attempt(PLUGIN, h, rec) is True

    assert spool.remove_plugin(PLUGIN) is True

    names = _removal_files(spool)
    assert len(names) == 1
    assert _read_removal(spool, names[0])["count"] == 1


def test_an_acked_attempt_is_settled_and_needs_no_record(spool):
    """The ack SUPERSEDES the record (INV-CB-007 arm (a)), and it settles the
    FLOW rather than merely its ledger entry.

    Red case (Sol 5): the artifacts of a PREMATURELY acked flow — a live
    result and the consumer's hold — outlive the ack until the teardown pass
    runs, and a removal can beat that pass. Subtracting the receipt tokens
    from the attempts half only, then unioning the artifacts back in, tells
    the operator a flow was aborted that its consumer has already absorbed.
    The tokens come off the COMPLETE union."""
    h, _claim = _publish(spool, "acked-1")
    hold = _results(spool) / f".collect-{h}-{'f' * 32}"
    hold.write_text('{"code": "x"}')
    assert cs.ack(_pdir(spool), h) is True
    assert (_results(spool) / f"{h}.json").exists(), \
        "the teardown has NOT run yet — the artifacts are still there"

    assert spool.remove_plugin(PLUGIN) is True

    assert not _pdir(spool).exists()
    assert _removal_files(spool) == []


def _fail_listing(monkeypatch, sub: str) -> None:
    """Make ONE directory's listing fault (EIO) the way a failing disk does:
    the directory opens, the listing does not. Scoped by the FD's resolved
    basename so only the named artifact class is affected."""
    real = os.listdir

    def flaky(fd):
        if isinstance(fd, int):
            try:
                where = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:                  # pragma: no cover
                where = ""
            if os.path.basename(where) == sub:
                raise OSError(errno.EIO, "injected")
        return real(fd)

    monkeypatch.setattr(cs.os, "listdir", flaky)


def test_remove_plugin_defers_when_an_artifact_listing_is_unprovable(
        spool, monkeypatch):
    """Red case (Sol 2): "no purge without a record" must not fail OPEN on a
    scan error. A transient EIO while listing a directory that HAS live
    artifacts maps to an empty set, the union reads as zero, and the whole
    spool dir — credentials, ledger and all — is purged with NO removal
    record: the one outcome the §10 exception cannot absorb. An unprovable
    listing DEFERS instead; the orphan GC converges later."""
    h = state_hash("unprovable-1")
    _put(_pending(spool) / f"{h}.json", time.time())
    _fail_listing(monkeypatch, "pending")

    assert spool.remove_plugin(PLUGIN) is False

    monkeypatch.undo()
    assert _pdir(spool).is_dir(), "the plugin dir SURVIVES an unprovable scan"
    assert (_pending(spool) / f"{h}.json").exists()
    assert _removal_files(spool) == []

    # With the fault gone the same removal proceeds, and records the flow.
    assert spool.remove_plugin(PLUGIN) is True
    names = _removal_files(spool)
    assert len(names) == 1 and _read_removal(spool, names[0])["count"] == 1


def test_remove_plugin_defers_when_the_attempt_listing_is_unprovable(
        spool, monkeypatch):
    """The same hole through the ledger half: a terminal unacked attempt is an
    outcome the consumer has not read, and a listing that faults must not
    erase it from the count that authorizes the purge."""
    now = time.time()
    h = state_hash("unprovable-2")
    assert spool.write_attempt(
        PLUGIN, h, ca.terminalize(_attempt_rec(h, now), "expired",
                                  now=now)) is True
    _fail_listing(monkeypatch, "attempts")

    assert spool.remove_plugin(PLUGIN) is False

    monkeypatch.undo()
    assert _pdir(spool).is_dir()
    assert _removal_files(spool) == []


def test_a_failed_purge_logs_no_callback_identifier(spool, monkeypatch,
                                                    caplog):
    """Red case (Terra 2): `shutil.rmtree`'s OSError names the ENTRY it failed
    on, and under `results/` or `attempts/` that filename IS a state hash.
    Interpolating the raw exception puts a callback identifier on exactly the
    log surface INV-CB-006 keeps free of them — the class and errno are all a
    diagnostic may say."""
    h = state_hash("leaky-purge")
    _put(_results(spool) / f"{h}.json", time.time())

    def boom(path, dir_fd=None):
        raise OSError(errno.EACCES, "Permission denied",
                      f"{PLUGIN}/results/{h}.json")

    monkeypatch.setattr(cs.shutil, "rmtree", boom)
    caplog.set_level(logging.WARNING)
    assert spool.remove_plugin(PLUGIN) is False
    monkeypatch.undo()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert h not in text, "a state hash reached a log line"
    assert str(errno.EACCES) in text, "the errno IS what a diagnostic reports"


def test_remove_plugin_with_nothing_unsettled_writes_no_record(spool):
    assert spool.remove_plugin(PLUGIN) is True

    assert not _pdir(spool).exists()
    assert _removal_files(spool) == []
    assert not _removals(spool).exists(), "no record, no store"


def test_remove_plugin_skips_the_purge_without_a_durable_record(
        spool, monkeypatch):
    """Amendment 11 (Sol 1 = Terra 7): a removal with unsettled flows whose
    record will not go durable must NOT proceed — a purge with no record is
    the one outcome INV-CB-007's exception cannot absorb. The next orphan-GC
    pass converges."""
    h = state_hash("undurable-1")
    _put(_pending(spool) / f"{h}.json", time.time())
    _fail_strict(monkeypatch)

    assert spool.remove_plugin(PLUGIN) is False

    assert _pdir(spool).is_dir(), "the plugin dir SURVIVES"
    assert (_pending(spool) / f"{h}.json").exists()
    assert _removal_files(spool) == []


def test_orphan_gc_records_an_unacked_attempt_before_removing_the_dir(spool):
    """Spec §10 / Sol r2 B7: a quiescent orphan dir can still hold terminal
    attempts inside their retention — the GC degrades to the removal
    exception rather than silently violating the retention promise."""
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    h = state_hash("ghost-1")
    rec = ca.terminalize(
        ca.new_attempt(state_hash=h, minted_ts=now - 5.0, status="result_ready",
                       now=now), "expired_unread", now=now)
    assert spool.write_attempt("ghost", h, rec) is True
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)

    removed = spool.gc_orphan_dirs(registry_valid=True,
                                   member_plugins={PLUGIN}, now=now)

    assert removed == ["ghost"]
    assert not _pdir(spool, "ghost").exists()
    names = _removal_files(spool)
    assert len(names) == 1 and names[0].startswith("ghost-")
    record = _read_removal(spool, names[0])
    assert record["reason"] == "orphan_gc"
    assert record["plugin"] == "ghost" and record["count"] == 1
    assert record["ts"] == now


def test_orphan_gc_skips_a_dir_whose_record_will_not_go_durable(
        spool, monkeypatch):
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    h = state_hash("ghost-2")
    rec = ca.terminalize(
        ca.new_attempt(state_hash=h, minted_ts=now - 5.0, status="result_ready",
                       now=now), "expired", now=now)
    assert spool.write_attempt("ghost", h, rec) is True
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)
    _fail_strict(monkeypatch)

    removed = spool.gc_orphan_dirs(registry_valid=True,
                                   member_plugins={PLUGIN}, now=now)

    assert removed == []
    assert _pdir(spool, "ghost").is_dir(), "the dir stays for the next pass"
    assert _removal_files(spool) == []


def test_orphan_gc_defers_a_dir_whose_inventory_is_unprovable(
        spool, monkeypatch):
    """Sol 2's second site: the GC purges a quiescent dir that may still hold
    terminal-unacked outcomes, so its inventory is the same licence the
    removal's is — and the same EIO must defer it rather than read as
    "nothing here"."""
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    h = state_hash("ghost-unprovable")
    rec = ca.terminalize(
        ca.new_attempt(state_hash=h, minted_ts=now - 5.0,
                       status="result_ready", now=now), "expired", now=now)
    assert spool.write_attempt("ghost", h, rec) is True
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)
    _fail_listing(monkeypatch, "attempts")

    removed = spool.gc_orphan_dirs(registry_valid=True,
                                   member_plugins={PLUGIN}, now=now)

    monkeypatch.undo()
    assert removed == []
    assert _pdir(spool, "ghost").is_dir(), "the dir stays for the next pass"
    assert _removal_files(spool) == []


def test_orphan_gc_defers_a_dir_whose_artifact_listing_is_unprovable(
        spool, monkeypatch):
    """Red case (re-review 1): the tri-state reached the ATTEMPTS inventory
    and stopped there. The QUIESCENCE scan behind the very same purge still
    collapsed a faulting listing into "nothing here", so an EIO under
    `results/` made a dir holding a LIVE credential artifact read as old and
    empty — and the GC deleted it, with no record and nothing to re-derive
    from. Any unprovable listing anywhere under the dir defers it."""
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    h = state_hash("ghost-results-eio")
    _put(_results(spool, "ghost") / f"{h}.json", now - 5 * DAY,
         json.dumps(_record(h)))
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)
    _fail_listing(monkeypatch, "results")

    removed = spool.gc_orphan_dirs(registry_valid=True,
                                   member_plugins={PLUGIN}, now=now)

    monkeypatch.undo()
    assert removed == []
    assert _pdir(spool, "ghost").is_dir(), "the dir SURVIVES an unprovable scan"
    assert (_results(spool, "ghost") / f"{h}.json").exists()
    assert _removal_files(spool) == []

    # Healthy-path control: with the fault gone the same dir IS collected —
    # the fix defers an unprovable pass, it does not stop the GC converging.
    assert spool.gc_orphan_dirs(registry_valid=True,
                                member_plugins={PLUGIN}, now=now) == ["ghost"]
    assert not _pdir(spool, "ghost").exists()


def test_orphan_gc_defers_a_dir_whose_own_listing_is_unprovable(
        spool, monkeypatch):
    """The same rule at the top of the walk: the plugin dir itself is one of
    the directories whose listing must be PROVED, not peeked at."""
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)
    _fail_listing(monkeypatch, "ghost")

    removed = spool.gc_orphan_dirs(registry_valid=True,
                                   member_plugins={PLUGIN}, now=now)

    monkeypatch.undo()
    assert removed == []
    assert _pdir(spool, "ghost").is_dir()


def test_orphan_gc_defers_a_dir_whose_entry_metadata_is_unprovable(
        spool, monkeypatch):
    """An entry whose metadata will not read is an entry of UNKNOWN age — and
    an unread entry could be the NEWEST one. Skipping it (the `_lstat_quiet`
    reading, where every error is absence) is how a dir with a fresh artifact
    passes a 24-hour quiescence gate."""
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    h = state_hash("ghost-lstat-eio")
    _put(_results(spool, "ghost") / f"{h}.json", now - 5 * DAY,
         json.dumps(_record(h)))
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)
    real = os.lstat

    def flaky(path, *, dir_fd=None):
        if path == f"{h}.json":
            raise OSError(errno.EIO, "injected")
        return real(path, dir_fd=dir_fd)

    monkeypatch.setattr(cs.os, "lstat", flaky)

    removed = spool.gc_orphan_dirs(registry_valid=True,
                                   member_plugins={PLUGIN}, now=now)

    monkeypatch.undo()
    assert removed == []
    assert _pdir(spool, "ghost").is_dir()
    assert (_results(spool, "ghost") / f"{h}.json").exists()


def test_a_failed_orphan_gc_logs_no_callback_identifier(spool, monkeypatch,
                                                        caplog):
    """The GC half of Terra 2: same `rmtree`, same filename-bearing OSError,
    same rule — errno and the plugin name only (INV-CB-006)."""
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    h = state_hash("ghost-leak")
    _put(_results(spool, "ghost") / f"{h}.json", now - 5 * DAY)
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)

    def boom(path, dir_fd=None):
        raise OSError(errno.EIO, "I/O error", f"ghost/results/{h}.json")

    monkeypatch.setattr(cs.shutil, "rmtree", boom)
    caplog.set_level(logging.WARNING)
    removed = spool.gc_orphan_dirs(registry_valid=True,
                                   member_plugins={PLUGIN}, now=now)
    monkeypatch.undo()

    assert removed == []
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert h not in text, "a state hash reached a log line"
    assert str(errno.EIO) in text


def test_orphan_gc_of_a_settled_dir_writes_no_record(spool):
    """An orphan dir with no unacked attempt is a settled dir: it purges with
    no record, exactly as before this change."""
    now = time.time()
    spool.ensure_plugin_dirs("ghost")
    h = state_hash("ghost-3")
    assert spool.write_attempt("ghost", h, _attempt_rec(h, now)) is True
    (_attempts(spool, "ghost") / f"{cs.ACK_PREFIX}{h}").write_bytes(b"")
    _quiesce(_pdir(spool, "ghost"), now - 5 * DAY)

    removed = spool.gc_orphan_dirs(registry_valid=True,
                                   member_plugins={PLUGIN}, now=now)

    assert removed == ["ghost"]
    assert _removal_files(spool) == []


def test_a_record_outlives_the_removal_of_a_same_named_plugin(spool):
    """`.removals` is a reserved dot-root entry: reinstalling and re-removing
    the SAME plugin never touches the record its first removal wrote."""
    h = state_hash("outlive-1")
    _put(_pending(spool) / f"{h}.json", time.time())
    assert spool.remove_plugin(PLUGIN) is True
    first = _removal_files(spool)
    assert len(first) == 1

    spool.ensure_plugin_dirs(PLUGIN)
    assert spool.remove_plugin(PLUGIN) is True      # nothing unsettled now

    assert _removals(spool).is_dir()
    assert _removal_files(spool) == first


def test_list_removal_records_retires_a_malformed_entry(spool):
    now = time.time()
    _put_removal(spool, f"{PLUGIN}-aa.json", _removal_rec(ts=now))
    bad = _put_removal(spool, f"{PLUGIN}-bb.json", b"{not json")
    worse = _put_removal(spool, f"{PLUGIN}-cc.json",
                         _removal_rec(ts=now, reason="nonsense"))

    records = spool.list_removal_records()

    assert [name for name, _ in records] == [f"{PLUGIN}-aa.json"]
    assert records[0][1]["count"] == 1
    assert not bad.exists() and not worse.exists(), "invalid entries retired"


def test_mark_removal_noted_sets_the_noted_clock(spool):
    now = time.time()
    _put_removal(spool, f"{PLUGIN}-aa.json", _removal_rec(ts=now - 10))

    assert spool.mark_removal_noted(f"{PLUGIN}-aa.json", now=now) is True

    rec = _read_removal(spool, f"{PLUGIN}-aa.json")
    assert rec["noted"] is True and rec["noted_ts"] == now
    assert rec["ts"] == now - 10, "the creation clock is untouched"


def test_mark_removal_noted_strict_failure_leaves_it_unnoted(
        spool, monkeypatch):
    """Notify-then-mark is at-LEAST-once: a mark that will not go durable
    returns False, the record stays un-noted and the next worker pass
    retries (one duplicate DM is the accepted cost)."""
    now = time.time()
    _put_removal(spool, f"{PLUGIN}-aa.json", _removal_rec(ts=now))
    _fail_strict(monkeypatch)

    assert spool.mark_removal_noted(f"{PLUGIN}-aa.json", now=now) is False

    rec = _read_removal(spool, f"{PLUGIN}-aa.json")
    assert rec["noted"] is False and rec["noted_ts"] is None


@pytest.mark.parametrize("name,age_d,noted_d,pruned", [
    ("noted-8d", 8, 8, True),          # noted a week+ ago — pruned
    ("noted-6d", 6, 6, False),         # inside the retention window
    ("unnoted-31d", 31, None, False),  # #532: un-noted evidence of an owed
    #                                    operator notice is NEVER age-pruned
    ("unnoted-10d", 10, None, False),
    ("noted-late", 20, 1, False),      # Sol r3 item 10: created 20 days ago,
    #                                    noted YESTERDAY — the prune clock is
    #                                    noted_ts, so it survives
    ("noted-old", 45, 32, True),       # the hard bound still reaps a NOTED
    #                                    straggler past max age
])
def test_prune_removal_records_uses_the_noted_clock_and_a_hard_bound(
        spool, name, age_d, noted_d, pruned):
    now = time.time()
    fname = f"{PLUGIN}-{name}.json"
    _put_removal(spool, fname, _removal_rec(
        ts=now - age_d * DAY, noted=noted_d is not None,
        noted_ts=None if noted_d is None else now - noted_d * DAY))

    count = spool.prune_removal_records(now=now)

    assert count == (1 if pruned else 0)
    assert (_removals(spool) / fname).exists() is not pruned
