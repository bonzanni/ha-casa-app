# Live eval scripts

Measurement and live-model checks run against a live Casa (see each script's
header for the required options/env); none is part of the pytest unit gate.

- `ellen_caption_file.py` — live-model check (#1378): a file sent with a caption runs
  one turn whose message is the caption; for an invoice PDF captioned "add it to the
  invoices" the resident shares the file and delegates to Finance without asking.
  Runs in the container or with `--local --cli` (see script header).
- `ellen_ask_buttons.py` — live-model check (#1390): asked to choose with buttons
  between long natural choices, the resident's buttons show words (a plain short
  option, or `n · <short>`), not the "Option n" floor. `--history FILE` resumes a fork
  of one long session built under the pre-#1390 description — the live condition; a
  fresh session did not reproduce the failure. Runs in the container or with
  `--local --cli` (see script header).
- `approval_card_eval.py` — measurement of the model-written approval cards: every
  case in `approval_card_cases/` (real protected tools with their own descriptions and
  input schemas, each consent kind, three cases whose arguments try to steer the
  writer) is built by Casa's own card builder and written by the real `write_card`
  against a logged-in CLI, three runs per case for Sonnet at its effort, Sonnet at low
  effort and Haiku. A separate Opus call judges each card against the case's material
  facts; the summary reports missed and wrong facts, warning accuracy, softened or
  overstated scope, code-like tokens, fallbacks, latency and length. Runs from a
  checkout with `--cli` and `--out` (see script header); `--check` builds the requests
  without a model.
