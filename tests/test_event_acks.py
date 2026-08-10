"""``event_acks.py`` — the durable, fail-closed consent store for
plugin-declared event subscriptions.

Structural sibling of ``callback_acks.py`` (same locking, atomic
persist-then-publish, and whole-store fail-closed load), but records are
keyed by :func:`plugin_events.ack_identity` over ``(subscriber,
subscriber_artifact_id, emitter, event, digest, sorted(targets))`` — the
artifact id AND the target set are part of the identity, unlike a callback
ack, because a subscription reaches into a subscriber role the operator
must approve.
"""
import json

import pytest

from event_acks import EventAckStore
from plugin_events import ack_identity


def _identity(subscriber="finance", artifact_id="art-1", emitter="gmail",
             event="new-mail", digest="digest-1", targets=("resident:assistant",)):
    return ack_identity(subscriber, artifact_id, emitter, event, digest,
                        list(targets))


def test_record_get_roundtrip(tmp_path):
    store = EventAckStore(path=tmp_path / "acks.json")
    rec = store.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                       ["resident:assistant"], 1000.0)

    identity = _identity()
    got = store.get(identity)

    assert got is not None
    assert got["subscriber"] == "finance"
    assert got["artifact_id"] == "art-1"
    assert got["emitter"] == "gmail"
    assert got["event"] == "new-mail"
    assert got["digest"] == "digest-1"
    assert got["targets"] == ["resident:assistant"]
    assert isinstance(got["gen"], str) and got["gen"]
    assert got["ts"] == 1000.0
    assert got == rec


def test_targets_are_stored_sorted(tmp_path):
    store = EventAckStore(path=tmp_path / "acks.json")
    rec = store.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                       ["specialist:z", "resident:assistant", "specialist:a"],
                       1000.0)
    assert rec["targets"] == ["resident:assistant", "specialist:a", "specialist:z"]


def test_get_missing_identity_returns_none(tmp_path):
    store = EventAckStore(path=tmp_path / "acks.json")
    assert store.get("nonexistent") is None


def test_restart_load_sees_prior_ack(tmp_path):
    path = tmp_path / "acks.json"
    store1 = EventAckStore(path=path)
    store1.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                  ["resident:assistant"], 1000.0)

    store2 = EventAckStore(path=path)
    got = store2.get(_identity())

    assert got is not None
    assert got["subscriber"] == "finance"


def test_record_idempotent_keeps_generation(tmp_path):
    store = EventAckStore(path=tmp_path / "acks.json")
    first = store.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                         ["resident:assistant"], 1000.0)
    second = store.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                          ["resident:assistant"], 1001.0)

    assert first["gen"] == second["gen"]


def test_identity_distinct_when_artifact_id_differs(tmp_path):
    """A plugin upgrade (new artifact id) must mint a NEW identity — no
    consent carries over silently."""
    store = EventAckStore(path=tmp_path / "acks.json")
    store.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                ["resident:assistant"], 1000.0)

    old_identity = _identity(artifact_id="art-1")
    new_identity = _identity(artifact_id="art-2")
    assert old_identity != new_identity
    assert store.get(old_identity) is not None
    assert store.get(new_identity) is None


def test_identity_distinct_when_targets_differ(tmp_path):
    """Retargeting a subscription is a new consent question — a different
    target set must mint a different identity."""
    store = EventAckStore(path=tmp_path / "acks.json")
    store.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                ["resident:assistant"], 1000.0)

    old_identity = _identity(targets=("resident:assistant",))
    new_identity = _identity(targets=("resident:assistant", "specialist:analyst"))
    assert old_identity != new_identity
    assert store.get(old_identity) is not None
    assert store.get(new_identity) is None


def test_corrupt_json_yields_zero_acks(tmp_path):
    path = tmp_path / "acks.json"
    path.write_text("{not valid json", encoding="utf-8")

    store = EventAckStore(path=path)

    assert store.get(_identity()) is None


def test_wrong_schema_version_yields_zero_acks(tmp_path):
    path = tmp_path / "acks.json"
    identity = _identity()
    path.write_text(json.dumps({
        "schema_version": 999,
        "acks": {
            identity: {
                "subscriber": "finance", "artifact_id": "art-1",
                "emitter": "gmail", "event": "new-mail", "digest": "digest-1",
                "targets": ["resident:assistant"], "ts": 1, "gen": "g1",
            },
        },
    }), encoding="utf-8")

    store = EventAckStore(path=path)

    assert store.get(identity) is None


def test_identity_key_mismatch_yields_zero_acks_whole_store(tmp_path):
    """A record whose recomputed identity != its key must never load — and
    it takes down the WHOLE store, not just the one bad entry, since a
    hand-edited or merged file can no longer be trusted at all."""
    path = tmp_path / "acks.json"
    good_identity = _identity()
    bad_key = "not-the-real-identity"
    path.write_text(json.dumps({
        "schema_version": 1,
        "acks": {
            good_identity: {
                "subscriber": "finance", "artifact_id": "art-1",
                "emitter": "gmail", "event": "new-mail", "digest": "digest-1",
                "targets": ["resident:assistant"], "ts": 1, "gen": "g1",
            },
            bad_key: {
                "subscriber": "other", "artifact_id": "art-x",
                "emitter": "outlook", "event": "new-mail", "digest": "digest-2",
                "targets": ["resident:assistant"], "ts": 2, "gen": "g2",
            },
        },
    }), encoding="utf-8")

    store = EventAckStore(path=path)

    assert store.get(good_identity) is None


def test_malformed_record_missing_field_yields_zero_acks(tmp_path):
    """Important-4(b) pin: a SECOND, otherwise perfectly valid record sits
    alongside the malformed one — the vacuous single-record version of
    this test could never distinguish "that one bad record failed to
    parse" from the actual whole-store fail-closed contract, since with
    only one record in the file the two are indistinguishable. Both
    identities must come back empty."""
    path = tmp_path / "acks.json"
    bad_identity = _identity()
    good_identity = _identity(subscriber="other", artifact_id="art-2",
                              emitter="outlook", digest="digest-2")
    path.write_text(json.dumps({
        "schema_version": 1,
        "acks": {
            bad_identity: {
                "subscriber": "finance", "artifact_id": "art-1",
                "emitter": "gmail", "event": "new-mail",
                # digest missing entirely
                "targets": ["resident:assistant"], "ts": 1, "gen": "g1",
            },
            good_identity: {
                "subscriber": "other", "artifact_id": "art-2",
                "emitter": "outlook", "event": "new-mail",
                "digest": "digest-2",
                "targets": ["resident:assistant"], "ts": 2, "gen": "g2",
            },
        },
    }), encoding="utf-8")

    store = EventAckStore(path=path)

    assert store.get(bad_identity) is None
    assert store.get(good_identity) is None


def _store_with_records(path, records: dict) -> None:
    path.write_text(json.dumps({
        "schema_version": 1,
        "acks": records,
    }), encoding="utf-8")


def _good_record(identity=None):
    """A SECOND, otherwise perfectly valid record + its identity — seeded
    alongside a deliberately bad one in every whole-store test below
    (Internal minor #2, mirrors Important-4(b)'s existing discipline): a
    single-record version of these tests cannot distinguish "that one bad
    record failed to parse and was skipped" (a hypothetical ``continue``)
    from the actual whole-store fail-closed contract (``return {}``),
    since with only one record in the file the two read identically —
    both would leave ``store.get(bad_identity)`` at ``None``. Only a
    surviving SECOND record proves which one actually happened."""
    identity = identity or _identity(
        subscriber="other", artifact_id="art-2", emitter="outlook",
        digest="digest-2")
    rec = {
        "subscriber": "other", "artifact_id": "art-2", "emitter": "outlook",
        "event": "new-mail", "digest": "digest-2",
        "targets": ["resident:assistant"], "ts": 2, "gen": "g2",
    }
    return identity, rec


def test_non_list_targets_yields_zero_acks_whole_store(tmp_path):
    path = tmp_path / "acks.json"
    bad_identity = _identity()
    good_identity, good_rec = _good_record()
    _store_with_records(path, {
        bad_identity: {
            "subscriber": "finance", "artifact_id": "art-1",
            "emitter": "gmail", "event": "new-mail", "digest": "digest-1",
            "targets": "resident:assistant",  # not a list
            "ts": 1, "gen": "g1",
        },
        good_identity: good_rec,
    })

    store = EventAckStore(path=path)

    assert store.get(bad_identity) is None
    assert store.get(good_identity) is None    # the whole store failed
    # closed — the good record never survives alongside the bad one.


def _store_with_record(path, rec, identity=None):
    identity = identity or _identity()
    path.write_text(json.dumps({
        "schema_version": 1,
        "acks": {identity: rec},
    }), encoding="utf-8")
    return identity


def test_non_numeric_ts_yields_zero_acks_whole_store(tmp_path):
    path = tmp_path / "acks.json"
    bad_identity = _identity()
    good_identity, good_rec = _good_record()
    _store_with_records(path, {
        bad_identity: {
            "subscriber": "finance", "artifact_id": "art-1",
            "emitter": "gmail", "event": "new-mail", "digest": "digest-1",
            "targets": ["resident:assistant"], "ts": "not-a-number",
            "gen": "g1",
        },
        good_identity: good_rec,
    })
    store = EventAckStore(path=path)
    assert store.get(bad_identity) is None
    assert store.get(good_identity) is None


def test_bool_ts_yields_zero_acks_whole_store(tmp_path):
    path = tmp_path / "acks.json"
    bad_identity = _identity()
    good_identity, good_rec = _good_record()
    _store_with_records(path, {
        bad_identity: {
            "subscriber": "finance", "artifact_id": "art-1",
            "emitter": "gmail", "event": "new-mail", "digest": "digest-1",
            "targets": ["resident:assistant"], "ts": True, "gen": "g1",
        },
        good_identity: good_rec,
    })
    store = EventAckStore(path=path)
    assert store.get(bad_identity) is None
    assert store.get(good_identity) is None


def test_non_string_gen_yields_zero_acks_whole_store(tmp_path):
    """Internal minor #2: no prior test exercised a malformed ``gen`` at
    all — a non-string (or empty-string) ``gen`` must fail the whole
    store exactly like a bad ``targets``/``ts`` does."""
    path = tmp_path / "acks.json"
    bad_identity = _identity()
    good_identity, good_rec = _good_record()
    _store_with_records(path, {
        bad_identity: {
            "subscriber": "finance", "artifact_id": "art-1",
            "emitter": "gmail", "event": "new-mail", "digest": "digest-1",
            "targets": ["resident:assistant"], "ts": 1, "gen": 123,
        },
        good_identity: good_rec,
    })
    store = EventAckStore(path=path)
    assert store.get(bad_identity) is None
    assert store.get(good_identity) is None


def test_extra_field_yields_zero_acks_whole_store(tmp_path):
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "subscriber": "finance", "artifact_id": "art-1", "emitter": "gmail",
        "event": "new-mail", "digest": "digest-1",
        "targets": ["resident:assistant"], "ts": 1, "gen": "g1",
        "unexpected": "surprise",
    })
    assert EventAckStore(path=path).get(identity) is None


def test_clean_record_still_loads(tmp_path):
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "subscriber": "finance", "artifact_id": "art-1", "emitter": "gmail",
        "event": "new-mail", "digest": "digest-1",
        "targets": ["resident:assistant"], "ts": 1, "gen": "g1",
    })
    got = EventAckStore(path=path).get(identity)
    assert got is not None and got["gen"] == "g1"


def test_float_ts_is_accepted(tmp_path):
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "subscriber": "finance", "artifact_id": "art-1", "emitter": "gmail",
        "event": "new-mail", "digest": "digest-1",
        "targets": ["resident:assistant"], "ts": 1.5, "gen": "g1",
    })
    assert EventAckStore(path=path).get(identity) is not None


def test_huge_int_ts_does_not_crash_load(tmp_path):
    path = tmp_path / "acks.json"
    identity = _store_with_record(path, {
        "subscriber": "finance", "artifact_id": "art-1", "emitter": "gmail",
        "event": "new-mail", "digest": "digest-1",
        "targets": ["resident:assistant"], "ts": 10 ** 1000, "gen": "g1",
    })
    store = EventAckStore(path=path)          # must not raise
    assert store.get(identity) is not None
    assert store.get(identity)["ts"] == 10 ** 1000


def test_nan_and_inf_ts_yield_zero_acks(tmp_path):
    for bad in (float("nan"), float("inf"), float("-inf")):
        path = tmp_path / "acks.json"
        identity = _store_with_record(path, {
            "subscriber": "finance", "artifact_id": "art-1", "emitter": "gmail",
            "event": "new-mail", "digest": "digest-1",
            "targets": ["resident:assistant"], "ts": bad, "gen": "g1",
        })
        assert EventAckStore(path=path).get(identity) is None


def test_load_never_raises_on_adversarial_ts(tmp_path):
    path = tmp_path / "acks.json"
    for bad in (10 ** 1000, -(10 ** 1000), float("nan"), float("inf"),
                "not-a-number", None, [1], {"x": 1}, True):
        _store_with_record(path, {
            "subscriber": "finance", "artifact_id": "art-1", "emitter": "gmail",
            "event": "new-mail", "digest": "digest-1",
            "targets": ["resident:assistant"], "ts": bad, "gen": "g1",
        })
        store = EventAckStore(path=path)      # construction calls _load
        assert isinstance(store._load(), dict)   # explicit: no exception


def test_deeply_nested_json_does_not_crash_load(tmp_path):
    path = tmp_path / "acks.json"
    path.write_text("[" * 200000, encoding="utf-8")   # trips RecursionError

    store = EventAckStore(path=path)               # must not raise
    assert store.get(_identity()) is None
    assert store._load() == {}


def test_oversized_store_yields_zero_acks(tmp_path):
    path = tmp_path / "acks.json"
    identity = _identity()
    inner = {
        "schema_version": 1,
        "acks": {identity: {
            "subscriber": "finance", "artifact_id": "art-1", "emitter": "gmail",
            "event": "new-mail", "digest": "digest-1",
            "targets": ["resident:assistant"], "ts": 1, "gen": "g1",
        }},
    }
    blob = json.dumps(inner) + (" " * (5 * 1024 * 1024))
    path.write_text(blob, encoding="utf-8")

    store = EventAckStore(path=path)
    assert store.get(identity) is None
    assert store._load() == {}


def test_corrupt_store_then_record_persists_a_valid_store(tmp_path):
    """Fail-closed whole-store load: a corrupt store loads empty, and the
    NEXT ``record()`` atomically persists a valid store overwriting the
    corruption (mirrors ``callback_acks.py:83``/``:174`` — never a
    refuse-to-persist behavior)."""
    path = tmp_path / "acks.json"
    path.write_text("{not valid json", encoding="utf-8")

    store = EventAckStore(path=path)
    assert store.get(_identity()) is None       # corrupt load -> empty

    rec = store.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                       ["resident:assistant"], 1000.0)
    assert store.get(_identity()) == rec

    # A fresh load from disk sees the valid store — the corruption is gone.
    reloaded = EventAckStore(path=path)
    assert reloaded.get(_identity()) == rec


def test_load_is_total_on_adversarial_content(tmp_path):
    path = tmp_path / "acks.json"
    identity = _identity()
    rec = {"subscriber": "finance", "artifact_id": "art-1", "emitter": "gmail",
           "event": "new-mail", "digest": "digest-1",
           "targets": ["resident:assistant"], "gen": "g1"}
    cases = [
        "[" * 200000,
        json.dumps({"schema_version": 1,
                    "acks": {identity: dict(rec, ts=10 ** 1000)}}),
        json.dumps({"schema_version": 1,
                    "acks": {identity: dict(rec, ts=float("nan"))}}),
        json.dumps({"schema_version": 1, "acks": {identity: dict(rec, ts=1)}})
        + (" " * (5 * 1024 * 1024)),
    ]
    for blob in cases:
        path.write_text(blob, encoding="utf-8")
        assert isinstance(EventAckStore(path=path)._load(), dict)


def test_revoke_subscriber_returns_count_and_persists(tmp_path):
    path = tmp_path / "acks.json"
    store = EventAckStore(path=path)
    store.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                ["resident:assistant"], 1000.0)
    store.record("finance", "art-1", "outlook", "new-mail", "digest-2",
                ["resident:assistant"], 1001.0)
    store.record("other-plugin", "art-1", "gmail", "new-mail", "digest-3",
                ["resident:assistant"], 1002.0)

    removed = store.revoke_subscriber("finance")

    assert len(removed) == 2
    # Spot-check that returned records carry expected subscriber field
    assert all(r.get("subscriber") == "finance" for r in removed)
    # Verify both emitters are present
    emitters = {r.get("emitter") for r in removed}
    assert emitters == {"gmail", "outlook"}

    finance_gmail = _identity(subscriber="finance", emitter="gmail", digest="digest-1")
    finance_outlook = _identity(subscriber="finance", emitter="outlook", digest="digest-2")
    other = _identity(subscriber="other-plugin", digest="digest-3")
    assert store.get(finance_gmail) is None
    assert store.get(finance_outlook) is None
    assert store.get(other) is not None

    reloaded = EventAckStore(path=path)
    assert reloaded.get(finance_gmail) is None
    assert reloaded.get(other) is not None


def test_revoke_subscriber_no_match_returns_zero_and_does_not_persist(tmp_path):
    path = tmp_path / "acks.json"
    store = EventAckStore(path=path)
    store.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                ["resident:assistant"], 1000.0)
    before = path.read_text(encoding="utf-8")

    removed = store.revoke_subscriber("nonexistent")

    assert len(removed) == 0
    assert path.read_text(encoding="utf-8") == before


def test_revoke_pair_drops_only_that_subscription(tmp_path):
    path = tmp_path / "acks.json"
    store = EventAckStore(path=path)
    store.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                ["resident:assistant"], 1000.0)
    store.record("finance", "art-1", "gmail", "invoice", "digest-2",
                ["resident:assistant"], 1001.0)
    store.record("finance", "art-1", "outlook", "new-mail", "digest-3",
                ["resident:assistant"], 1002.0)

    removed = store.revoke_pair("finance", "gmail", "new-mail")

    assert len(removed) == 1
    # Spot-check that the returned record has the expected fields
    assert removed[0].get("emitter") == "gmail"
    assert removed[0].get("event") == "new-mail"
    assert removed[0].get("subscriber") == "finance"

    kept1 = _identity(emitter="gmail", event="invoice", digest="digest-2")
    kept2 = _identity(emitter="outlook", event="new-mail", digest="digest-3")
    dropped = _identity(emitter="gmail", event="new-mail", digest="digest-1")
    assert store.get(dropped) is None
    assert store.get(kept1) is not None
    assert store.get(kept2) is not None


def test_record_publishes_only_after_durable_write(tmp_path, monkeypatch):
    """A failed persist must raise AND leave the in-memory view unchanged —
    a reconcile racing the failure can never route an ack that would vanish
    on reboot."""
    store = EventAckStore(path=tmp_path / "acks.json")

    def _boom(path, text, **kwargs):   # **kwargs: the store passes mode=PRIVATE
        raise OSError("disk full")

    import atomic_io
    monkeypatch.setattr(atomic_io, "atomic_write_text", _boom)

    with pytest.raises(OSError):
        store.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                     ["resident:assistant"], 1000.0)

    assert store.get(_identity()) is None


def test_revoke_publishes_only_after_durable_write(tmp_path, monkeypatch):
    store = EventAckStore(path=tmp_path / "acks.json")
    store.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                ["resident:assistant"], 1000.0)

    def _boom(path, text, **kwargs):   # **kwargs: the store passes mode=PRIVATE
        raise OSError("disk full")

    import atomic_io
    monkeypatch.setattr(atomic_io, "atomic_write_text", _boom)

    with pytest.raises(OSError):
        store.revoke_subscriber("finance")

    # memory unchanged: the revoke did NOT half-apply
    assert store.get(_identity()) is not None


def test_prune_stale_drops_only_identities_outside_valid_set(tmp_path):
    store = EventAckStore(path=tmp_path / "acks.json")
    store.record("finance", "art-1", "gmail", "new-mail", "digest-1",
                ["resident:assistant"], 1000.0)
    store.record("finance", "art-1", "outlook", "new-mail", "digest-2",
                ["resident:assistant"], 1001.0)
    store.record("other-plugin", "art-1", "gmail", "new-mail", "digest-3",
                ["resident:assistant"], 1002.0)

    keep_identity = _identity(emitter="gmail", digest="digest-1")
    stale_identity = _identity(emitter="outlook", digest="digest-2")
    other_identity = _identity(subscriber="other-plugin", digest="digest-3")

    removed = store.prune_stale({keep_identity, other_identity})

    assert len(removed) == 1
    # Spot-check that the returned record has the expected fields
    assert removed[0].get("emitter") == "outlook"
    assert removed[0].get("event") == "new-mail"
    assert removed[0].get("subscriber") == "finance"

    assert store.get(stale_identity) is None
    assert store.get(keep_identity) is not None
    assert store.get(other_identity) is not None
