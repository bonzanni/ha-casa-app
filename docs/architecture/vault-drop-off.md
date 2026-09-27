---
last_reviewed: 2026-09-27
---

# Vault drop-off

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a sign-in link or one-time code reaches the plugin tool that consumes it without
travelling in a delegation brief: the `casa.dropOffs` manifest field, the vault item Casa
writes for it, and the assistant's `vault_drop_off` tool. The kernel sentence that names a
declared drop-off as a consumer is [`personality.md`](personality.md)'s (INV-PERS-015 keeps it
on every resident projection); the configurator's read-only vault tools and the rule that
Casa never repeats an operator-typed vault string are
[`plugin-secret-exploration.md`](plugin-secret-exploration.md)'s; a value a plugin tool hands
to another tool of the same plugin by reference is the result broker's
([`plugin-result-contract.md`](plugin-result-contract.md)).

## Mental model

**The agent that holds the link puts it where the plugin looks, and says only that it is
there.** The safety kernel forbids repeating a sign-in link in any reply, message, delegation
brief or completion summary. Before this route existed, a link the resident read from a
mailbox could reach a specialist's sign-in tool only inside a brief, so a fresh finance
install could not finish its bank sign-in. The route that replaces it is an indirection
Casa already uses for every plugin secret: pass a reference, never the value. The resident
stores the link in the plugin's drop-off, tells the specialist that it is waiting, and the
plugin reads it from the vault, redeems it and deletes the item.

**The plugin declares a name; Casa decides the address.** A declaration is only a list of
names — `"casa": {"dropOffs": ["signin_link"]}`. The item Casa writes is titled
`Casa drop-off <plugin> <name>` in Casa's default vault (`ONEPASSWORD_DEFAULT_VAULT`), where
`<plugin>` is the plugin's runtime name: its manifest name for a plugin a specialist owns
(`bank-feed`, not the registry's `finance.bank-feed`), because that is the only name the
plugin itself knows. The value is the item's `password` field. Since the title is built by
Casa, no declaration can aim the write at another item — a plugin's credential item least of
all. The item carries the tag `casa-drop-off`, and Casa deletes only items that carry it.

**The plugin-author side** reads `op://<default vault>/Casa drop-off <plugin> <name>/password`
when its step runs without the value as an argument, compares the item's `created_at` with
its own record of when it asked for the link to refuse a stale one, and deletes the item
after the attempt, on success or failure.

## Contracts & invariants

**INV-TOOL-010**: A present `casa.dropOffs` is accepted only as a list of at most eight distinct lowercase ASCII names (`^[a-z][a-z0-9_]{0,63}$`) and a malformed one refuses the install or update as `drop_offs_invalid`; `vault_drop_off` writes only when the named plugin resolves to exactly one installed plugin, by registry or runtime name, that declares the named drop-off, and the value is a non-empty string of at most 4096 characters with no control character; it then creates one item titled from the plugin's runtime name and the drop-off name, tagged `casa-drop-off`, from a template passed to `op` on stdin, after deleting only earlier items with that title that carry the tag — an item with that title and without the tag refuses the write as `drop_off_collision`, with nothing deleted or created; and no result, error or log line carries the value or anything `op` printed.

**The tool is the assistant's alone.** It is on the assistant resident's tool list and on no
other shipped agent's. The assistant's doctrine tells it when to use it: for a sign-in link or
code the operator pasted or asked it to read from a specific email, when the consuming plugin
declares a drop-off; with none declared, the person hands the link to the specialist directly
in an interactive engagement's topic. The kernel's per-send rule still governs the mailbox
read.

**Write sequence.** One process-wide lock covers it, so two drops for the same item cannot
both create: `op item list` in the default vault, `op item delete` by id for each earlier
Casa-tagged item with the title, then `op item create -` with the JSON template on stdin. The
value is never an argument of any `op` call.

## Failure behavior

**Unknown or ambiguous plugin.** `unknown_plugin` when no installed plugin has that registry
or runtime name, `ambiguous_plugin` when two do (two specialists owning plugins with the same
manifest name). Nothing is written.

**Undeclared drop-off.** `unknown_drop_off`, carrying the names the plugin does declare (an
empty list for none), so the caller can correct the name or learn that the plugin has no
drop-off.

**Invalid value.** `invalid_value` for an empty string, a non-string, more than 4096
characters, or a control character (a newline included).

**No vault or token.** `no_vault` or `no_token`; no `op` call is made.

**`op` fails.** `op_failed` with its exit code (`-1` when `op` could not be started),
`op_timeout`, or `op_unreadable` when the listing is not a JSON list — the same fixed
classifications as the configurator's vault tools. A failure after a delete leaves no item;
the next drop writes one.

**Collision.** An item with the drop-off's title that Casa did not create (no tag) refuses the
write as `drop_off_collision`; nothing is deleted, and the plugin could not have read the value
anyway, since two items with one title make its `op://` reference ambiguous.

## Extension points

**A new plugin that needs a value handed to it by another agent** declares a drop-off name and
reads the Casa-titled item; it never asks for the value in a brief. **A new caller** of the
tool gets it on its role's tool list and a doctrine sentence saying when to use it — the tool
itself never widens what it writes.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/vault_drop_off.py`
- `casa/rootfs/opt/casa/plugin_store.py::manifest_drop_offs`
- `casa/rootfs/opt/casa/tools.py::_tool_vault_drop_off`
- `casa/rootfs/opt/casa/tools.py::_drop_off_target`

**Tests**
- `tests/test_vault_drop_off.py`
- `tests/test_assistant_prompts.py`

**Related**
- [`architecture/plugin-secret-exploration.md`](../architecture/plugin-secret-exploration.md)
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/plugin-result-contract.md`](../architecture/plugin-result-contract.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
<!-- END SOURCEMAP -->
