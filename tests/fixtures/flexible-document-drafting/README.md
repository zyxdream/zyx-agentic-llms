# Flexible drafting test inputs

Fictional inputs copied byte-for-byte from the previously validated local
spreadsheet closeout case. Follow
[the UI test scenario](../../scenarios/flexible-document-drafting-live-case.md).

Upload `invoices.xlsx` and `accounting/ledger.csv` for the core test.
`marketing/ledger.csv` is the optional unrelated duplicate-name input.
`expected-results.json` and `original-sha256.json` are tester-side checks;
do not upload them as source evidence.

The reporting cutoff is November 8, 2026. The core combined scenario was run
locally on November 9, 2026; see
[the observed results](../../scenarios/spreadsheet-to-markdown-live-results-2026-11-09.md).
