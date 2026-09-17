# Casa conventions for plugin authors

## Repo + naming

- One plugin = one GitHub repo. Never group plugins into a monorepo.
- Default repo name: `casa-plugin-<slug>`. `<slug>` is the plugin's `name`
  field in `.claude-plugin/plugin.json` (kebab-case, no `casa-plugin-`
  prefix inside `plugin.json`).
- **Repos are created PRIVATE** (`gh repo create … --private`). Casa installs
  plugins from private repos (the in-container `GITHUB_TOKEN` authenticates the
  clone), so private is sufficient for the user's own agents. Making a plugin
  public — to *share* it beyond this Casa — is a deliberate later step the user
  runs themselves (`gh repo edit <repo> --visibility public`); Claude Code
  hard-blocks creating a public repo from within an engagement.
- Ship `origin` = just-created user-owned repo; `gh repo create --clone`
  sets this for you.

## Commit style

Match the superpowers convention:
`feat|fix|chore|test(<area>): <one-line>`

Examples:
- `feat(skill): teach Claude when to identify faces`
- `feat(mcp): wrap AWS Rekognition search_faces_by_image`

## What Casa consumes

Casa adds your plugin to its registry via the configurator's `plugin_add` /
`plugin_update` tools, which publish an immutable content-addressed artifact
from your pinned commit. That means:

- `.claude-plugin/plugin.json` — required. `name`, `description`, `version`, and
  `author` **as an object** — `{"name": "..."}` (optionally `email`/`url`), NOT a
  bare string. Claude Code rejects a string `author` at install time
  (`author: Invalid input: expected object, received string`), which fails the
  whole install.
  - **`plugin.json::version` is THE plugin version** (P-2). Every change bumps
    it — Casa derives the registry version from it, and it's what
    `verify_plugin_state` reports. If the plugin ships an MCP server with its
    own `server/package.json`, keep that `version` in **sync** with
    `plugin.json` (bump both in the same commit) so the manifest and the
    running server never disagree.
  - **Release ritual (v0.74.0 — REQUIRED, mechanically enforced):** every
    release ships as an **annotated tag** named exactly
    `"v" + plugin.json.version` (version `1.3.0` → tag `v1.3.0`):
    1. `VERSION=$(jq -r .version .claude-plugin/plugin.json)`
    2. `git tag -a "v${VERSION}" -m "release v${VERSION}"`
    3. Push branch **and** tag **atomically** — a half-push can leave a
       branch the configurator would mis-pin:
       `git push --atomic origin main "refs/tags/v${VERSION}"`
    4. **Existing conflicting tag → fail closed.** If the remote already has
       `v${VERSION}` at a different commit, **stop** — never `--force`/move a
       published release tag. Bump `plugin.json::version` and retry.
    5. **Remote peel-verify before completing** (executable — the tag is
       annotated, so compare the *peeled* commit, not the tag-object sha):
       ```bash
       [ "$(git ls-remote origin "refs/tags/v${VERSION}^{}" | cut -f1)" \
         = "$(git rev-parse HEAD)" ] && echo PEEL-OK || echo PEEL-FAILED
       ```
       (equivalently `gh api repos/<owner>/<repo>/commits/v${VERSION} --jq
       .sha` — the same call Casa's resolver makes, so your verify and the
       configurator's pin agree by construction). On PEEL-FAILED, stop and
       fix the push before emitting completion.
  - **Handoff:** hand the operator/configurator all **three** identity
    fields — `ref` (the `vX.Y.Z` tag), `revision` (the 40-hex commit sha,
    lowercase, that the tag peels to), `version` (`X.Y.Z`). Casa pins via
    `plugin_update(name, new_ref=<tag>, expected_revision=<sha>)`; pushing a
    tag alone changes nothing in Casa until the configurator points the
    registry at it (§3.13). **`emit_completion` mechanically validates every
    `casa_plugin_repo` artifact** (annotated tag exists, peels to
    `revision`, tag == `"v" + <remote plugin.json.version>`, `version`
    matches) and rejects the completion otherwise — the engagement stays
    live so you can fix the release and re-emit.
- `skills/<name>/SKILL.md` — skills pack. Single-line description triggers
  right. Keep it specific.
- `agents/<name>.md` — optional subagents.
- **Naming (harmonized 2026-07-19):** the `.claude-plugin/plugin.json`
  `name` is the plugin's canonical identity everywhere downstream — the
  registry entry, `verify_plugin_state`, plugin health, and the tool
  namespace (`mcp__plugin_<name>_<server>__*`). Keeper repos are named
  `casa-plugin-<name>` (repo `casa-plugin-gmail` ⇒ plugin `gmail`). Pick the
  short name first, derive the repo from it, and **state the plugin name
  explicitly in your completion handoff** — the configurator must never
  have to guess it from the repo (`plugin_add` hard-rejects a mismatched
  name).
- `.mcp.json` — MCP server declaration. Use `${CLAUDE_PLUGIN_ROOT}/server.py`
  and similar relative anchors. Every referenced path must be **committed**
  — never a dev-only venv or build dir; a Python server with library deps
  MUST follow the vendored-deps pattern in
  `casa-self-containment.md` §"Python MCP servers" (and smoke-test the
  vendored imports under `python3` before tagging a release).
- `hooks/hooks.json` — optional.
- `README.md` — required. Document required env vars + any
  `casa.systemRequirements` declarations in `.claude-plugin/plugin.json`.

Your plugin does NOT know about Casa. It's a plain CC plugin.

## Env var declaration

`.mcp.json::env` is your implicit env declaration:

```json
"env": {
  "MY_VAR": "${MY_VAR}"
}
```

When Configurator runs `plugin_add`, it reports every required `${VAR}`
reference (minus CC built-ins) and writes `plugin-env.conf`. What it does with
one depends on what the variable holds: a CREDENTIAL is searched for in the
default 1Password vault and wired as a reference, and the user is asked only
which item or field it is when the search cannot settle it — never for the
value. A PLAIN SETTING (a vault name, a host, a region) is set from your
plugin's documentation, or the user is asked for it by what it means; it is
never mapped to a vault item, so document each such variable and its default.
Values never appear in transcripts.

A variable your setup tool creates belongs in `casa.setupProvides`: it does
not withhold the plugin, and nothing fills it in by itself — your setup tool
reports the reference or value to wire and the configurator wires it, so make
that report name each value exactly.

## Completion schema

When you finish, emit:

```json
{
  "status": "ok",
  "text": "<human-readable summary>",
  "artifacts": [{
    "kind": "casa_plugin_repo",
    "repo_url": "https://github.com/<user>/casa-plugin-<slug>.git",
    "plugin_name": "<slug>",
    "ref": "vX.Y.Z",
    "revision": "<40-hex commit sha the tag peels to, lowercase>",
    "version": "X.Y.Z",
    "visibility": "public|private"
  }],
  "next_steps": [{
    "action": "add_to_registry_and_assign_with_confirmation",
    "plugin_name": "<slug>",
    "repo_url": "...",
    "ref": "vX.Y.Z",
    "revision": "<same 40-hex sha>",
    "description": "<short>",
    "category": "productivity|data|security|...",
    "targets": ["<role>", ...]
  }]
}
```

`ref` is always the release tag (`vX.Y.Z` — never a bare sha, never a
branch); `revision` is the exact commit it peels to; `version` matches
`plugin.json`. All three are validated against the live remote when you call
`emit_completion` — a lightweight tag, a moved tag, or a version mismatch
rejects the completion.

## Operator approval for non-allow-listed tools

Casa enforces a permission gate on every tool call. Tools matching your
`tools.allowed` patterns run with no prompt. Tools that do NOT match
raise a `[Allow] [Deny]` inline keyboard in this engagement's
Telegram topic; your call blocks until the operator taps a button (or
10 min elapses, treated as deny).

When you see "Operator denied via Telegram" in a tool result, the
operator has rejected that specific call. Acknowledge in your next
turn, then either retry with a different tool, describe what you
would have done so the operator can decide whether to approve, or
put the decision to the operator via `mcp__casa-engagement-channel__ask`
(a decision is always an `ask`, never a `reply`) and END YOUR TURN.

## Protected tools (`casa.protectedTools`)

If your plugin has a tool consequential enough that it should never run
without the operator tapping Approve, declare it in
`.claude-plugin/plugin.json`:

```json
"casa": {
  "protectedTools": [
    "reset_invoice",
    {"name": "delete_draft", "summary": "Delete the invoice draft for {period}"}
  ]
}
```

Each entry is either a bare tool-name string (the approval prompt then
uses the generic headline: agent name + short tool name + the exact
arguments) or an object `{"name", "summary"}` that upgrades the headline
to a plain-language action sentence (the exact arguments and the full
tool id stay visible below in both cases):

- `summary` is a single-line template, at most 200 chars, no control or
  bidi-control characters.
- `{arg}` placeholders are filled in from that call's OWN arguments only —
  every placeholder must name a real, scalar (string/number/bool)
  argument of the tool it decorates. A template that references an
  argument that doesn't exist, or that uses anything beyond a bare
  `{identifier}` (no `{x!r}`, `{x:>10}`, `{x[0]}`, `{x.y}`), silently
  falls back to the generic headline (agent name + short tool name; the full tool id stays visible below) at render time rather than
  breaking the prompt — write templates so they interpolate cleanly,
  since a fallback loses the plain-language value you added.
- The exact arguments are always shown too, in full, below the headline —
  `summary` only adds a human-readable line, it never hides or replaces
  the binding detail.

## MCP server naming = the grant namespace

Your `.mcp.json` `mcpServers` key becomes part of every tool's callable name:
`mcp__plugin_<plugin-name>_<server-key>__<tool>`. Casa grants installed
plugins server-level (`mcp__plugin_<plugin-name>_<server-key>`), derived from
that key. Name the server after the plugin (one server per plugin unless you
truly need more), and never rename it in a version bump — a rename orphans
nothing functionally (grants re-derive) but changes the tool names agents and
skills reference.
