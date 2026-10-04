# proposal-fixture

A test plugin for live checks of specialist buttons and file hand-off. It is not for
real use.

- `offer` posts a decision as buttons: Yes, No, More and `📎 Add a document`. `apply`
  records the tap it received in `applied.jsonl`; `more` pages twice and then reports no
  more entries.
- `offer_hang` and `hang` exercise a call that never returns.
- `ingest_document(path)` copies a file handed over by `share_inbound_file` into the
  plugin's data folder and records its name and size in `ingested.jsonl`.
- The `fixture-check` job runs one batch that calls `offer` once and then
  `emit_completion`, so a scheduled trigger with `job: proposal-fixture:fixture-check`
  can be seen to fire and the job ends ok.

Install it pinned to a commit SHA of this repository, not a tag. Casa requires a pinned
ref to match the manifest version, and release tags do not.
