from pathlib import Path

DOC = (
    Path(__file__).resolve().parents[1]
    / "docs/architecture/jobs-and-delivery.md"
)

EXPECTED = (
    "Enforced inside the registry, never at the call sites: the "
    "shutdown-deferral guard sits at three registry transitions — the two "
    "compatibility cancellation transitions, and the continuation compensation "
    "that settles by *deleting* the child row. What is closed is a property, "
    "not a roster of publish points: a cancellation-reasoned settlement is "
    "deferred to boot, while a success or non-cancellation verdict still "
    "commits mid-stop through whichever registry transition carries it — the "
    "next paragraph narrows *which* cancellations defer. The delivery-expiry "
    "sweeps touch delivery state alone and never execution state."
)


def test_inv_job_009_enforcement_states_property_not_publish_point_roster():
    text = DOC.read_text(encoding="utf-8")
    section = text.split("**INV-JOB-009**:", 1)[1]
    # The window ends at the NEXT invariant in the ledger shard. It ended at
    # INV-JOB-003 before the delivery half moved to its own document; on a
    # shard that no longer contains that id the cut silently returned the
    # whole remainder, which still worked but pinned by accident.
    section = section.split("**INV-JOB-010**:", 1)[0]
    enforcement = " ".join(section.strip().split("\n\n")[1].split())

    assert enforcement == EXPECTED
