# Recipe: reconcile overwritten config after a default sync

**When:** the operator says "reconcile config" / "carry over my changes" / asks about a
heads-up that an update overwrote customizations, OR you are asked to review config drift
after a deploy.

## Background

On every boot the default-sync reconciler makes image-default-owned files under
`/config/agents/**` and `/config/policies/**` track the shipped defaults. On a true
conflict (both the image and the live file changed the same file since the last sync), or
when a kept-live file is invalid against a newly tightened schema, **the image wins** so
casa always boots — but the operator's previous version is preserved first:

- Normal path: committed to the `/config` git repo **before** the overwrite. The report
  records the `pre_sync_sha` of that commit.
- Degraded path (git unavailable): saved next to the file as `<file>.casabak`.

## Steps

(You have no shell — every step below uses Read/Edit and typed tools only.)

1. **Read the report** with the Read tool: `/data/config-sync-report.json` (this single
   file is readable by policy). The fields that mean "a customization was overwritten"
   are `conflicts[]`, `schema_forced[]`, and `casabak[]`. (`updated[]`/`deleted[]` are
   routine default-tracking — nothing to reconcile.)

2. **Show the operator what was lost.** For a `casabak` entry: Read BOTH
   `/config/<path>.casabak` (their previous content) and `/config/<path>` (the new
   default) and summarize the meaningful differences in-topic. For a `conflicts[]` /
   `schema_forced[]` entry with a `pre_sync_sha`: the previous content is preserved in
   the `/config` git history at that commit — you cannot render git diffs yourself, so
   tell the operator the file and `pre_sync_sha`, describe the CURRENT content, and ask
   them what of their customization should carry forward (they can view the old version
   via the HA terminal if needed).

3. **Ask the operator** whether to keep the new default as-is, or carry their change
   forward on top of it. Do not assume — present what you found and let them decide.

4. **If carrying forward:** re-apply the operator's intent on top of the new default using
   the normal per-artifact edit recipes (e.g. `voice/edit`, `prompt/edit`,
   `executor/edit-definition`). Re-applying *intent* on the new base is safer than blindly
   restoring the old file, which may reintroduce the very field a schema-tightening
   removed.

5. **Validate + reload:** after edits, the standard config-commit gate runs schema
   validation; then run the reload each edit recipe you re-applied through names (see
   `reload.md` "What requires what" — e.g. `agent` for a voice edit, `triggers` for a
   trigger prompt, `executors` for an executor definition). `scope=config_sync` re-runs the
   sync with its own `agents` + `policies` cascade; it does not stand in for those reloads,
   and an edit it does not cover is force-reloaded at completion. Never restore content that
   fails validation — that is exactly what the backstop overwrote to keep casa booting.

6. **Report leftovers:** you cannot delete files. List any `<file>.casabak` sidecars you
   reconciled and tell the operator they can be removed manually (or left — the git
   history is the durable archive either way).

## Notes

- Carry-over is **always operator-initiated and post-boot.** Never block boot on it.
- If the operator wants nothing carried over, just confirm — the new defaults already
  apply; mention any `.casabak` sidecars for manual cleanup.
