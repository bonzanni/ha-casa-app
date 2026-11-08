# Recipe: delete a resident — RETIRED

Residents are fixed: assistant, butler, concierge. Deleting a resident is not
supported, and hooks block it unconditionally. Do not attempt to work around
the hook, and never edit hook policy files — that is itself denied.

- "Remove/replace <name>" usually means the PERSONA, not the role: follow
  `recipes/persona/apply.md` to rebind the resident's identity.
- To stop a resident delegating somewhere: `recipes/delegate/unwire.md`.
