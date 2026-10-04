---
name: fixture-check
description: The PLAY fixture's background job — one batch, one visible line, then complete.
---

# Fixture check

You are running the proposal fixture's background job. It has one batch.

- Launch turn: acknowledge in one line and do no work.
- Batch 1: call the `offer` tool once, so a proposal lands in the operator's chat.
  Then call `emit_completion` with `status: "ok"` and `text: "fixture check posted one proposal"`.
- Any other turn: answer in one line.
