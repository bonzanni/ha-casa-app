# Recipe: wire plugin secrets

Most plugins that ship MCP servers declare environment variables in
their `.mcp.json` (API keys, host overrides, vault references). Casa
resolves those at MCP-server-start time from
`/config/plugin-env.conf` via the 1P universal
resolver — `op://...` references are resolved to plaintext, plain
values pass through unchanged.

This recipe covers wiring those vars. The flow is the same whether you
arrived from `recipes/plugin/add.md` Stage 4 or the operator just
asked to rotate a secret on an already-installed plugin.

## When to use

- `plugin_add` (or `plugin_update`) returned a non-empty `required_env_vars` list.
- `verify_plugin_state` reports `ready: false` with one or more
  `secrets[*].status: unresolved`.
- The operator asks to update an existing secret (1P field changed,
  vendor rotated the key, etc.).

## First: what is searched for

Search 1Password for every unresolved required variable not declared in
`casa.setupProvides`, except a variable that configures how 1Password itself
is used (which vault). `ONEPASSWORD_DEFAULT_VAULT` and
`OP_SERVICE_ACCOUNT_TOKEN` come from app options, never from a vault item;
the exception also covers a plugin's own vault-name variable. Any other value
— a key, a token, but also a region, a host, an account or an email — can
legitimately be stored in a 1Password item, alongside a secret or not, and is
wired from an item that actually holds a field for it. A documented default
does not bypass searching for a variable outside the 1Password-configuration
exception.

A variable whose name or documentation mentions a vault or 1Password counts
as configuring 1Password unless the plugin's documentation says otherwise.
When it is unclear whether a variable configures 1Password, never map it and
ignore its candidate rows. The exploration below still searches for such a
variable and lists what it found; set it only from the plugin's documentation
or from the operator, as the last case below says for a non-secret variable.

## Discover the source — explore before asking

`plugin_add` and `plugin_update` already searched the default vault for you:
when a plugin declares required variables that are unresolved (other than
those in its `casa.setupProvides`, which its setup tool reports — below), their result
carries `secret_candidates` — the vault searched, the queries tried (the
plugin name, then each vendor stem of the variables), up to five matching
items (each as `matched_query` — the search term its title contained, never
its title and not its name — with its id) and, per field,
the field's id, its `role` (`client_id`, `client_secret`, `api_key`, `token`,
`refresh_token`, `access_token`, `credential`, `username`, `password`, `email`,
`hostname`, `url`, `account`, `region`, `other_known`, or null) and its type —
never a label, a section or a value — and the variables still `unresolved`.
Read it before anything else, and decide from it, one variable at a time. A
candidate field for a variable is a field whose role matches it, or, when no
found field's role matches it, a field with no role (null or `other_known`)
on an item that could hold it. An item holding no candidate field for a
variable is never put to the operator, even when its title matches. A
`matched_query` says only that a search word is in the title, not that the
item holds anything this plugin needs.

- When exactly one item matches and every unresolved variable maps to exactly
  one field role, wire it (Set the entry below): the mapping is variable →
  role (`GMAIL_CLIENT_ID` → `client_id`, `GMAIL_USER_EMAIL` → `email`,
  `ELEVENLABS_API_KEY` → `api_key`, `DEMO_REGION` → `region`), the reference
  is `op://<vault>/<item id>/<field id>`, one `set_plugin_env_reference` per
  variable; then reload and verify, and name the item id and field roles you
  used in your completion. Evaluate each variable separately: when an item
  holds a clear field-role match for one variable, wire it even when another
  variable has no candidate field.
- When a variable's candidate fields do not settle it — several items each
  hold a field with its role, one item holds two fields with its role, or its
  only candidates are fields with no role — ask in the engagement topic,
  naming what you found. Describe each item by its category and the roles of
  its fields — what the operator can recognise — and never by its `matched_query`: two
  items with the same `matched_query` are two different items whose titles
  both contain that word, never two items with the same name ("two items
  contain 'gmail', an API credential with a client id, a client secret and
  an email, and a login with a username, a password and an email — which one
  holds GMAIL_USER_EMAIL?"; "the item has two fields I cannot place, ids f7
  and f9 — which is ELEVENLABS_API_KEY?"). Never ask the operator for a
  secret value; ask which item or field it is.
- When no found item holds a candidate field for a variable (empty `items`,
  or only items without one), call `list_vault_items(query=...)` once more
  with a different keyword if one is plausible (a product name from the
  plugin's README, say). If nothing found holds a variable, use the plugin's
  documentation or ask the operator for its value only for a non-secret
  variable; report a secret unwired. A non-secret variable — a region, a
  host, an environment, a plugin's vault name — is set from the plugin's
  documentation with a literal `op_ref_or_value`, and when the documentation
  gives no value, you ask for it in the engagement topic by what it means. A
  secret — a key, a token, a password, a client secret — stays unwired: report
  that vault, the queries tried, that nothing held it, and which variables
  stay unwired. That report — not a request for values — is your
  completion's job. If nothing found holds a variable and you cannot tell
  whether it is a secret, treat it as a secret, never ask the operator for
  its value, and report it unwired.
- When the vault could not be read (`secret_candidates.error` is `op_failed`,
  `op_timeout` or `op_unreadable`), that is NOT "nothing matched": try
  `list_vault_items` once by hand; if it fails the same way, report that vault
  as unreadable, with the classification, and which variables stay unwired.

The default vault is named in your world state (`Default vault:`); omit
`vault` to use it, or pass it explicitly to name it in your report. If the
operator already gave a 1Password reference (`op://<vault>/<item id>/<field id>`;
an operator may type names instead of ids, and the resolver accepts them,
but you pass it through unchanged and never repeat it), skip to Set the
entry below. An operator-supplied 1Password reference may skip discovery only
for a variable that does not configure 1Password. For a manual search:

    list_vault_items(query="<vendor-or-plugin-keyword>")
    # → { items: [ { matched_query, id, category }, ... ] }   # the search term, never the title

Filter by a keyword — the schema requires one; don't enumerate the whole
vault.

Once an item is chosen, list its fields:

    get_item_fields(item="<item id>")
    # → { fields: [ { id, role, type }, ... ] }   # never labels or values

The resolver shells `op read op://<vault>/<item id>/<field id>` at boot; the
field id is the third path segment. Plain-typed values (`type: STRING`,
`type: CONCEALED`) work directly — file/document fields don't.

## Set the entry

    set_plugin_env_reference(
      plugin="<plugin_name>",
      var_name="<VAR>",
      op_ref_or_value="op://<vault>/<item id>/<field id>",
    )

Or, if the operator wants a literal value:

    set_plugin_env_reference(
      plugin="<plugin_name>",
      var_name="<VAR>",
      op_ref_or_value="<plain-value>",
    )

The tool upserts the line in `plugin-env.conf` — call it once per
required var.

## Reload — MANDATORY, and BEFORE you verify

`plugin-env.conf` is re-sourced into `os.environ` by
`casa_reload(scope='plugin_env')` — sub-second, in-process.
A live agent's MCP-server subprocesses inherit env at next spawn.
The reload also **regenerates `plugin-health.json`** from the new
effective environment (action `plugin_health_regenerated`), so a
now-resolved plugin clears its stale health issue without a registry
mutation.

**Order matters:** `verify_plugin_state` grades secrets against the
*effective* `os.environ`, not the conf file. Verifying before the
reload gives a wrong answer in BOTH directions: a newly-added var reads
`unresolved` (the false-red the gmail-v0.2.0 incident recorded), and a
*rotated* var still shows the OLD value as `resolved` — a stale-green
(verify flags a plain-value conf/env mismatch as
`unresolved: reload pending`, but an `op://` rotation cannot be
detected at all before the reload). Set → Reload → Verify, always.

## Verify — AFTER the reload

    verify_plugin_state(plugin_name="<plugin_name>")

Look at `secrets[*].status`. Every required var should report
`resolved` (with `source: op` for 1P references, `source: plain` for
literals). `status: unresolved` with `reason: "not in plugin-env.conf"`
means a `set_plugin_env_reference` call is still missing. If the
top-level `ready` is still `false`, quote its `reasons` verbatim in
your completion (or escalate) — never finish leaving a red
`plugin-health.json` unexplained.

## Canonical order

    config_git_commit(message="<plugin>: wire <VAR> via 1Password")
    casa_reload(scope="plugin_env")
    verify_plugin_state(plugin_name="<plugin>")   # expect secrets resolved
    emit_completion(status="ok", text="Wired <VAR> for <plugin>; ready=<bool> (reasons=<...> if false); committed SHA <sha>; called casa_reload(scope='plugin_env') to refresh MCP-server env + plugin health.")

**`plugin-env.conf` is gitignored** (it's a mode-0600 secrets file). So
`config_git_commit` after a secret-only change stages nothing and returns
`sha=""` **plus a `warning` explaining that only whitelisted paths are
tracked — that is expected, NOT a failure. Do not retry the commit.** Still
call it (it's a harmless no-op that keeps the flow uniform), but in
`emit_completion` say "no SHA (secrets file is gitignored)" rather than
reporting a blank `<sha>`.

If you arrived here from the install flow, batch — call
`casa_reload(scope='plugin_env')` first, then `casa_reload(scope='agent', role=<role>)`
per target role at the end of the install.

## Common mistakes

- Setting the var without surfacing it through `get_item_fields` first.
  A mistyped field id resolves to an empty string and the MCP server fails
  to start with no clear error in the agent log — copy ids from the result.
- Using `op://` syntax for a literal value, or omitting `op://` for a
  vault reference. The resolver only follows the prefix — anything
  else passes through verbatim.
- Calling `list_vault_items` without a `query`. The vault dump can be
  several hundred items long; constrain the search.
- Forgetting `casa_reload(scope='plugin_env')` between
  `config_git_commit` and `emit_completion`. The file on disk is
  correct but `os.environ` (and thus next MCP-server spawn) keeps the
  prior values.
- Verifying BEFORE the reload — guaranteed-stale `unresolved` result
  (see "Order matters" above).

## A value the plugin's setup tool reports (`casa.setupProvides`)

A variable the plugin declares in `casa.setupProvides` is created or found
by the plugin's own setup tool — a key it forges, an application id it
learns — and `verify_plugin_state` grades it `unprovisioned`, not
`unresolved`. Never ask the operator for it. Nothing wires it on its own:
the setup tool REPORTS the reference or value to wire, and wiring it is your
job. When that report reaches you — in your brief, or in a setup result you
are shown — run Set the entry, Reload and Verify above with exactly what it
names. When no setup report has named the value yet, say it is waiting for
the plugin's setup run; never report it as something no configurator can
do.

## Optional keys NOT declared by the plugin (e.g. `context7`)

Some plugins ship an MCP server that **works without a key** and reads an
**optional** API key from the environment — the key is NOT declared in the
plugin's `.mcp.json`, so `plugin_add` returns no `required_env_vars`
and `verify_plugin_state` shows nothing unresolved. The "When to use" triggers
above won't fire, but the operator may still want the key wired (for higher rate
limits / reliability). **`context7`** is the canonical case: its MCP server
(`npx @upstash/context7-mcp`) reads **`CONTEXT7_API_KEY`** from env if present
and otherwise runs keyless (rate-limited).

Wire it exactly like any other secret — the var is **global** (the `plugin` arg
to `set_plugin_env_reference` is a label only; the line is written flat into
`plugin-env.conf` and re-sourced into `os.environ`, which the plugin's MCP
subprocess inherits):

    # the operator gives the reference, or you find the item and its field
    # id as above: op://<vault>/<item id>/<field id>
    set_plugin_env_reference(
      plugin="context7",
      var_name="CONTEXT7_API_KEY",
      op_ref_or_value="op://<vault>/<item id>/<field id>",
    )
    config_git_commit(message="context7: wire optional CONTEXT7_API_KEY via 1Password")
    casa_reload(scope="plugin_env")
    emit_completion(status="ok", text="Wired CONTEXT7_API_KEY (optional, raises context7 rate limits); no SHA (secrets file gitignored); reloaded plugin_env (set_1_vars).")

**Verify differently:** because context7 declares no required env var,
`verify_plugin_state` won't report it. Confirm instead that `plugin-env.conf`
contains the `CONTEXT7_API_KEY=...` line (it does after `set_plugin_env_reference`).
The key takes effect for the next MCP-server spawn (e.g. the next plugin-developer
engagement). A bad/empty key does NOT break context7 — it falls back to keyless.

## Remove an entry

Use `remove_plugin_env_reference` when a var is no longer needed — a plugin
update shipped in-code defaults, the operator moved a key to an add-on
option, or the plugin was removed and left orphaned entries. NEVER try to
Edit `plugin-env.conf` directly (`path_scope` blocks it — that is by design).

    remove_plugin_env_reference(plugin="<plugin_name>", var_name="<VAR>")
    # → {ok: true, removed: true|false}   (removed:false = was not present — fine)
    casa_reload(scope="plugin_env")

The reload is MANDATORY after a removal: it is what drops the key from the
effective environment (deletion-diff) and regenerates plugin health. Without
it the old value keeps applying until the next restart. Removal is
idempotent — report `removed: false` factually, don't retry.
