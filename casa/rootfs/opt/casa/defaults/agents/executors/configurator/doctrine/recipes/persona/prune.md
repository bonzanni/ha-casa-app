# Recipe: prune unreferenced personas

Persona versions are immutable, so every upgrade installs a new version beside the old one and the
superseded bytes stay on disk. `persona_prune` removes every installed version that nothing refers
to any more. It is never automatic — deleting content the operator approved is their call.

## Steps

1. `persona_list` and show the operator exactly which entries have an empty `referenced_by` — those
   are what a prune would remove. Do not describe the sweep in the abstract; name them.
2. Ask the operator to confirm they want those removed.
3. `persona_prune`.
4. Report `removed` and `kept` verbatim. A kept entry carries the reason (`persona_pinned` with its
   referrers, or a failure kind) — do not summarize it as "some were skipped".
5. Each removal also revokes that version's install approval, so re-installing any of them will ask
   for the operator's approval again. Say so.

## Common mistakes

- Running the prune before showing what it will delete. `persona_list` first, always.
- Reporting `ok: true` as "everything was cleaned up" when `kept` is non-empty.
