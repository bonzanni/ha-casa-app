"""#609 — resident webhook secrets: minted at registration, reported truthfully.

Before this, the ONLY mint for a resident webhook secret lived inside request
verification, so a `static_header` trigger was created, committed and reported
live while its secret did not exist — and the only thing that could create it
was a call that could not succeed without it.

Two halves are proven here. The WRITER must create exactly the slots it owns
and never touch anything else. The READER must describe what a REQUEST would
actually do, which is a different question from what the declaration says, and
the two do diverge in the live tree.
"""

from __future__ import annotations

import os
import time
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


def _webhook(name: str, *, mode: str = "static_header", owner: str = "casa",
             header: str = "X-API-Key", tolerance: int = 300,
             clearance: str = "public") -> TriggerSpec:
    return TriggerSpec(
        name=name, type="webhook", clearance=clearance,
        auth={"mode": mode, "header": header, "tolerance_secs": tolerance,
              "secret_owner": owner},
    )


def _rows(specs, registry, role="assistant", *, secrets_dir, usable=True):
    snapshot = rts.snapshot_rows(
        specs=specs, registry=registry, role=role, global_secret_usable=usable)
    return {r["name"]: r for r in rts.resolve_rows(snapshot, secrets_dir=secrets_dir)}


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def test_the_mint_creates_exactly_the_casa_owned_secret_backed_slots(tmp_path: Path):
    """Red case: drop the mode filter and an `hmac_body` file appears (it rides
    the ONE global secret and must write nothing here); drop the owner filter
    and a provider file appears — Casa pre-empting a slot the operator fills by
    hand, which it can neither regenerate nor import."""
    specs = [
        TriggerSpec(name="heartbeat", type="cron", schedule="0 * * * *"),
        TriggerSpec(name="daily", type="interval", minutes=60),
        _webhook("plain", mode="hmac_body"),
        _webhook("vm"),
        _webhook("el", mode="timestamped_hmac", owner="provider"),
    ]
    assert rts.mint_for_specs(specs, secrets_dir=tmp_path, role="assistant") == []
    # #620: a mint now creates TWO artifacts — the secret and its value-bound
    # receipt. Exact equality still catches any slot the filters should have
    # excluded, and now also catches a receipt written for one of them.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["vm", "vm.mint"]
    assert (tmp_path / "vm").stat().st_size == 43


def test_the_mint_registers_the_value_for_log_redaction(tmp_path: Path):
    """The lazy path registered every minted value with the redactor. Minting
    earlier must not lose that, or the value is unredacted in this process
    until its first request — and forever for a trigger never called."""
    import log_redact

    rts.mint_for_specs([_webhook("vm")], secrets_dir=tmp_path, role="assistant")
    value = (tmp_path / "vm").read_text()
    assert log_redact.redact(f"boom name=vm value={value}") == "boom name=vm value=«redacted»"


def test_a_slot_that_is_not_absent_is_never_written_over(tmp_path: Path):
    """Only `absent` may be minted into. `read_secret` collapses absent,
    unreadable and present-but-invalid to None; minting on that re-enters
    `_publish` for a file that already exists — raising on a read-only
    directory on every pass, forever, with no Casa surface able to clear it."""
    placed = b"operator-placed-opaque-value"
    (tmp_path / "el").write_bytes(placed)
    (tmp_path / "vm").write_bytes(b"tooshort")
    before = {p.name: (p.read_bytes(), p.stat().st_ino) for p in tmp_path.iterdir()}

    assert rts.mint_for_specs(
        [_webhook("el", mode="timestamped_hmac", owner="provider"), _webhook("vm")],
        secrets_dir=tmp_path, role="assistant") == []

    after = {p.name: (p.read_bytes(), p.stat().st_ino) for p in tmp_path.iterdir()}
    assert after == before


def test_a_mint_failure_is_reported_and_never_raised(tmp_path: Path):
    """Red case: let it raise and boot dies — an exception escaping `main()`
    does not restart Casa, `svc-casa/finish` STOPS the app. Every request to
    the trigger 401s either way, so trading Telegram, voice and every reminder
    for it is the wrong trade."""
    os.chmod(tmp_path, 0o555)
    try:
        failures = rts.mint_for_specs([_webhook("vm"), _webhook("other")],
                                      secrets_dir=tmp_path, role="assistant")
    finally:
        os.chmod(tmp_path, 0o755)
    assert [name for name, _ in failures] == ["vm", "other"], (
        "every slot is attempted; one failure does not abandon the rest")
    assert all("PermissionError" in reason for _, reason in failures)


def test_a_stuck_slot_is_a_report_row_not_a_failure(tmp_path: Path):
    """`ensure_secret` returns None WITHOUT raising when a file exists that is
    not valid for the owner. Counting that as a failure made the reload raise
    for that role on every later call, forever, and the only exit was host
    filesystem access the operator does not have."""
    (tmp_path / "vm").write_bytes(b"not-a-casa-token")
    assert rts.mint_for_specs([_webhook("vm")], secrets_dir=tmp_path, role="assistant") == []
    assert (tmp_path / "vm").read_bytes() == b"not-a-casa-token"


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


def test_every_state_reports_what_a_request_would_do(tmp_path: Path):
    """The four probe-derived states plus the two global-secret ones, each
    against a slot in that exact condition."""
    registry = _registry()
    specs = [
        TriggerSpec(name="heartbeat", type="cron", schedule="0 * * * *",
                    channel="main"),
        _webhook("plain", mode="hmac_body"),
        _webhook("vm"),
        _webhook("gone"),
        _webhook("el", mode="timestamped_hmac", owner="provider"),
        _webhook("wedged"),
    ]
    registry.register_agent(role="assistant", triggers=specs, channels=["main"])
    rts.mint_for_specs([s for s in specs if s.name != "gone"], secrets_dir=tmp_path, role="assistant")
    (tmp_path / "wedged").unlink()
    (tmp_path / "wedged").write_bytes(b"not-a-casa-token")
    (tmp_path / "gone").unlink(missing_ok=True)

    rows = _rows(specs, registry, secrets_dir=tmp_path)
    assert {n: r["state"] for n, r in rows.items()} == {
        "heartbeat": "not_applicable",
        "plain": "global_secret",
        "vm": "readable",
        "gone": "missing",
        "el": "awaiting_import",
        "wedged": "invalid",
    }
    assert "#621" in rows["el"]["detail"], (
        "awaiting_import must say no Casa surface can place it, not prescribe "
        "a remedy the operator cannot perform")
    assert "43" in rows["wedged"]["detail"], "invalid must carry the byte count"


def test_an_hmac_body_trigger_is_not_called_healthy_when_the_global_secret_is_blank(
        tmp_path: Path):
    """Red case: report `not_applicable` for hmac_body and this reads clean
    while every request to the route 401s permanently. The global secret is
    blank when the option holds an unresolved op:// reference or generation
    failed — both reachable, and the 401 is already a shipped test."""
    registry = _registry()
    specs = [_webhook("plain", mode="hmac_body")]
    registry.register_agent(role="assistant", triggers=specs, channels=[])

    assert _rows(specs, registry, secrets_dir=tmp_path, usable=True)["plain"]["state"] \
        == "global_secret"
    absent = _rows(specs, registry, secrets_dir=tmp_path, usable=False)["plain"]
    assert absent["state"] == "global_secret_absent"
    assert "401" in absent["detail"]
    assert webhook_auth.verify(
        "hmac_body", body=b"{}", headers={"X-Webhook-Signature": "whatever"},
        secret=b"", header_name="X-Webhook-Signature", tolerance_secs=300,
        now=0) is False


def test_a_route_the_declaration_no_longer_names_is_still_reported(tmp_path: Path):
    """The dangerous half. A bare `casa_reload(scope="policies")` replaces
    role_configs without re-registering, so the declaration can lose a webhook
    the registry still routes: the request serves 200 and dispatches a turn
    while a declaration-derived report shows nothing at all — the cleanest
    possible picture over a live undeclared route.

    Red case: take the row set from the declaration alone and this row
    disappears entirely."""
    registry = _registry()
    registry.register_agent(role="assistant", triggers=[_webhook("vm")], channels=[])
    rts.mint_for_specs([_webhook("vm")], secrets_dir=tmp_path, role="assistant")

    rows = _rows([], registry, secrets_dir=tmp_path)  # declaration lost it

    assert registry.get_webhook_target("vm") == "assistant", "premise: still routed"
    assert rows["vm"]["state"] == "routed_undeclared"


def test_a_declared_but_unrouted_trigger_says_so(tmp_path: Path):
    """The mirror: declared, never registered, so requests 404. Distinct from
    `missing`, whose remedy is a reload rather than a restart."""
    rows = _rows([_webhook("vm")], _registry(), secrets_dir=tmp_path)
    assert rows["vm"]["state"] == "unregistered"


@pytest.mark.parametrize(
    ("field", "changed"),
    [("clearance", {"clearance": "private"}), ("header", {"header": "X-Other"}),
     ("tolerance_secs", {"tolerance": 60}), ("secret_owner", {"owner": "provider"}),
     ("mode", {"mode": "timestamped_hmac"})],
)
def test_a_divergence_in_any_value_a_request_reads_is_misrouted(
        tmp_path: Path, field: str, changed: dict):
    """The report's authority is the REGISTRY — what a request is verified
    with — not the declaration. Comparing a narrower tuple reads CLEAN while
    the route behaves differently from the file: measured, a route running at
    `private` while triggers.yaml says `public` still served 200 and stamped
    the turn at the registered clearance.

    Red case: drop this field from the compare and exactly this parameter goes
    green while the divergence stands."""
    registry = _registry()
    registry.register_agent(role="assistant", triggers=[_webhook("vm")], channels=[])
    rts.mint_for_specs([_webhook("vm")], secrets_dir=tmp_path, role="assistant")

    rows = _rows([_webhook("vm", **changed)], registry, secrets_dir=tmp_path)

    assert rows["vm"]["state"] == "misrouted", field
    assert rows["vm"]["effective"] != rows["vm"]["declared"]
    assert "probe" in rows["vm"], (
        "a routing divergence must never hide the file's own condition")


def test_a_name_routed_by_another_role_is_misrouted_not_readable(tmp_path: Path):
    """The secrets directory is keyed by NAME alone, with no role namespacing,
    so a row that ignored the target role would describe one role's file while
    another role's route serves it."""
    registry = _registry()
    registry.register_agent(role="butler", triggers=[_webhook("vm")], channels=[])
    rts.mint_for_specs([_webhook("vm")], secrets_dir=tmp_path, role="assistant")

    rows = _rows([_webhook("vm")], registry, role="assistant", secrets_dir=tmp_path)
    assert rows["vm"]["state"] == "misrouted"
    assert rows["vm"]["effective"]["role"] == "butler"
    assert rows["vm"]["declared"]["role"] == "assistant"


def test_the_summary_counts_states_and_never_claims_readiness(tmp_path: Path):
    """A boolean rollup has to decide what "ready" means for
    `awaiting_import` and `global_secret_absent`, and every answer to that was
    wrong in a different direction. Counts state the facts and let the reader
    decide."""
    registry = _registry()
    specs = [_webhook("vm"), _webhook("el", mode="timestamped_hmac", owner="provider")]
    registry.register_agent(role="assistant", triggers=specs, channels=[])
    rts.mint_for_specs(specs, secrets_dir=tmp_path, role="assistant")

    snapshot = rts.snapshot_rows(specs=specs, registry=registry, role="assistant",
                                 global_secret_usable=True)
    rows = rts.resolve_rows(snapshot, secrets_dir=tmp_path)
    assert rts.summarize(rows) == {"awaiting_import": 1, "readable": 1}
    assert all("ready" not in key for key in rts.summarize(rows))


def test_no_row_carries_secret_bytes(tmp_path: Path):
    """The report rides `POST /admin/reload` as well as the tool, so a row that
    carried the value would put bearer material on a surface `log_redact` does
    not cover. Byte counts and errnos only."""
    registry = _registry()
    specs = [_webhook("vm")]
    registry.register_agent(role="assistant", triggers=specs, channels=[])
    rts.mint_for_specs(specs, secrets_dir=tmp_path, role="assistant")
    value = (tmp_path / "vm").read_text()

    rows = _rows(specs, registry, secrets_dir=tmp_path)
    assert value not in repr(rows)
    assert rows["vm"]["state"] == "readable"


# ---------------------------------------------------------------------------
# The report and `registered` must describe the SAME role, from the same two
# places. `casa_reload_triggers` falls back to the specialist registry when a
# role is not a resident; a report that consulted only `role_configs` reported
# every one of a specialist's live webhooks as routed-but-undeclared, in the
# same envelope whose `registered` list named them. (Sol, diff review.)
# ---------------------------------------------------------------------------


async def test_a_specialists_registered_webhook_is_not_reported_undeclared(tmp_path: Path):
    """Red case: drop the specialist fallback in `_trigger_secret_report` and
    this reads `routed_undeclared` while `registered` in the same payload
    names the trigger — an envelope that contradicts itself."""
    import reload as reload_mod

    registry = _registry()
    spec = _webhook("special-hook")
    registry.register_agent(role="finance", triggers=[spec], channels=[])
    rts.mint_for_specs([spec], secrets_dir=tmp_path, role="finance")

    class _Specialists:
        def all_configs(self):
            return {"finance": type("Cfg", (), {"triggers": [spec]})()}

    runtime = type("RT", (), {
        "role_configs": {},                      # a specialist is NOT here
        "specialist_registry": _Specialists(),
        "trigger_registry": registry,
        "webhook_global_secret_usable": True,
    })()

    import trigger_reconcile
    original = trigger_reconcile.SECRETS_DIR
    trigger_reconcile.SECRETS_DIR = tmp_path
    try:
        snapshot = reload_mod._trigger_secret_snapshot(runtime, "finance")
        report = await reload_mod._trigger_secret_probe(snapshot)
    finally:
        trigger_reconcile.SECRETS_DIR = original

    rows = {r["name"]: r for r in report["trigger_secrets"]}
    assert registry.get_webhook_target("special-hook") == "finance", "premise: routed"
    assert rows["special-hook"]["state"] == "readable"


async def test_the_report_probes_the_filesystem_off_the_event_loop(tmp_path: Path):
    """The condition this release exists for is a `/data` that is full,
    read-only or HUNG. A hung one must not stall the loop from inside the very
    report written to explain it — so the snapshot is synchronous and the
    probes go to a worker thread.

    Asserted as an OUTCOME, not an arrangement: a concurrent task must make
    progress while the probes are blocked. Run the probes inline and the
    counter is 0.

    The sibling property — that the probe also runs after BOTH reload locks are
    released, so a hung probe cannot wedge reload admission — is structural:
    `dispatch` takes the snapshot inside the lock and calls the probe after its
    `finally`. Moving a hang off the loop and into the lock only relocates it.
    """
    import asyncio

    import reload as reload_mod
    import trigger_reconcile

    registry = _registry()
    spec = _webhook("vm")
    registry.register_agent(role="assistant", triggers=[spec], channels=[])
    rts.mint_for_specs([spec], secrets_dir=tmp_path, role="assistant")
    runtime = type("RT", (), {
        "role_configs": {"assistant": type("Cfg", (), {"triggers": [spec]})()},
        "specialist_registry": None, "trigger_registry": registry,
        "webhook_global_secret_usable": True,
    })()

    real_probe = webhook_auth.probe_secret

    def _slow_probe(*a, **kw):
        time.sleep(0.30)
        return real_probe(*a, **kw)

    ticks = 0

    async def _tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    original_dir, webhook_auth.probe_secret = trigger_reconcile.SECRETS_DIR, _slow_probe
    trigger_reconcile.SECRETS_DIR = tmp_path
    ticker = asyncio.create_task(_tick())
    try:
        snapshot = reload_mod._trigger_secret_snapshot(runtime, "assistant")
        report = await reload_mod._trigger_secret_probe(snapshot)
    finally:
        ticker.cancel()
        webhook_auth.probe_secret = real_probe
        trigger_reconcile.SECRETS_DIR = original_dir
        await asyncio.gather(ticker, return_exceptions=True)

    assert report["trigger_secrets"][0]["state"] == "readable"
    assert ticks >= 5, f"the loop was blocked during the probes (ticks={ticks})"


def test_the_snapshot_half_touches_no_filesystem(tmp_path: Path):
    """It runs UNDER the reload lock, so it must do no IO at all — otherwise a
    hung `/data` wedges reload admission even with the probes moved off. Any
    read would go through `probe_secret`, so assert it is never reached."""
    import reload as reload_mod
    import trigger_reconcile

    registry = _registry()
    spec = _webhook("vm")
    registry.register_agent(role="assistant", triggers=[spec], channels=[])
    runtime = type("RT", (), {
        "role_configs": {"assistant": type("Cfg", (), {"triggers": [spec]})()},
        "specialist_registry": None, "trigger_registry": registry,
        "webhook_global_secret_usable": True,
    })()

    calls = []
    real_probe = webhook_auth.probe_secret
    webhook_auth.probe_secret = lambda *a, **kw: calls.append(a) or real_probe(*a, **kw)
    original = trigger_reconcile.SECRETS_DIR
    trigger_reconcile.SECRETS_DIR = tmp_path
    try:
        snapshot = reload_mod._trigger_secret_snapshot(runtime, "assistant")
    finally:
        webhook_auth.probe_secret = real_probe
        trigger_reconcile.SECRETS_DIR = original

    assert calls == [], "the snapshot half read the filesystem"
    assert [r["name"] for r in snapshot] == ["vm"], "it still produced the row"


# ---------------------------------------------------------------------------
# #620 — mint provenance (INV-TRIG-014)
#
# RED CASE. Specified externally by Sol, accepted externally by Terra; frozen.
# Do not edit it — if it is wrong, it gets re-specified and re-accepted.
#
# What it pins is the SUBSTANCE of the invariant, not the existence of a field:
# a receipt certifies the exact BYTES it was written for, never merely its own
# presence. `_valid_value("provider", <a 43-byte casa token>)` is True at this
# SHA, so nothing separates an operator credential from a Casa token by shape —
# a presence-only receipt would licence a future retirement to destroy a
# credential Casa can neither regenerate nor import (#621).
# ---------------------------------------------------------------------------


def test_resident_provenance_is_value_bound_not_receipt_presence(
        tmp_path: Path, monkeypatch):
    minted = b"C" * 43
    replacement = b"P" * 43
    monkeypatch.setattr(
        webhook_auth.secrets, "token_urlsafe",
        lambda n: minted.decode("ascii"),
    )

    spec = _webhook("vm")
    registry = _registry()
    registry.register_agent(role="assistant", triggers=[spec], channels=[])

    assert rts.mint_for_specs([spec], secrets_dir=tmp_path, role="assistant") == []

    names = sorted(p.name for p in tmp_path.iterdir())
    assert len(names) == 2
    assert names == ["vm", "vm.mint"]
    assert (tmp_path / "vm").read_bytes() == minted

    receipt = (tmp_path / "vm.mint").read_bytes()
    assert (tmp_path / "vm").write_bytes(replacement) == 43
    assert (tmp_path / "vm").read_bytes() == replacement
    assert (tmp_path / "vm.mint").read_bytes() == receipt

    rows = rts.resolve_rows(
        rts.snapshot_rows(
            specs=[spec],
            registry=registry,
            role="assistant",
            global_secret_usable=True,
        ),
        secrets_dir=tmp_path,
    )

    assert len(rows) == 1
    # #620 (INV-TRIG-016): a casa-owned resident slot whose bytes the receipt no
    # longer certifies is REFUSED at the request and reported as such — the
    # row's state carries the fact, and the value-bound reading is `unproven`.
    assert rows[0]["state"] == "unproven_blocked"
    assert webhook_auth.resident_secret_provenance("vm", secrets_dir=tmp_path) == "unproven"
    assert sum(row.get("provenance") == "casa_minted" for row in rows) == 0
