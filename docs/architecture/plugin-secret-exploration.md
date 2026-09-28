---
last_reviewed: 2026-09-28
---

# Plugin secret exploration

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a plugin's required secrets are found: the vault exploration a plugin mutation's result
carries, the two vault tools the configurator holds, the one field projection they share,
and what a failing `op` call is allowed to say. Wiring a secret — the `plugin-env.conf`
entry, its reload and its verification — is [`plugin-runtime.md`](plugin-runtime.md); the
mutation envelope the exploration rides on is [`plugin-mutation-tools.md`](plugin-mutation-tools.md);
the option that names the vault is in [`reference/operator-options.md`](../reference/operator-options.md).

## Mental model

**The tool explores, the configurator decides, the operator is asked last.** Whether the
configurator searched the vault used to depend on the wording of its brief. The playbook's
install drives said "credentials are in 1Password" and passed; the operator's own message on
2026-09-15 said neither that nor a version, the one vault call the configurator made was
refused by a schema that demanded a vault name the deployment already held, and the install
ended with the operator asked for values. A result field that is always present turns "did
it think of searching?" into "did it read its own result?". The recipe then orders the
decision — explore, then wire, then ask — and asking is for a name, or for a non-secret
value, never for a secret value.

**Casa never repeats an operator-typed vault string.** Three review rounds tried to decide
which metadata strings were safe to echo by comparing them against where secrets live — field
values, then every string in the document, then also by shape — and each round found a string
that got through (a value duplicated into a label, a value split across labels, a plain-shaped
secret typed as the label itself) or a string wrongly held back (an ordinary label the item's
notes happened to mention). The mechanism is cut. What is returned about an item is its
op-generated id and, per field, the field's op-generated id, its type (validated against
`op`'s own enum) and a **role** from a closed set Casa owns — `client_id`, `client_secret`,
`api_key`, `token`, `refresh_token`, `access_token`, `credential`, `username`, `password`,
`email`, `hostname`, `url`, `account`, `region`, `other_known`, or none — derived in-process
from the label, which is never returned. In place of a title, the query term the item matched,
under the key `matched_query` — never `name`: a reader of `name` takes the search word for the
item's name, and two different items whose titles both contain it then read as two items with
the same name, which is how an operator was once asked to choose between two items "literally
named bank" that did not exist.
Labels, sections, values, references and notes never reach a result by any path; the `op`
CLI's own output never reaches one either: a failure is a fixed classification, because
truncating stderr is not redaction. A field with no role is the operator's to name when no
found field's role matches the variable: the configurator asks which field is which by its
id, and never for a secret value — which is the ruling this whole document implements.
Wiring uses the ids: `op://<vault>/<item id>/<field id>` is a reference the resolver accepts.

## Contracts & invariants

**INV-TOOL-009**: When `plugin_add` or `plugin_update` activates a plugin with required environment variables that are unresolved and not declared in the `casa.setupProvides` of the manifest that mutation just published, and a default vault and a 1Password token are configured, the result carries `secret_candidates` — the vault searched, the queries tried, the matching items as the query term each matched and its op id with, per field, the op id, a role from Casa's closed set and a validated type, and the variables still unresolved, none of them a name that manifest declares in `casa.setupProvides` — and never a title, a label, a section, a field value, a reference, a token or the `op` CLI's own output; a failed lookup is reported as a fixed classification — the exit code for a non-zero exit, `op_timeout` or `op_unreadable` for a call that did not return or returned something other than the expected JSON — and does not fail the mutation and logs no message of the failure's own; no operator-typed vault string is returned — an item is reported by the query term its title matched (the `matched_query` key, never a `name`) and its id, and a field by its id, its type and a role from Casa's closed set, never by its label or section; nothing is wired by it; and the two vault tools accept an omitted `vault`, fall back to the same default, and fail with the same classification.

The exploration runs after the registry write and the reload, so it can neither delay nor
fail activation. The queries are the plugin name and each vendor stem of the unresolved
variables (`GMAIL_CLIENT_ID` → `gmail`), deduplicated, at most three; at most five items are
returned, each with the fields `get_item_fields` would report for it (id, role, type). A variable already
present in the effective environment is not listed as unresolved. Neither is a name the plugin
declares in `casa.setupProvides` (#1024): its own setup tool makes that value and reports it
for wiring, so no vault item holds it — and since every declarable name is `CASA_PLUGIN_*`, its
only query would have been `casa`. The declaration is read from the manifest the mutation just
published, never from the resolved snapshot, which during an update can still be the old
artifact: a name the new version stops declaring but still references is explored again. A
declaration that cannot be read counts as none. The field is absent when
there is nothing to explore for — no unresolved variable, no default vault, no token — so
its absence is not a failure and its presence is not a promise that anything matched.

**The vault tools' schema is the recipe's fence.** `list_vault_items` requires a `query` and
`get_item_fields` an `item`; `vault` is optional on both and falls back to the configured
default, which every executor engagement's world-state block also names (`Default vault:`),
so the configurator can omit it or name it in its report. The schemas are explicit JSON
Schema objects: the SDK compiles the dict shorthand to required-all, which is how #535's
handler-side fallback shipped without ever being reachable through the validator — the
call the fallback was written for was refused before the handler ran.

**What the recipe does with the result** (`recipes/plugin/secrets.md`) is guidance the
configurator model follows; the exploration above is unchanged by it and still lists what it
found for every unresolved variable.
Its rule is: "Search 1Password for every unresolved required variable not declared in
`casa.setupProvides`, except a variable that configures how 1Password itself is used (which
vault)." Casa's own vault and token options are never items, and a plugin's own vault-name
variable — or one whose name or documentation mentions a vault or 1Password, unless the
plugin's documentation says otherwise — is never mapped to an item, so the candidate rows the
exploration still lists for it are ignored. Every other variable, a region or a host as much
as a key, is decided one variable at a time over its candidate fields. A candidate field for a
variable is a field whose role matches it, or, when no found field's role matches it, a field
with no role (null or `other_known`) on an item that could hold it. An item holding no
candidate field for a variable is never put to the operator, even when its title matches. A
variable whose role matches exactly one field is wired, one `set_plugin_env_reference` per
variable, then reloaded and verified, and named in the completion — even when another
variable has no candidate field; a variable whose candidate fields do not settle it — several
of them, or only fields with no role — is a question asked in the engagement topic naming what was found — each item described by its category and the roles of
its fields, which the operator can recognise, and never by its `matched_query`. A variable no
found item holds is one more search with a different keyword if one is plausible; then a
non-secret value is taken from the plugin's documentation or asked of the operator, and a
secret — or a variable that cannot be told apart from one — is reported unwired, naming the
vault and the queries, and its value is never asked for. A completion that leaves a required
variable unwired without saying what was searched is a doctrine violation, pinned in prose
tests like the liveness prohibition.

## Failure behavior

**`op` exits non-zero, at any step.** The result carries `{"error": "op_failed", "exit_code": N}`
in place of the candidates (or of the tool's items or fields), and nothing of what `op` wrote.
The mutation's `ok` is unaffected. The recipe treats it as an UNREADABLE vault, never as
"nothing matched": one manual `list_vault_items`, and if that fails the same way, a report
naming the vault as unreadable with the classification and the variables left unwired.

**`op` does not return, or returns something that is not the expected JSON.** `op_timeout`
and `op_unreadable`, fixed strings; the bytes that could not be parsed are never forwarded.
Anything else the exploration raises is logged by its exception class only — never its
message or traceback — and reported as `op_failed` with exit code `-1`; the mutation's
envelope is unchanged in every case.

**No default vault, or no token.** No `op` call is made and `secret_candidates` is absent;
`required_env_vars` still names what the plugin needs, and the recipe's manual search is the
path.

**A variable already resolved in the environment.** It is not listed as unresolved and does
not widen the queries.

**A variable the published manifest declares in `casa.setupProvides`.** The same: not listed,
no query; a plugin whose only unresolved variables are declared ones carries no
`secret_candidates` and no `op` call is made. `required_env_vars` still names it.

## Extension points

**A new vault-facing surface** returns the projection `_project_item_fields` produces (ids,
roles from `_field_role`, validated types) and the classification `_op_failed` returns, and
nothing else about an item or a failure; a second parser, an echoed title or label, or a
quoted stderr is the defect this document exists to prevent. **A label that should map to a
role and does not** gets a row in `_FIELD_ROLES` with a test, never a returned label.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::_secret_candidates`
- `casa/rootfs/opt/casa/tools.py::_secret_candidate_queries`
- `casa/rootfs/opt/casa/tools.py::_tool_list_vault_items`
- `casa/rootfs/opt/casa/tools.py::_tool_get_item_fields`
- `casa/rootfs/opt/casa/tools.py::_project_item_fields`
- `casa/rootfs/opt/casa/tools.py::_op_failed`

**Tests**
- `tests/test_plugin_add_secret_candidates.py`
- `tests/test_vault_tools_default_vault.py`
- `tests/test_vault_tool_schemas.py`
- `tests/test_plugin_secrets_doctrine.py`

**Related**
- [`architecture/plugin-mutation-tools.md`](../architecture/plugin-mutation-tools.md)
- [`architecture/plugin-runtime.md`](../architecture/plugin-runtime.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`reference/operator-options.md`](../reference/operator-options.md)
<!-- END SOURCEMAP -->
