---
name: fixture-check
description: The PLAY fixture's background job — one visible line, then complete.
---

# Fixture check

You are running the proposal fixture's background job. It is one short piece of work.

- Call the `offer` tool once, so a proposal lands in the operator's chat.
- Then call `emit_completion` with `status: "ok"` and `text: "fixture check posted one proposal"`.
- If an operator message reaches you, answer it in one line and carry on.
