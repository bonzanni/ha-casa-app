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
- `phone_eval.py` — measurement of the phone agent against the real live voice API
  (paid; never in tests or CI). Every case in `phone_cases/` (a call in progress:
  caller, transfer level, the conversation so far, the expected action and outcome,
  facts that must not be said) is built with Casa's own phone prompts and abilities
  (runners replaced by recorders), the caller's last line is spoken into a live
  session, and Casa's decision step reads the reply. Actions are checked against what
  the helper and the decision step asked for; outcomes and disclosures are judged by a
  model. The report gives pass rates overall and per group, unstable cases, and the
  median and p90 of the decision call; it exits 1 when a `disclosure` or `transfer`
  run failed. The shipped cases are generic; real ones live outside the repository
  and are passed with `--cases`. Needs `OPENAI_API_KEY` and `--yes-this-costs-money`
  (see script header); `--help` needs neither.
