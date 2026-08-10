"""``callback_acks.py`` — the durable, fail-closed consent store for
plugin-declared authorization callbacks (INV-CB-003).

Structural sibling of ``trigger_acks.py``: same locking, atomic
persist-then-publish, and whole-store fail-closed load, but records are
keyed by :func:`plugin_callbacks.ack_identity` over ``(plugin, effective,
declaration_digest)`` — no artifact id, so a routine plugin upgrade that
leaves the declaration unchanged keeps its ack.
"""
import json

import pytest

from callback_acks import CallbackAckStore
from plugin_callbacks import ack_identity


def test_record_get_roundtrip(tmp_path):
    store = CallbackAckStore(path=tmp_path / "acks.json")
    rec = store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")

    identity = ack_identity("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    got = store.get(identity)

    assert got is not None
    assert got["plugin"] == "elevenlabs"
    assert got["effective"] == "plg-elevenlabs--oauth"
    assert got["declaration_digest"] == "digest-1"
    assert isinstance(got["gen"], str) and got["gen"]
    assert isinstance(got["ts"], int)
    assert got == rec


def test_get_missing_identity_returns_none(tmp_path):
    store = CallbackAckStore(path=tmp_path / "acks.json")
    assert store.get("nonexistent") is None


def test_restart_load_sees_prior_ack(tmp_path):
    path = tmp_path / "acks.json"
    store1 = CallbackAckStore(path=path)
    store1.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")

    store2 = CallbackAckStore(path=path)
    identity = ack_identity("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    got = store2.get(identity)

    assert got is not None
    assert got["plugin"] == "elevenlabs"


def test_record_idempotent_keeps_generation(tmp_path):
    store = CallbackAckStore(path=tmp_path / "acks.json")
    first = store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    second = store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")

    assert first["gen"] == second["gen"]


def test_corrupt_json_yields_zero_acks(tmp_path):
    path = tmp_path / "acks.json"
    path.write_text("{not valid json", encoding="utf-8")

    store = CallbackAckStore(path=path)

    assert store.get(ack_identity("elevenlabs", "eff", "digest-1")) is None


def test_wrong_schema_version_yields_zero_acks(tmp_path):
    path = tmp_path / "acks.json"
    identity = ack_identity("elevenlabs", "eff", "digest-1")
    path.write_text(json.dumps({
        "schema_version": 999,
        "acks": {
            identity: {
                "plugin": "elevenlabs", "effective": "eff",
                "declaration_digest": "digest-1", "ts": 1, "gen": "g1",
            },
        },
    }), encoding="utf-8")

    store = CallbackAckStore(path=path)

    assert store.get(identity) is None


def test_identity_key_mismatch_yields_zero_acks_whole_store(tmp_path):
    """INV-CB-003 red case: a record whose recomputed identity != its key
    must never load — and it takes down the WHOLE store, not just the one
    bad entry, since a hand-edited or merged file can no longer be trusted
    at all."""
    path = tmp_path / "acks.json"
    good_identity = ack_identity("elevenlabs", "eff-good", "digest-1")
    bad_key = "not-the-real-identity"
    path.write_text(json.dumps({
        "schema_version": 1,
        "acks": {
            good_identity: {
                "plugin": "elevenlabs", "effective": "eff-good",
                "declaration_digest": "digest-1", "ts": 1, "gen": "g1",
            },
            bad_key: {
                "plugin": "other", "effective": "eff-bad",
                "declaration_digest": "digest-2", "ts": 2, "gen": "g2",
            },
        },
    }), encoding="utf-8")

    store = CallbackAckStore(path=path)

    # Even the record whose key WAS correct fails to load: whole-store
    # fail-closed, not per-record filtering.
    assert store.get(good_identity) is None


def test_malformed_record_missing_field_yields_zero_acks(tmp_path):
    path = tmp_path / "acks.json"
    identity = ack_identity("elevenlabs", "eff", "digest-1")
    path.write_text(json.dumps({
        "schema_version": 1,
        "acks": {
            identity: {
                "plugin": "elevenlabs", "effective": "eff",
                # declaration_digest missing entirely
                "ts": 1, "gen": "g1",
            },
        },
    }), encoding="utf-8")

    store = CallbackAckStore(path=path)

    assert store.get(identity) is None


def _store_with_record(path, rec):
    identity = ack_identity("elevenlabs", "eff", "digest-1")
    path.write_text(json.dumps({
        "schema_version": 1,
        "acks": {identity: rec},
    }), encoding="utf-8")
    return identity


def test_non_numeric_ts_yields_zero_acks_whole_store(tmp_path):
    """INV-CB-003: ``ts`` must be a real finite number. A string ``ts`` is a
    malformed record and fails the WHOLE store, same as a bad identity."""
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "plugin": "elevenlabs", "effective": "eff",
        "declaration_digest": "digest-1", "ts": "not-a-number", "gen": "g1",
    })
    assert CallbackAckStore(path=path).get(identity) is None


def test_bool_ts_yields_zero_acks_whole_store(tmp_path):
    """A bool is a subclass of int but is not a real timestamp — rejected."""
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "plugin": "elevenlabs", "effective": "eff",
        "declaration_digest": "digest-1", "ts": True, "gen": "g1",
    })
    assert CallbackAckStore(path=path).get(identity) is None


def test_extra_field_yields_zero_acks_whole_store(tmp_path):
    """An otherwise-valid record carrying a key outside the exact set
    {plugin, effective, declaration_digest, gen, ts} means the file was
    written by something other than this store — the whole store fails."""
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "plugin": "elevenlabs", "effective": "eff",
        "declaration_digest": "digest-1", "ts": 1, "gen": "g1",
        "unexpected": "surprise",
    })
    assert CallbackAckStore(path=path).get(identity) is None


def test_clean_record_still_loads(tmp_path):
    """The tightened schema must not reject a well-formed record."""
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "plugin": "elevenlabs", "effective": "eff",
        "declaration_digest": "digest-1", "ts": 1, "gen": "g1",
    })
    got = CallbackAckStore(path=path).get(identity)
    assert got is not None and got["gen"] == "g1"


def test_float_ts_is_accepted(tmp_path):
    """A float ``ts`` (a legitimate ``time.time()`` shape) still loads."""
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "plugin": "elevenlabs", "effective": "eff",
        "declaration_digest": "digest-1", "ts": 1.5, "gen": "g1",
    })
    assert CallbackAckStore(path=path).get(identity) is not None


def test_huge_int_ts_does_not_crash_load(tmp_path):
    """A stored record with an absurdly large integer ``ts`` (``10**1000``)
    must NOT crash ``_load``: ``math.isfinite`` on such an int raises
    ``OverflowError`` (int→C-double), and ``_load`` runs at construction
    (boot), so that would be a fail-OPEN-into-crash defeating the whole-store
    fail-closed contract (INV-CB-003). An int of any magnitude is a valid (if
    absurd) timestamp, so it loads rather than failing the store."""
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "plugin": "elevenlabs", "effective": "eff",
        "declaration_digest": "digest-1", "ts": 10 ** 1000, "gen": "g1",
    })
    store = CallbackAckStore(path=path)          # must not raise
    assert store.get(identity) is not None
    assert store.get(identity)["ts"] == 10 ** 1000


def test_nan_and_inf_ts_yield_zero_acks(tmp_path):
    """A non-finite float ``ts`` (NaN / ±inf) is not a real timestamp — the
    record fails the whole store, but the check must not raise."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        path = tmp_path / "acks.json"
        identity = _store_with_record(path, {
            "plugin": "elevenlabs", "effective": "eff",
            "declaration_digest": "digest-1", "ts": bad, "gen": "g1",
        })
        assert CallbackAckStore(path=path).get(identity) is None


def test_load_never_raises_on_adversarial_ts(tmp_path):
    """Totality pin: ``_load`` must return a dict (never propagate) for a
    spread of hostile ``ts`` values — huge ints, non-finite floats, and
    non-numbers alike."""
    path = tmp_path / "acks.json"
    for bad in (10 ** 1000, -(10 ** 1000), float("nan"), float("inf"),
                "not-a-number", None, [1], {"x": 1}, True):
        _store_with_record(path, {
            "plugin": "elevenlabs", "effective": "eff",
            "declaration_digest": "digest-1", "ts": bad, "gen": "g1",
        })
        store = CallbackAckStore(path=path)      # construction calls _load
        assert isinstance(store._load(), dict)   # explicit: no exception


def test_deeply_nested_json_does_not_crash_load(tmp_path):
    """``json.loads`` on deeply-nested JSON raises ``RecursionError`` — NOT an
    ``OSError``/``ValueError`` — so an inner-only guard would let it crash the
    ACKS singleton at construction (boot). ``_load`` must be TOTAL: catch-all
    fail-closed to no acks (INV-CB-003)."""
    path = tmp_path / "acks.json"
    path.write_text("[" * 200000, encoding="utf-8")   # trips RecursionError

    store = CallbackAckStore(path=path)               # must not raise
    assert store.get(ack_identity("elevenlabs", "eff", "digest-1")) is None
    assert store._load() == {}


def test_oversized_store_yields_zero_acks(tmp_path):
    """An absurdly large store is rejected wholesale (bounded read) rather than
    slurped into memory — fail closed to no acks."""
    path = tmp_path / "acks.json"
    identity = ack_identity("elevenlabs", "eff", "digest-1")
    inner = {
        "schema_version": 1,
        "acks": {identity: {
            "plugin": "elevenlabs", "effective": "eff",
            "declaration_digest": "digest-1", "ts": 1, "gen": "g1",
        }},
    }
    # Valid JSON, but padded past the cap with trailing whitespace so the file
    # is well-formed yet oversized.
    blob = json.dumps(inner) + (" " * (5 * 1024 * 1024))
    path.write_text(blob, encoding="utf-8")

    store = CallbackAckStore(path=path)
    assert store.get(identity) is None
    assert store._load() == {}


def test_load_is_total_on_adversarial_content(tmp_path):
    """Totality pin over the whole load (not just the ts field): deeply-nested
    JSON, a huge-int ts, a NaN ts, and an oversized file each yield a dict from
    ``_load`` and never propagate."""
    path = tmp_path / "acks.json"
    identity = ack_identity("elevenlabs", "eff", "digest-1")
    rec = {"plugin": "elevenlabs", "effective": "eff",
           "declaration_digest": "digest-1", "gen": "g1"}
    cases = [
        "[" * 200000,                                             # RecursionError
        json.dumps({"schema_version": 1,
                    "acks": {identity: dict(rec, ts=10 ** 1000)}}),  # huge int
        json.dumps({"schema_version": 1,
                    "acks": {identity: dict(rec, ts=float("nan"))}}),  # NaN
        json.dumps({"schema_version": 1, "acks": {identity: dict(rec, ts=1)}})
        + (" " * (5 * 1024 * 1024)),                             # oversized
    ]
    for blob in cases:
        path.write_text(blob, encoding="utf-8")
        assert isinstance(CallbackAckStore(path=path)._load(), dict)


def test_revoke_plugin_returns_removed_and_persists(tmp_path):
    path = tmp_path / "acks.json"
    store = CallbackAckStore(path=path)
    store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    store.record("elevenlabs", "plg-elevenlabs--callback2", "digest-2")
    store.record("other-plugin", "plg-other-plugin--oauth", "digest-3")

    removed = store.revoke_plugin("elevenlabs")

    assert len(removed) == 2
    assert {r["plugin"] for r in removed} == {"elevenlabs"}

    other_identity = ack_identity("other-plugin", "plg-other-plugin--oauth", "digest-3")
    ev_identity = ack_identity("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    assert store.get(ev_identity) is None
    assert store.get(other_identity) is not None

    # Persisted: a fresh store instance over the same file agrees.
    reloaded = CallbackAckStore(path=path)
    assert reloaded.get(ev_identity) is None
    assert reloaded.get(other_identity) is not None


def test_revoke_plugin_no_match_returns_empty_and_does_not_persist(tmp_path):
    path = tmp_path / "acks.json"
    store = CallbackAckStore(path=path)
    store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    before = path.read_text(encoding="utf-8")

    removed = store.revoke_plugin("nonexistent")

    assert removed == []
    assert path.read_text(encoding="utf-8") == before


def test_record_publishes_only_after_durable_write(tmp_path, monkeypatch):
    """A failed persist must raise AND leave the in-memory view unchanged —
    a reconcile racing the failure can never route an ack that would vanish
    on reboot (mirrors trigger_acks.py's identical pin)."""
    store = CallbackAckStore(path=tmp_path / "acks.json")

    def _boom(path, text, **kwargs):   # **kwargs: the store passes mode=PRIVATE
        raise OSError("disk full")

    import atomic_io
    monkeypatch.setattr(atomic_io, "atomic_write_text", _boom)

    with pytest.raises(OSError):
        store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")

    identity = ack_identity("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    assert store.get(identity) is None


def test_revoke_publishes_only_after_durable_write(tmp_path, monkeypatch):
    store = CallbackAckStore(path=tmp_path / "acks.json")
    store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    identity = ack_identity("elevenlabs", "plg-elevenlabs--oauth", "digest-1")

    def _boom(path, text, **kwargs):   # **kwargs: the store passes mode=PRIVATE
        raise OSError("disk full")

    import atomic_io
    monkeypatch.setattr(atomic_io, "atomic_write_text", _boom)

    with pytest.raises(OSError):
        store.revoke_plugin("elevenlabs")

    # memory unchanged: the revoke did NOT half-apply (a crash would have
    # silently resurrected it from disk otherwise)
    assert store.get(identity) is not None


def test_prune_stale_drops_only_identities_outside_valid_set(tmp_path):
    store = CallbackAckStore(path=tmp_path / "acks.json")
    store.record("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    store.record("elevenlabs", "plg-elevenlabs--callback2", "digest-2")
    store.record("other-plugin", "plg-other-plugin--oauth", "digest-3")

    keep_identity = ack_identity("elevenlabs", "plg-elevenlabs--oauth", "digest-1")
    stale_identity = ack_identity("elevenlabs", "plg-elevenlabs--callback2", "digest-2")
    other_identity = ack_identity("other-plugin", "plg-other-plugin--oauth", "digest-3")

    removed = store.prune_stale({keep_identity, other_identity})

    assert len(removed) == 1
    assert removed[0]["declaration_digest"] == "digest-2"
    assert store.get(stale_identity) is None
    assert store.get(keep_identity) is not None
    assert store.get(other_identity) is not None
