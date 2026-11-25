---
last_reviewed: 2026-11-25
---

# Store cleanup

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How Casa removes old entries from the plugin store (`/config/plugins/store/<name>/<id>/`)
and the specialist component store (`/config/specialists/store/<id>/`): what keeps an
entry, how long an unkept one waits, and when it is removed. How an entry gets into a
store is [`plugins.md`](plugins.md) and [`specialist-lifecycle.md`](specialist-lifecycle.md);
the age sweeps of staging trees and consent receipts are
[`specialist-bundle-recovery.md`](specialist-bundle-recovery.md)'s.

## Mental model

Every plugin install or update publishes an artifact directory, and every specialist
install or upgrade a component directory. `store_gc` removes the ones nothing names any
more, in two kinds of pass.

**Naming is textual.** An entry is *named* when its 64-hex id occurs anywhere in the bytes
of a reference file: the plugin registry; any regular file directly inside
`/config/specialists` or directly inside one of its child directories (a specialist's
active, prior and desired tuples, its owned-plugin sidecars, the bundle journals); and the
engagement records in `/data/engagements.json`. No field is parsed — the registry is only
checked to be a JSON object — so a file added later in those places that names an id is
covered without a change here. A reference file that does not exist counts as empty.

**Only a real directory with a 64-lowercase-hex name is a candidate.** A symlink, a staging
or import directory, or any other name is never one.

**Observing and removing are separate passes.** `observe` runs at every start and hourly
while Casa runs (the scheduler job `store_cleanup_observe`). It records, in the ledger
`/config/plugins/.store-gc.json`, when each entry was first seen unnamed, drops the row of
an entry that is named again or gone, and never removes anything. Removal happens only in
the start pass, `run_boot_sweep`, which `plugin_boot.main` runs as its last step in the
`init-plugin-store` oneshot, before `svc-casa` starts. That pass observes first and
removes only what is unnamed in that same pass and has waited long enough.

**The wait counts only while observation is continuous.** An entry waits 30 days on the
ledger. When more than two hours passed since the last observation — Casa stopped, or an
image without the cleanup ran — or the ledger is missing or unusable, every wait starts
again, because the reference files may have named an entry while nobody looked. The wait
exists because the registry is tracked in the config git repository and can be restored
to name a superseded artifact, while git holds no artifact bytes.

What this means for an operator: after a plugin update, or after a plugin or specialist is
removed, the old entry stays at least 30 days and is removed at a start after that. An
entry a recorded engagement names stays while that record stays, and a specialist's
previous generation stays while its prior tuple file names it.

## Contracts & invariants

**INV-PLUG-062**: The store cleanup removes a plugin-store or specialist-store entry only in the start pass that `init-plugin-store` runs before `svc-casa`, only when no reference file names the entry's id in that pass and the entry has been on the cleanup ledger for at least 30 days of observation with no gap over two hours; the hourly pass removes nothing, and a start pass removes nothing when the registry is not valid, when bundle reconciliation raised or reported `deferred_age_sweeps`, or when a reference source exists and cannot be read.

Enforced by `run_boot_sweep`, which returns before observing when `plugin_boot.main`
reports an invalid registry or unfinished reconciliation, and which deletes only from the
ledger the same pass's `observe` just brought up to date; `observe` has no removal path.

What it does not cover: a registry restored through git to an entry whose artifact was
already removed. That entry then reports its artifact as missing. Naming is a byte search, so an id that occurs in an unrelated
field of a reference file also keeps its entry; a file two levels below
`/config/specialists`, or under a symlinked child directory, names nothing. An artifact
the image bundles and the registry does not select is imported again at the next start
after its removal, and waits again; the shipped image bundles none.

## Failure behavior

**A reference source cannot be read.** A source that exists but cannot be read or listed,
or a registry whose bytes are not a JSON object, skips the pass: nothing is removed, the
ledger is left as it was, and a warning names the source. `observe` never raises.

**Boot says the stores may not be settled.** An invalid registry, or a bundle
reconciliation that raised or reported `deferred_age_sweeps`, skips the start pass with
an info line; the ledger is not touched.

**A removal fails.** The entry stays on the ledger, a warning names it, and the next start
tries again. Artifacts are frozen read-only, so a refused step makes the directory it
failed in writable and tries once more. An emptied plugin name directory is removed too.

**The start pass runs long.** It stops starting new removals once 30 seconds have passed
since it began; a removal already started is finished, so one very large entry can take the
pass beyond that. What is left goes at a later start, and the summary line says so.

**Anything else goes wrong in the start pass.** It is logged and the pass ends;
`run_boot_sweep` never raises, so it never holds `svc-casa` back.

## Extension points

**A new place that names a store entry.** A file inside the reference set described above
is covered as it is. A reference kept anywhere else must be added to the bytes
`_reference_bytes` reads, or the entry it names is removed once it has waited.

**A new store.** Its entries must be listed by `_list_stores` and its key added to
`_STORES`, so the ledger keeps rows for it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/store_gc.py`

**Tests**
- `tests/test_store_gc.py`
- `tests/test_plugin_boot.py`
- `tests/test_scheduled_sweeper_jobs.py`

**Related**
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/specialist-lifecycle.md`](../architecture/specialist-lifecycle.md)
- [`architecture/specialist-bundle-recovery.md`](../architecture/specialist-bundle-recovery.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
<!-- END SOURCEMAP -->
