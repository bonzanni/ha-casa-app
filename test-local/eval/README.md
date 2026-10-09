# Memory-accuracy eval scripts

Phase-0/0.1 measurement harness from the 2026-06 memory-accuracy project, kept as
regression guards. They run against a live Casa + Hindsight instance (see each
script's header for the required options/env). Outcome of the original
measurement: extraction 100%, and the feared cross-domain pollution did not
manifest at small bank scale — re-run these when the `casa` bank grows 10–100×.

- `phase0_memory_baseline.py` — extraction/recall baseline per access tier
- `phase0_1_pollution_rank.py` — cross-domain pollution ranking + recall-budget sweep
- `context7_keytest.py` — probe: context7 MCP key wiring through add-on options
- `ellen_brief_fidelity.py` — live-model gate (W3/Sol B11): pins the
  invoice_reset mistranslation — Ellen's `engage_executor` call must carry
  process instructions VERBATIM in `brief.process_requirements` with
  `interaction_required=True`, never paraphrased into a feature requirement.
  REQUIRED pre-merge command (see script header for the in-container run
  command); not part of the pytest unit gate (needs a live model).
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
