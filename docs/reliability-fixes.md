# Research and spreadsheet reliability

This pass retains the existing agent loop, Flask/Celery/Redis/SQLAlchemy design,
artifact ownership, cancellation, version history, and exact-file downloads.
It adds no agent tool, database migration, frontend change, provider, or reviewer.

## 1. Blocked research pages

Previously an HTTP-200 access interstitial could become a searchable passage.
The source reader now recognizes conservative combinations of a gate-like HTML
title and instructions addressed to the visitor. Ordinary articles discussing
access restrictions remain readable. It checks both new retrievals and old cached
HTML without modifying historical artifacts.

Blocked reads return the existing unavailable/error shape plus `source_blocked`,
requested/final URLs, available title/status, and retrieval time. They create no
new passage or phrase-match position. HTTP errors retain their status codes.
The controller is guided to change source or disclose incomplete coverage.
Search summaries/citations and the separate phrase/chunk positions are retained.
Search ranking and completeness of current legal research remain model/provider
evaluation concerns; recognizing a block does not establish legal currency.

## 2. Avoidable result and reference errors

The isolated runtime normalizes nested date/datetime result values to ISO strings,
preserving offsets. Unsupported objects and non-finite numbers still fail
explicitly; there is no blanket string conversion. Module imports remain local
to each fresh Python call. `recalculate_workbook` returns a Path, which must be
converted explicitly if included in the JSON result.

Displayed research source, citation, and discovered-link references carry the
matching research snapshot ID. Existing IDs and top-level fields remain available;
saved query/source records are not rewritten. Authorization and mismatched-pair
rejection remain intact. Read guidance explains both valid target forms. Writer
guidance also prohibits invented generated-file download links.

These changes reduce specific avoidable failures, not all model-generated errors.
Syntax errors, missing imports, and incorrect document IDs still require visible
correction through the existing loop. No hidden repair or identifier remapping
was added.

## 3. Workbook input/output checks

The runtime's `check_workbook` helper applies nominated input changes to disposable
copies, recalculates with the existing engine, compares independently expected
outputs, and reports formula errors. It rejects formula replacement as an input
change. The source and delivered workbook remain unchanged.

The controller chooses the business expectations and relevant primary controls.
Dates should drive elapsed periods unless the user explicitly requests a fixed
period. Metadata-only dates and fixed assumptions require clear labels and
refresh instructions. A passing rate check does not establish date sensitivity
or full business correctness. Failed comparisons are structured findings for
correction/disclosure, not an automatic formula rewrite or publication policy.

## 4. Selected text fitting

`recalculate_workbook(..., fit_text_ranges={sheet: [range]})` optionally wraps
selected plain-text headings, summaries, and notes and increases insufficient row
heights using Calc measurements. A temporary unmerged measurement cell supports
merged text. Column widths, data, formulas, and larger existing heights remain
unchanged; the temporary sheet is removed before export.

The controller is instructed to finish wrapped text ranges before delivery and
inspect the final recalculated file. Choosing this helper and choosing meaningful
checks remain agent decisions. A formatting-only script that bypasses
recalculation can lose cached values even while preserving formulas. Native Excel
and rich/rotated-text rendering still need visual evaluation. No automatic global
restyling, column deletion, or mandatory extra model call was added.

See the [runtime contract](../backend/python_runtime/README.md) for examples.

## Final-file verification gap

Previously the agent could check a recalculated scratch copy but publish an
earlier XLSX without stored formula results. The server now checks the exact
selected file before completion and rejects unreadable workbooks, missing formula
results and Excel error cells through the existing recoverable tool-error loop.
It rechecks file identity/hash under publication locks and records the successful
report with the completion. Checking a different copy cannot satisfy this gate.
See [file delivery](file-delivery.md#selected-excel-file-readiness) for limits and
failure behavior. Business-rule correctness, cache freshness and visual quality
remain separate evaluation concerns; this adds no model reviewer or forced research.

## Validation

The affected backend tests cover retrieval and historical-cache rejection,
self-contained references, strict serialization, controller/drafting/version
continuity, and actual Docker/Calc checks for input relationships, error recovery,
text fitting, and original-file preservation. There are 295 passing checks in
the affected set. The generic date test detects a disconnected input; the fitting
test covers merged and wrapped text and effective grouped column widths.

The exact previously misclassified access page was also replayed offline and is
now reported as blocked. Natural provider/browser evaluations and downloaded-file
reviews are retained under the ignored `artifacts/live-reliability-fixes-*`
directories. Those evaluations distinguish deterministic behavior from remaining
model decisions and do not establish a latency distribution or universal quality.
