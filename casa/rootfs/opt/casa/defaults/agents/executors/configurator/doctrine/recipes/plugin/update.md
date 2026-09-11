# Recipe: update a registered plugin to a new ref

Use `plugin_update(name, new_ref, expected_revision)` when a plugin repo has
a new release you want Casa to run. It re-publishes the artifact from
`new_ref` (same source repo/subdir), installs any new system requirements
before moving the registry pointer, then reloads + verifies. The **version
is derived from the fetched manifest** — you never pass it.

## What `new_ref` must be (v0.74.0)

- `new_ref` is the **producer's handed-off `ref` verbatim** — the `vX.Y.Z`
  release tag from the plugin-developer completion. **Never** infer a tag
  from the version, **never** reuse the registry entry's existing ref,
  **never** substitute `master`/`main` or a bare sha when a tag was handed
  off.
- **No ref handed off ⇒ producer error.** Stop and surface it — do not
  guess. The producer must re-run its release ritual and hand off
  `ref` + `revision` + `version`.
- **Always pass `expected_revision`** = the producer's handed-off `revision`
  (40-hex sha). A tag that moved between the build and this pin aborts hard
  (`revision_mismatch`) before anything is installed or activated. A
  `tag_version_mismatch` means the tag doesn't match the remote
  `plugin.json.version` — also a producer error; surface it.

## Do it

1. Confirm the plugin is registered: `plugin_list()` (or
   `verify_plugin_state`).
2. Call `plugin_update(name, new_ref=<handed-off tag>,
   expected_revision=<handed-off sha>)`.
3. Assert the returned `revision` and `version` equal the handoff
   (`revision` compares as lowercase 40-hex; the registry stores
   `git:<sha>`).
4. Read the **phase fields** — they say what actually happened and what (if
   anything) to retry:
   - `ok:true` (`activation_committed:true, runtime_ready:true`) — done.
     Report readiness over the targets Casa SERVES — read them from
     `verify_plugin_state`'s `desired.targets` — and never say "every target
     runs the new artifact": a target listed under `desired.ignored_targets`
     (for now, a worker assignment on an operator-installed plugin) runs
     nothing, and saying otherwise reports an activation that never happened.
     Name any ignored target in the same breath. Then `emit_completion(...)`.
   - `activation_committed:false` — nothing changed (resolve/guard/publish
     failed; `kind` says why: `ref_not_found`, `revision_mismatch`,
     `tag_version_mismatch`, `resolve_auth_failed`, `source_empty`,
     `resolve_unavailable` — the last carries `retry_after_s` when GitHub
     asked to wait). Fix the cause; retrying the same call is safe.
   - `activation_committed:true, runtime_ready:false` — **the pin already
     moved.** Do NOT repeat `plugin_update` as if nothing happened. The
     remedy is a reload/verify retry: `casa_reload(scope="agent",
     role=<affected role>)` then `verify_plugin_state(name)`. A persisting
     `reload_required` on a target means that agent is still bound to the
     previous artifact — surface it, do not mask it.
   - `ok:true` with a non-empty `pending_targets` — success: those
     specialist targets are not installed yet (the documented
     plugin-before-specialist order). Nothing to retry; the specialist
     install activates them.

## Why this is safe

Artifacts are content-addressed by provenance, so a new commit is always a
NEW `artifact_id` even if the manifest `version` field didn't change — the
stale-version-cache bug is structurally impossible. Old artifacts are
retained (GC handles them later). Any mid-flight executor engagement keeps
running its RECORDED artifact; verify lists those under
`sessions_on_previous_artifact` as informational (not a failure) — they pick
up the new code on their next launch.

**No separate casa_reload is needed on the happy path** — `plugin_update`
reloads + verifies internally. Report the outcome and `emit_completion(...)`.

## Setup is Casa's, not yours to route

An update re-mints per-trigger secrets (consent re-approval rotates them), so
a plugin whose credential is artifact-bound is left pointing the external
service at STALE credentials until its **setup tool** runs.

**Casa runs it. You do not route it anywhere.** A plugin that declares
`casa.setupTool` gets a durable per-artifact setup obligation, which Casa
releases once EVERY consent that artifact declares has been approved — or
immediately, when Casa can establish it needs none. One declined consent
withholds it (the setup tool is argument-free and cannot target a subset), and a
consent Casa cannot currently ask about — an unassigned target, a role without
the webhook channel — leaves the run pending rather than counting as none. The `plugin_update`
result reports the declared `setup_tool` so you can say what is queued. Emit NO
`next_steps` entry for it, do not ask an agent to run it, and do not ask the
operator to.

The operator hears the setup outcome in a separate message from whichever
agent Casa dispatches to. Your completion `text` should say that setup is
queued and that the setup tool's own result is what to go on.

A plugin whose setup tool is named only in a producer handoff or a README —
with no `casa.setupTool` in the manifest — has **no supported automatic
path** before v1.0. Do not hunt for the tool name and do not invent a
hand-back: say plainly in `text` that the plugin declares no setup hook, so
its provisioning step (if any) is unrun.

**Make no claim about the integration's state, in either direction (#443).**
Casa does not know it. Not every plugin's credential is artifact-bound: one
gated by `casa.callbacks` keeps its consent ack across an update (that ack binds
the declaration, not the artifact) and holds its credential outside the replaced
artifact, so it is very often still serving throughout. Casa cannot tell which
from the outside, and announcing a fault that does not exist is as bad as
missing one — it makes the operator authorize something that needed no
authorizing. So in `text` say only that the setup tool still needs to run and
that its own result is what to go on.

Do not promise more of that result than it carries. The setup tool's contract is
idempotent **provisioning** — argument-free, re-runnable, `setup_`-prefixed (see
the plugin-developer `ingress.md`) — and it is not *required* to test what it
provisioned. Some tools may check more than that; only the tool's own output
says. So report what it returns, and do not translate it into a verdict on the
connection.
