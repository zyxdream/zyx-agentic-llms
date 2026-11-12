# Spreadsheet analysis live results — November 8, 2026

The real UI/LLM test met its core requirements after fixing a catalogue
compaction bug exposed by the first attempt. The corrected run selected the
relevant workbooks, recovered from Python errors, evaluated formulas, generated
a new XLSX, and downloaded it without changing the originals.

## Environment and evidence

Normal `dev-monica` app on branch `Monica-agent`, SQLite `instance/myapp.db`,
Redis, Flask, main/document/legacy Celery workers, one Beat, and local React
frontend. The app and workers were restarted after checking that no workflows
or preparations were active. A SQLite backup preceded restart. Docker used
`lexintel-python-analysis:1`. Real Cognito, preparation, provider responses,
generated code, container execution, and UI downloads were used; no decisions,
progress, tool results, or API responses were mocked.

Dedicated matter: **Excel analysis live test — 2026-11-08**; task **250**, public
ID `9325b055-a4d2-4082-a69e-afd2495d3fea`. Corrected workflow:
`wf_150fcdde-ae2b-41ac-b5b0-cf9296138697`.

Ignored local evidence is in
`artifacts/live-case-assistant/spreadsheet-live-20261108/`. It includes the exact
prompt, reproducible synthetic fixtures, literal arithmetic oracle, worker
logs, read-only SQL snapshots, screenshots, independent workbook audits, and
the actual browser download. Sources were uploaded and the prompt submitted
through the UI. Browser helper permissions prevented automated file selection;
the native file picker was used. The download was completed through Chrome's
native Save dialog after the authenticated application request returned 200.

## Expected versus observed

| Check | Observed result |
| --- | --- |
| Preparation before the first agent call | All three XLSX files became ready. Profiles preserve invoice header row 4, payment header row 3, hidden/archive structure, and seven uncached formulas. Navigation summaries explicitly describe samples. |
| First-call context and selection | All three file/version IDs reached the controller. Python received only `01-invoices.xlsx` and `02-payments.xlsx`; the marketing budget was never staged. The relevant last worksheets were inspected from originals. The long invoice card explicitly omitted two sheet-detail previews, so inspecting actual structure mattered. |
| Full-data calculations | Six invoices and eight payment rows were used. Original invoice formulas and Control total were evaluated with LibreOffice on working copies. Leading-zero invoice IDs stayed strings. |
| Correct reconciliation | Invoice total **$4,660**; posted payments **$1,790**, comprising matched **$1,700** and unmatched **$90**. Pending **$100** and reversed **$1,000** were not deducted. Outstanding **$2,960**; overdue **$2,000**. Vendor balances: Alpha **$800**, Beta **$1,000**, Gamma **$1,160**. The 2025 archive and unrelated marketing budget were excluded. |
| Real error recovery | One explicitly requested, uncaught `RuntimeError("spreadsheet-recovery-check")` was returned as an observation. A subsequent inspection encountered a genuine datetime JSON-serialization error. The LLM corrected its code and continued without a Retry action. |
| New-file export and download | New `reconciliation-2026-11-08.xlsx`, eight sheets, **14,368 bytes**. Downloaded bytes exactly match the stored derived artifact. Source formula expressions in provenance columns are literal strings, not executable formulas. |
| Original integrity | All three local fixtures and stored upload copies retain their original SHA256 hashes. Seven original formula caches remain absent; recalculation did not alter them. |
| Publication and reload | One final response, empty editor, same 12 steps and 6 artifacts after reload, no repeated execution, zero active workflows/preparations. |

Derived-file artifact: `d7a6a771-98f3-434f-8df2-42da3eaeeacc`.
Final execution artifact: `c14582b5-db56-4c86-b77b-d6186b6f1115`.
Downloaded SHA256:
`0cfd711c8dce677dfa9977f6ce20200cce6ace79d66674cc57c6e6ad3f5ac1d1`.

Independent checks compare actual downloaded workbook cells/types to literal
fixture arithmetic, rather than accepting the agent's own validation flags:
**152 assertions passed** in `generated-workbook-audit.json`. A separate
read-only audit passed **94 download/result checks** and **10 reload checks**.

## Bug found and fixed

Initial workflow `wf_f177bbbb-55fe-4fbb-95d2-555d488a32c9` stalled before any
model decision or artifact. Catalogue shrinking changed a 121-character preview
to 120 characters plus an ellipsis, preserving its length and repeatedly
claiming progress. The real invoice profile remained over budget, exposing an
infinite loop that short mocked summaries had not triggered.

The preview target now includes the ellipsis. Six regression cases verify
strict progress at the boundary and finite compaction while retaining document
identity. The captured invoice card now shrinks from 3,038 to 1,114 tokens in
38 finite changes. The stalled request was cancelled through the UI, its worker
restarted, and the same task was submitted again. The cancelled attempt had no
LLM calls or Python executions. The focused regression/controller run passed
**68 tests**, with one opt-in Docker test skipped.

## Timing and limits

Preparation took **9.532 seconds**, with three `gpt-6-luna` calls and 6,702
instrumented tokens. The corrected assignment took **76.428 seconds**, including
six `gpt-6.1-sol` controller calls and five Python executions. Controller usage
was **73,495 aggregate tokens**; all six provider responses were HTTP 200 and
all attempts were first attempts. A separate post-completion conversation brief
used one call and 2,692 tokens. These totals are not a single context size or a
complete billed-cost estimate.

This test establishes the exercised synthetic workflow, not comprehensive
Excel compatibility, arbitrary workbook layouts, large-file performance, or
production deployment behavior. The exported method sheet includes the test's
labelled recovery details; the financial results remain separate.
