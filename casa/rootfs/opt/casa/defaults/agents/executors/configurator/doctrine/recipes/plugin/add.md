# Recipe: add a plugin to the registry

A Casa plugin is a Claude Code plugin (commands / skills / hooks / MCP
servers) consumed by Casa agents. Under the unified plugin architecture the
registry (`/config/plugins/registry.json`) is the single assignment authority:
one `plugin_add` call publishes an immutable, content-addressed artifact,
installs any system requirements, assigns it to targets, and reloads + verifies
— all inside the tool.

## Ask the user

1. **Plugin name?** The plugin's own `.claude-plugin/plugin.json` `name` —
   lowercase, hyphenated. **The repo name is NOT the plugin name:** keeper
   repos follow `casa-plugin-<name>` (repo `casa-plugin-gmail` → plugin
   `gmail`; repo `casa-plugin-todo-list` → plugin `todo-list`),
   and a repo may host the plugin in a `subdir`. Never pass the repo
   basename as `name`. Sources of truth, in order: the plugin-developer
   completion handoff (it states the plugin name), or the repo's
   `plugin.json`. `plugin_add` hard-rejects a wrong name with
   `name_mismatch` + the canonical `manifest_name` — retry the ADD with
   that exact name, don't guess again. (On `plugin_update` a
   `name_mismatch` means the NEW manifest renamed the plugin — that is
   never a retry: a rename is an explicit migration, `plugin_add` under
   the new name + `plugin_remove` of the old, operator-confirmed.)
2. **Source repo?** `<owner>/<repo>` (or a `https://github.com/<owner>/<repo>`
   URL). For a plugin-developer build, take it from that engagement's topic.
3. **Pin (ref)?** The release tag (`vX.Y.Z`) for a plugin-developer build —
   take it, plus the `revision` sha, verbatim from that engagement's
   completion handoff. (A sha or branch is acceptable only for a manual add
   of a third-party repo with no release handoff.)
4. **Targets?** One or more of `resident:<role>`, `specialist:<role>`,
   `executor:<type>` (e.g. `specialist:finance`).

## Do it

Call `plugin_add(name, repo, ref, subdir?, targets, expected_revision?)`.
For a plugin-developer handoff, ALWAYS pass `expected_revision` = the
producer's freshly-built `revision` — a tag that moved since the build
aborts hard (`revision_mismatch`) before anything is installed or activated.
There is **no version argument** — the version is derived from the fetched
`plugin.json` (a caller-supplied version is exactly the stale-code bug this
architecture removes).

The tool, internally and in order: resolves `ref`→commit (a 404 is a hard
`ref_not_found`, never silent), fetches + validates + atomically publishes the
artifact, installs system requirements BEFORE activation, writes the registry
entry, then reloads the affected in-casa agents and verifies desired==active.
**No separate casa_reload is needed** — reload + verify happen inside
`plugin_add` (and `plugin_update` / `plugin_assign` / `plugin_unassign` /
`plugin_remove`).

## Result

The result carries `artifact_id`, `version`, `revision`, `granted_tools`,
`required_env_vars`, and a `verify` summary. If `required_env_vars` is
non-empty, wire the secrets next (see `secrets.md`) — the plugin's MCP server
won't start without them. Then read `verify_plugin_state(name)`: `ready:true`
means every target agrees. The result carries the same phase fields as
`plugin_update` (`activation_committed` / `runtime_ready`) — on
`activation_committed:true, runtime_ready:false` the registry entry exists
but a target didn't come up; retry the reload/verify, not the add. Exception:
`ok:true` with a non-empty `pending_targets` (e.g. `["specialist:weather"]`) is
**success, not a failure** — the plugin targets a specialist that is not
installed yet. `plugin_add` (this recipe) is needed only for an
OPERATOR-owned plugin: a specialist with bundled or repo-declared plugin
dependencies (its manifest's `dependencies`, `kind: "plugin/implementation"`)
installs them in the SAME flow as the specialist itself —
`specialist_install_inspect` + `specialist_install_commit`, see
`recipes/specialist/install.md` — never a separate `plugin_add`. Do NOT
retry, work around, or remove the plugin; if `pending_targets` names a
specialist slug, proceed to that specialist's install and it will pick the
plugin up automatically as one of its own declared dependencies (or, if the
specialist is never going to declare it, the plugin simply stays pending —
that is a valid end state, not an error). Report the outcome and
`emit_completion(...)`.

## Setup is Casa's, not yours to route

Some plugins ship an MCP **setup tool** (naming convention `setup_*`, e.g.
`setup_elevenlabs_voicemail`) that (re)points an external service at Casa —
typically writing a freshly-minted per-trigger secret into the external
caller's config. Install and consent re-approval mint FRESH secrets, so the
external side holds no usable credential from this install until that tool
runs. The operator must never need to remember a follow-up incantation.

**Casa runs it. You do not route it anywhere.** A plugin that declares
`casa.setupTool` gets a durable per-artifact setup obligation, which Casa
releases once EVERY consent that artifact declares has been approved — or
immediately, when Casa can establish it needs none. One declined consent
withholds it (the setup tool is argument-free and cannot target a subset), and a
consent Casa cannot currently ask about — an unassigned target, a role without
the webhook channel — leaves the run pending rather than counting as none. Because plugin tools
surface only on the plugin's target agents, never in this engagement, Casa
dispatches its own turn to one of them; the operator hears the setup outcome in
a separate message. The `plugin_add` result reports the declared `setup_tool` so
you can say what is queued.

**A trigger/callback/event consent DM that expired or was missed is re-issued
ONLY by `consent_reprompt`** (#494). The server-posted DM keyboard is the sole
surface whose Approve commits; a consent question relayed through an ask (this
engagement's ask facility, or the assistant's `ask_user`) renders an identical
Approve/Deny, accepts the tap, acks it — and commits NOTHING, wedging setup
behind a consent the operator believes they gave. So NEVER re-ask a consent
yourself: when health shows `*_pending_ack` and no keyboard is live, call
`consent_reprompt` and relay its result. It reports consents the operator
explicitly DENIED without re-asking them (a new mutation or `casa_reload`
re-asks) and fails typed (`delivery_failed`) when no keyboard could actually
be delivered — do not report success it does not claim.

So: emit NO `next_steps` entry for setup, do not ask an agent to run it, and
do not ask the operator to. Your completion `text` says that setup is queued
and that its own result is what to go on. Make no claim of your
own about whether the integration works, in either direction (#443) — Casa
cannot see the external side, and announcing a fault that does not exist makes
the operator authorize something that needed no authorizing, which costs the
same trust as missing a real one. Do not over-read the tool's result either:
the authoring contract requires idempotent provisioning and does NOT require
the tool to test what it provisioned (see the plugin-developer `ingress.md`).

Do not hunt for a tool name. A plugin whose setup tool is named only in a
producer handoff or a README — with no `casa.setupTool` in the manifest — has
**no supported automatic path** before v1.0. Say plainly in `text` that the
plugin declares no setup hook, so its provisioning step (if any) is unrun;
never guess a name and never invent a hand-back to compensate.

Setup tools in this contract are **argument-free and idempotent** (that is the
plugin-developer authoring doctrine). Wiring is global to the plugin, so Casa
runs it ONCE, not per target.

A plugin registry entry a specialist's install/upgrade OWNS (registry name
`<slug>.<name>`) can never be managed through this recipe or `remove.md` /
`update.md` / an assign-unassign step: `plugin_add` itself can't collide with
one (owned names are scoped), but `plugin_update` / `plugin_remove` /
`plugin_assign` / `plugin_unassign` all refuse an owned entry outright with
`kind: "owned_by_specialist"` (plus the owning slug). Use
`specialist_upgrade` / `specialist_uninstall` on the SLUG instead of trying to
touch the owned entry directly.
