"""#620 — the resident mint receipt (INV-TRIG-014).

#620 is that a deleted resident webhook trigger leaves its secret at
`/data/webhook_secrets/<name>` and a same-name trigger inherits it. Retiring
the secret is the fix and it is BLOCKED: `_valid_value("provider", <a 43-byte
casa token>)` is True, so no shape check separates a Casa token from an
operator credential — and Casa can neither regenerate nor import the latter
(#621), so destroying one is unreconstructible loss.

A receipt is the missing durable fact: proof that Casa GENERATED the bytes that
are in a slot right now. What is pinned here is that the proof is by VALUE and
never by presence, and that every other condition is `unproven` — because
`unproven` is what a later retirement must refuse to act on.

#620 itself is closed at the ROUTE lifecycle (`tests/test_resident_trigger_retirement.py`,
INV-TRIG-016), not by the mint: `test_successful_removal_then_recreate_gets_fresh_credential`
pins the retirement-then-fresh-mint sequence at the primitive level, and
`test_delete_and_recreate_without_registration_keeps_credential` keeps the narrow
truth the old characterisation was about — the writer alone never deletes.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from unittest.mock import MagicMock

from aiohttp import web

import resident_trigger_secrets as rts
import webhook_auth
from config import TriggerSpec
from trigger_registry import TriggerRegistry


def _registry() -> TriggerRegistry:
    return TriggerRegistry(scheduler=MagicMock(), app=web.Application(), bus=None)


def _rows(specs, registry, role="assistant", *, secrets_dir, usable=True):
    snapshot = rts.snapshot_rows(
        specs=specs, registry=registry, role=role, global_secret_usable=usable)
    return {r["name"]: r for r in rts.resolve_rows(snapshot, secrets_dir=secrets_dir)}


def _webhook(name: str, *, mode: str = "static_header", owner: str = "casa") -> TriggerSpec:
    return TriggerSpec(
        name=name, type="webhook", clearance="public",
        auth={"mode": mode, "header": "X-API-Key", "tolerance_secs": 300,
              "secret_owner": owner},
    )


def _receipt(name: str, tmp_path: Path) -> dict:
    return json.loads((tmp_path / f"{name}.mint").read_bytes())


class _ModuleShim:
    """A stand-in for a module with some attributes overridden.

    Faults are injected by swapping the MODULE'S OWN reference
    (`webhook_auth.os`), never by patching the shared `os` module — patching a
    shared module attribute is the failure this repo carries a standing rule
    about, and `os.replace` is used across the whole tree.
    """

    def __init__(self, wrapped, **overrides):
        self._wrapped = wrapped
        self.__dict__.update(overrides)

    def __getattr__(self, item):
        return getattr(self._wrapped, item)


# ---------------------------------------------------------------------------
# Certification is by VALUE, never by presence
# ---------------------------------------------------------------------------


def test_a_hand_placed_value_is_never_certified(tmp_path: Path):
    """Mutation killed: certifying a slot because its bytes are casa-SHAPED.

    A 43-byte ASCII value is exactly what Casa mints, and also exactly what an
    operator may place. Shape proves nothing about who wrote it.
    """
    (tmp_path / "vm").write_bytes(b"z" * 43)

    assert webhook_auth._valid_value("casa", b"z" * 43) is True, "precondition"
    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "unproven"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["vm"]


def test_a_receipt_does_not_survive_a_value_replacement(tmp_path: Path):
    """Mutation killed: certifying on the receipt's PRESENCE.

    This is the H2 guard. If a stale receipt kept certifying after the bytes
    changed, a later retirement would destroy whatever replaced them — and the
    replacement is exactly the operator credential Casa cannot re-import.
    """
    minted = webhook_auth.mint_resident_secret("vm", secrets_dir=tmp_path, role="assistant")
    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "casa_minted"
    before = (tmp_path / "vm.mint").read_bytes()

    (tmp_path / "vm").write_bytes(b"operator-placed-opaque-credential-value-!!")

    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "unproven"
    assert (tmp_path / "vm.mint").read_bytes() == before, "receipt untouched"
    assert (tmp_path / "vm").read_bytes() != minted


def test_the_receipt_binds_the_exact_minted_bytes(tmp_path: Path):
    """The digest is of the value, and the value is never in the receipt."""
    minted = webhook_auth.mint_resident_secret("vm", secrets_dir=tmp_path, role="assistant")
    rec = _receipt("vm", tmp_path)

    assert rec == {"v": 2, "minted_by": "casa",
                   "value_sha256": hashlib.sha256(minted).hexdigest(),
                   "role": "assistant"}
    assert minted not in (tmp_path / "vm.mint").read_bytes()
    assert len(minted) == 43
    assert (tmp_path / "vm.mint").stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# Never backfill, never mark what Casa did not create
# ---------------------------------------------------------------------------


def test_an_existing_value_is_returned_but_never_receipted(tmp_path: Path):
    """Mutation killed: writing a receipt for a value the mint merely FOUND.

    Backfilling is the single most dangerous thing this mechanism could do:
    it would certify an operator's credential as Casa's own.
    """
    placed = b"z" * 43
    (tmp_path / "vm").write_bytes(placed)
    before = {p.name: (p.read_bytes(), p.stat().st_ino) for p in tmp_path.iterdir()}

    assert webhook_auth.mint_resident_secret("vm", secrets_dir=tmp_path, role="assistant") == placed

    after = {p.name: (p.read_bytes(), p.stat().st_ino) for p in tmp_path.iterdir()}
    assert after == before
    assert not (tmp_path / "vm.mint").exists()
    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "unproven"


def test_a_lost_create_race_receipts_nothing(tmp_path: Path):
    """Mutation killed: receipting without checking that WE won the link.

    `_publish` keeps whoever got to the name first rather than clobbering
    them, and that can be an operator hand-placing a credential. Certifying
    bytes we did not write would be the exact failure this mechanism prevents.

    Reproduced with a real `FileExistsError` from `os.link` — the file is
    genuinely already there — rather than by patching `os.link`, which is the
    shared module attribute.
    """
    intruder = b"an-operator-credential-placed-first"   # not casa-shaped
    (tmp_path / "vm").write_bytes(intruder)

    got = webhook_auth.mint_resident_secret("vm", secrets_dir=tmp_path, role="assistant")

    assert (tmp_path / "vm").read_bytes() == intruder, "the winner's file was kept"
    assert not (tmp_path / "vm.mint").exists(), "we receipted a file we did not write"
    assert got is None, "the intruder's value is not a valid casa secret"
    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "unproven"


def test_publish_reports_whether_this_call_created_the_name(tmp_path: Path):
    """The win flag itself, mutation-checked apart from its consumer."""
    assert webhook_auth._publish("vm", b"a" * 43, tmp_path) is True
    assert webhook_auth._publish("vm", b"b" * 43, tmp_path) is False
    assert (tmp_path / "vm").read_bytes() == b"a" * 43


def test_a_provider_slot_is_never_minted_or_receipted(tmp_path: Path):
    """Casa neither writes nor marks a slot it does not own."""
    specs = [_webhook("el", mode="timestamped_hmac", owner="provider"),
             _webhook("plain", mode="hmac_body")]

    assert rts.mint_for_specs(specs, secrets_dir=tmp_path, role="assistant") == []
    assert sorted(p.name for p in tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Every anomaly is `unproven`, and nothing is deleted to make it so
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    b"not json at all",
    b"[]",
    b'{"v": 1, "minted_by": "casa", "value_sha256": "x"}',
    b'{"v": 2, "minted_by": "plugin", "value_sha256": "x", "role": "assistant"}',
    b'{"v": 2, "minted_by": "casa", "role": "assistant"}',
    b'{"v": 2, "minted_by": "casa", "value_sha256": 12345, "role": "assistant"}',
    b'{"v": 2, "minted_by": "casa", "value_sha256": "x", "role": "../etc"}',
    b'{"v": 2, "minted_by": "casa", "value_sha256": "x", "role": 7}',
    b"",
])
def test_a_malformed_receipt_reads_unproven_and_is_not_deleted(
        tmp_path: Path, payload: bytes):
    """A receipt this module cannot parse is reported, NEVER removed.

    `_read_state` deletes a malformed rotation file; this deliberately does
    not. Speculative deletion in this directory is the whole hazard.
    """
    webhook_auth.mint_resident_secret("vm", secrets_dir=tmp_path, role="assistant")
    (tmp_path / "vm.mint").write_bytes(payload)

    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "unproven"
    assert (tmp_path / "vm.mint").read_bytes() == payload


def _rewrite_receipt(tmp_path: Path, minted: bytes, **fields) -> None:
    """Replace the receipt, keeping the digest CORRECT unless told otherwise.

    Keeping it correct is the point: a case that corrupts the digest AND the
    field under test would pass on the digest mismatch alone and prove
    nothing about the field.
    """
    rec = {"v": 2, "minted_by": "casa",
           "value_sha256": hashlib.sha256(minted).hexdigest(), "role": "assistant"}
    rec.update(fields)
    (tmp_path / "vm.mint").write_bytes(json.dumps(rec).encode("utf-8"))


def test_a_boolean_version_is_not_version_one(tmp_path: Path):
    """`True == 1` in Python, so `rec["v"] != 1` accepts `"v": true`.

    The digest here is correct, so nothing but the version check can make
    this unproven.
    """
    minted = webhook_auth.mint_resident_secret("vm", secrets_dir=tmp_path, role="assistant")
    _rewrite_receipt(tmp_path, minted, v=True)

    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "unproven"


def test_an_unexpected_receipt_key_is_unproven(tmp_path: Path):
    """The schema is exact. A receipt carrying more than it should was
    written by something that is not this version of this code."""
    minted = webhook_auth.mint_resident_secret("vm", secrets_dir=tmp_path, role="assistant")
    _rewrite_receipt(tmp_path, minted, minted_for="somebody-else")

    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "unproven"


def test_a_non_ascii_digest_reads_unproven_and_does_not_raise(tmp_path: Path):
    """`hmac.compare_digest` raises TypeError on a non-ASCII str.

    `resident_secret_provenance` is documented total, and it is read from
    `resolve_rows` on the reload report path, so an exception here would
    escape into a reload envelope rather than reading as unproven.
    """
    minted = webhook_auth.mint_resident_secret("vm", secrets_dir=tmp_path, role="assistant")
    _rewrite_receipt(tmp_path, minted, value_sha256="é" * 64)

    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "unproven"


def test_a_digest_of_the_wrong_shape_is_unproven(tmp_path: Path):
    """Uppercase hex, short, long, or not hex at all. `hexdigest()` emits
    lowercase, so anything else did not come from this writer."""
    minted = webhook_auth.mint_resident_secret("vm", secrets_dir=tmp_path, role="assistant")
    good = hashlib.sha256(minted).hexdigest()

    for bad in (good.upper(), good[:63], good + "a", "z" * 64, "", 12345):
        _rewrite_receipt(tmp_path, minted, value_sha256=bad)
        assert webhook_auth.resident_secret_provenance(
            "vm", secrets_dir=tmp_path) == "unproven", bad

    _rewrite_receipt(tmp_path, minted)          # and the good one still works
    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "casa_minted"


def test_a_symlinked_receipt_is_unproven(tmp_path: Path):
    minted = webhook_auth.mint_resident_secret("vm", secrets_dir=tmp_path, role="assistant")
    real = tmp_path / "elsewhere"
    real.write_bytes(json.dumps({
        "v": 2, "minted_by": "casa", "role": "assistant",
        "value_sha256": hashlib.sha256(minted).hexdigest()}).encode())
    (tmp_path / "vm.mint").unlink()
    (tmp_path / "vm.mint").symlink_to(real)

    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "unproven"


def test_an_absent_slot_is_absent_and_an_unreadable_one_is_not(tmp_path: Path):
    """ENOENT is the ONLY absent condition.

    Collapsing unreadable into absent would tell a later retirement that a
    transiently-faulty /data holds nothing.
    """
    assert webhook_auth.resident_secret_provenance(
        "nothing-here", secrets_dir=tmp_path) == "absent"

    (tmp_path / "adir").mkdir()
    assert webhook_auth.resident_secret_provenance(
        "adir", secrets_dir=tmp_path) == "unproven"


def test_a_receipt_without_its_secret_certifies_nothing(tmp_path: Path):
    (tmp_path / "vm.mint").write_bytes(json.dumps({
        "v": 2, "minted_by": "casa", "role": "assistant",
        "value_sha256": hashlib.sha256(b"z" * 43).hexdigest()}).encode())

    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "absent"


# ---------------------------------------------------------------------------
# The mint stays non-fatal and idempotent
# ---------------------------------------------------------------------------


def test_a_receipt_write_failure_publishes_nothing_and_is_one_failure(
        tmp_path: Path, monkeypatch):
    """#620 (receipt BEFORE link): the proof is made durable first, so a receipt
    that cannot be written is a mint that does not happen — no live file, no
    receipt, exactly one named failure — and the next pass retries it. A slot
    without its proof would 401 forever with nothing to retry it, which is
    why this inverts the earlier best-effort rule.
    """
    real_replace = os.replace

    def fail_the_receipt_only(src, dst, **kw):
        if str(dst).endswith(".mint"):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst, **kw)

    # Patched on the module's own reference, never on the shared `os` module:
    # `os.replace` is used by half the tree and a global swap is the hazard
    # class this repo has an explicit standing rule about.
    monkeypatch.setattr(webhook_auth, "os",
                        _ModuleShim(os, replace=fail_the_receipt_only))

    failures = rts.mint_for_specs([_webhook("vm")], secrets_dir=tmp_path, role="assistant")
    assert len(failures) == 1 and failures[0][0] == "vm", failures
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "absent"

    # and once the fault clears, the next pass mints, receipted
    monkeypatch.setattr(webhook_auth, "os", os)
    assert rts.mint_for_specs([_webhook("vm")], secrets_dir=tmp_path, role="assistant") == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["vm", "vm.mint"]
    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "casa_minted"


def test_a_directory_sync_fault_after_the_link_keeps_the_receipt(tmp_path: Path, monkeypatch):
    """#620 (review round 2): `_publish` can raise AFTER the live name was
    linked — the directory fsync. Unwinding the receipt then would leave a
    readable slot no certified read accepts, and a readable slot is never
    re-minted: a transient durability fault would become a permanent 401 the
    operator can only repair on the host. The receipt stands; the fault is
    reported."""
    import stat as _stat
    real_os = webhook_auth.os
    dir_syncs = []

    def fsync_dirs_fail(fd):
        # The receipt writer syncs the directory first (a fault there aborts
        # BEFORE anything is linked — the other pin); the SECOND directory
        # sync is `_publish`'s, after the link. That is the one that fails.
        if _stat.S_ISDIR(real_os.fstat(fd).st_mode):
            dir_syncs.append(fd)
            if len(dir_syncs) == 2:
                raise OSError(5, "I/O error")
        return real_os.fsync(fd)
    monkeypatch.setattr(webhook_auth, "os", _ModuleShim(os, fsync=fsync_dirs_fail))

    minted = webhook_auth.mint_resident_secret("vm", secrets_dir=tmp_path, role="assistant")

    assert minted is not None and len(minted) == 43
    assert sorted(p.name for p in tmp_path.iterdir()) == ["vm", "vm.mint"]
    assert webhook_auth.resident_secret_provenance("vm", secrets_dir=tmp_path) == "casa_minted"
    assert webhook_auth.read_certified_secret(
        "vm", role="assistant", secrets_dir=tmp_path) == minted
    # and the next pass finds a certified, readable slot — nothing to do
    assert rts.mint_for_specs([_webhook("vm")], secrets_dir=tmp_path, role="assistant") == []
    assert (tmp_path / "vm").read_bytes() == minted


def test_a_repeated_mint_changes_nothing(tmp_path: Path):
    first = webhook_auth.mint_resident_secret("vm", secrets_dir=tmp_path, role="assistant")
    before = {p.name: (p.read_bytes(), p.stat().st_ino) for p in tmp_path.iterdir()}

    assert webhook_auth.mint_resident_secret("vm", secrets_dir=tmp_path, role="assistant") == first

    after = {p.name: (p.read_bytes(), p.stat().st_ino) for p in tmp_path.iterdir()}
    assert after == before
    assert webhook_auth.resident_secret_provenance(
        "vm", secrets_dir=tmp_path) == "casa_minted"


def test_the_receipt_fits_a_maximum_length_trigger_name(tmp_path: Path):
    """`triggers.v1.json` caps a name at 248 bytes against a 255-byte
    NAME_MAX, reserving 7 for a suffix. A longer one would raise
    ENAMETOOLONG and silently leave the slot permanently unproven."""
    name = "a" * 248

    assert webhook_auth.mint_resident_secret(name, secrets_dir=tmp_path, role="assistant") is not None
    assert webhook_auth.resident_secret_provenance(
        name, secrets_dir=tmp_path) == "casa_minted"


# ---------------------------------------------------------------------------
# The retirement primitives stay total over the new sidecar
# ---------------------------------------------------------------------------


def test_retire_secret_removes_the_receipt(tmp_path: Path):
    webhook_auth.mint_resident_secret("plg-p--vm", secrets_dir=tmp_path, role="assistant")
    assert (tmp_path / "plg-p--vm.mint").exists()

    webhook_auth.retire_secret("plg-p--vm", secrets_dir=tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_prefix_retire_derives_the_base_name_through_the_receipt_suffix(tmp_path: Path):
    """Without `.mint` in the suffix strip, `plg-p--vm.mint` yields the base
    name `plg-p--vm.mint` and is retired under a name that does not exist."""
    webhook_auth.mint_resident_secret("plg-p--vm", secrets_dir=tmp_path, role="assistant")

    assert webhook_auth.retire_secrets_with_prefix(
        "plg-p--", secrets_dir=tmp_path) == ["plg-p--vm"]
    assert sorted(p.name for p in tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# The report carries the fact — on BOTH row shapes, pinned separately
# ---------------------------------------------------------------------------


def test_a_readable_row_reports_its_provenance(tmp_path: Path):
    """Mutation killed: dropping the assignment on the ordinary readable row."""
    spec = _webhook("vm")
    registry = _registry()
    registry.register_agent(role="assistant", triggers=[spec], channels=[])

    rts.mint_for_specs([spec], secrets_dir=tmp_path, role="assistant")
    rows = _rows([spec], registry, secrets_dir=tmp_path)

    assert rows["vm"]["state"] == "readable"
    assert rows["vm"]["provenance"] == "casa_minted"

    (tmp_path / "vm").write_bytes(b"z" * 43)
    rows = _rows([spec], registry, secrets_dir=tmp_path)
    # #620 (INV-TRIG-016): bytes Casa did not mint no longer authenticate a
    # casa-owned resident route — the row says what a request would do.
    assert rows["vm"]["state"] == "unproven_blocked", "refused, and reported as such"
    assert "provenance" not in rows["vm"]


def test_a_provider_declaration_reports_a_casa_token_as_casa_minted(tmp_path: Path):
    """The operator-facing point of the field, in the direction that bites.

    An operator declares `secret_owner: provider` on a name Casa previously
    minted and places nothing. The slot reads `readable` — a casa token
    satisfies the provider rule — so today the report says the setup is fine
    when the route authenticates with Casa's token, not the operator's
    credential.
    """
    casa_spec = _webhook("vm")
    rts.mint_for_specs([casa_spec], secrets_dir=tmp_path, role="assistant")

    provider_spec = _webhook("vm", mode="timestamped_hmac", owner="provider")
    registry = _registry()
    registry.register_agent(role="assistant", triggers=[provider_spec], channels=[])

    rows = _rows([provider_spec], registry, secrets_dir=tmp_path)

    assert rows["vm"]["state"] == "readable"
    assert rows["vm"]["owner"] == "provider"
    assert rows["vm"]["provenance"] == "casa_minted", (
        "the operator's provider slot is holding a CASA token")


def test_a_misrouted_row_reports_provenance_inside_its_probe(tmp_path: Path):
    """Mutation killed: dropping the assignment on the `misrouted` branch.

    It returns before the ordinary readable branch, so a single assignment on
    the latter leaves this row silently without the field — and cross-role
    adoption, the closest thing in this report to #620's own shape, is
    exactly what surfaces as `misrouted`.
    """
    spec = _webhook("vm")
    registry = _registry()
    registry.register_agent(role="butler", triggers=[spec], channels=[])
    rts.mint_for_specs([spec], secrets_dir=tmp_path, role="butler")

    # "assistant" declares the name that "butler" actually routes.
    rows = _rows([spec], registry, role="assistant", secrets_dir=tmp_path)

    assert rows["vm"]["state"] == "misrouted"
    assert rows["vm"]["probe"]["state"] == "readable"
    assert rows["vm"]["probe"]["provenance"] == "casa_minted"


def test_a_non_readable_row_carries_no_provenance(tmp_path: Path):
    """It is meaningless without bytes, and a field that appears everywhere
    stops carrying information."""
    spec = _webhook("vm")
    registry = _registry()
    registry.register_agent(role="assistant", triggers=[spec], channels=[])

    rows = _rows([spec], registry, secrets_dir=tmp_path)   # never minted

    assert rows["vm"]["state"] == "missing"
    assert "provenance" not in rows["vm"]


# ---------------------------------------------------------------------------
# #620 — what a successful removal does, and what a mere edit does not
# ---------------------------------------------------------------------------


def _mint_specs(specs, *, secrets_dir: Path, role: str = "assistant"):
    """The writer takes the role it mints for; at the base it did not."""
    try:
        return rts.mint_for_specs(specs, secrets_dir=secrets_dir, role=role)
    except TypeError:
        return rts.mint_for_specs(specs, secrets_dir=secrets_dir)


def _retire_specs(role: str, *, declared, staged, secrets_dir: Path):
    """`(retired, failed)` counts; the base has no retirement and records none."""
    fn = getattr(rts, "retire_for_role", None)
    if fn is None:
        return 0, 0
    out = fn(role, declared=list(declared), staged=set(staged), secrets_dir=secrets_dir)
    return len(out.retired), len(out.failed)


def test_successful_removal_then_recreate_gets_fresh_credential(tmp_path: Path):
    """#620 IS CLOSED by retirement at the route lifecycle, not by the mint.

    Replaces the committed characterisation that asserted inheritance. A slot
    Casa minted for a role is retired when that role's declaration no longer
    backs the name; the recreate mints FRESH bytes with a fresh receipt.

    Red at the base: the retirement records nothing and `mint_for_specs`
    returns the surviving bytes — count 0 where 1 is required, same bytes
    where different ones are.
    """
    spec = _webhook("doorbell")
    assert _mint_specs([spec], secrets_dir=tmp_path) == []
    first = (tmp_path / "doorbell").read_bytes()

    assert _retire_specs("assistant", declared=[], staged=set(), secrets_dir=tmp_path) == (1, 0)
    assert sorted(p.name for p in tmp_path.iterdir()) == []

    assert _mint_specs([spec], secrets_dir=tmp_path) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["doorbell", "doorbell.mint"]
    assert (tmp_path / "doorbell").read_bytes() != first
    assert webhook_auth.resident_secret_provenance(
        "doorbell", secrets_dir=tmp_path) == "casa_minted"


def test_delete_and_recreate_without_registration_keeps_credential(tmp_path: Path):
    """The narrow truth the old pin was really about, kept: the MINT alone never
    deletes, so a name deleted and recreated with no registration in between
    keeps its bytes — the route never left service (J1). Green at the base and
    after; it delimits the guarantee rather than establishing it.
    """
    spec = _webhook("doorbell")
    assert _mint_specs([spec], secrets_dir=tmp_path) == []
    first = (tmp_path / "doorbell").read_bytes()
    inode = (tmp_path / "doorbell").stat().st_ino

    assert _mint_specs([], secrets_dir=tmp_path) == []      # "deleted", no registration
    assert _mint_specs([spec], secrets_dir=tmp_path) == []  # recreated

    assert (tmp_path / "doorbell").read_bytes() == first
    assert (tmp_path / "doorbell").stat().st_ino == inode
    assert sorted(p.name for p in tmp_path.iterdir()) == ["doorbell", "doorbell.mint"]
