# Flexible document drafting: local live results

Branch: `Monica-agent`, based on `3b7238ec`. Tested locally on November 8–9,
2026, with the normal development app, real LLM responses, SQL artifacts and
the rebuilt `lexintel-python-analysis:1` image. No test server or deployment.

## Implemented path

`create_draft` and general `revise_draft` now use a dedicated Markdown writer.
Each candidate is an immutable draft artifact with a revision ID, parent ID,
complete Markdown source and saved Court export settings. The existing
controller/publication checks select the published revision. Legacy drafts and
uploaded `preserve_original` revisions retain their existing path.

New documents have read-only Preview/Source views. Downloads convert the pinned
revision deterministically to Markdown, HTML, DOCX or PDF; General/Court applies
to DOCX/PDF. Word tables contain actual rows/cells. A trusted Writer helper
creates PDF inside the restricted Docker runtime. Exports are private saved
artifacts, separate from the unchanged source revision. No schema migration.

## Real UI scenario

Task 253, “Flexible documents — tables and revision exports,” in the synthetic
Excel live-test matter:

`http://localhost:3000/case-assistant/d00e9c50-0793-4d2b-a39e-4d5430fc8059`

1. Asked the agent to publish “Pilot Account Review” using synthetic prompt
   data only: three account rows, leading-zero IDs, a second milestones table,
   numbered list, block quote and literal JavaScript example. The preview
   displayed two real tables and preserved the exact identifiers and values.
2. Downloaded the original Markdown, then requested exactly two changes: one
   Summary paragraph and account `000103` from `$75.25` to `$50.25`. The saved
   Markdown diff contained exactly those changes; all other source bytes and
   saved layout settings were retained.
3. Reloaded the UI. The revised document, source view and saved workflow history
   were restored. No direct editing controls appeared for the Markdown draft.
4. Downloaded revised Markdown, HTML, General DOCX/PDF and Court DOCX/PDF through
   the authenticated UI. Word inspection found two native tables, of dimensions
   4×4 and 3×2. Both PDFs retained the revised values and leading-zero IDs.
5. Opened the original workflow’s saved export record and downloaded its earlier
   revision after the newer revision was published. Its bytes matched the
   original download exactly. The current draft and both source artifacts stayed
   unchanged. Export records’ file hashes matched their private stored files.
6. Rendered and visually checked the downloaded Word document and both PDFs.
   Tables wrapped legibly without clipping; the quote and code remained text.

Initial revision: `bceafd82-5b8a-401d-991c-97488578585d`.
Revised revision: `cb58f96b-73c5-4635-8cef-8f616932bd61`.
Workflows: `wf_4cbb8332-0eb7-4386-8446-da3b9c717902` and
`wf_ae3f66e0-c5da-4ac8-a1b5-8db34b44509d`.

Seven saved export records cover the two revisions and format/layout choices;
the repeated original download reused its saved export. Download analytics
recorded **zero LLM calls and zero LLM tokens**. One PDF request was interrupted
by Flask’s development auto-reload during converter edits; retry succeeded
without regenerating the document.

## Converter and failure checks

A separate 48-row synthetic table produced six General PDF pages, retaining
all IDs and repeating the header on every page. Visual review caught ordinary
rows splitting across pages; native Word `cantSplit` row formatting fixed this.
The final pages begin with whole rows. A separate two-page Court fixture
preserved caption, parties, case number, title, numbered body and native table.

SQL-backed tests cover wrong owner/chat, mismatched current snapshots, pinned
older revisions, cache reuse, exact source bytes, unavailable Court settings,
PDF failure followed by retry, preserved source after failure, and default Word
compatibility. PDF runtime errors return a useful 503; invalid/unavailable
layout choices return 400. Real Docker spreadsheet regressions passed against
the rebuilt image, including isolation, timeouts, formula working copies and
generated-file reuse/downloads.

## Validation

Backend commands ran with Conda `lexintel` active.

| Check | Result |
|---|---|
| Focused drafting/controller/workflow/spreadsheet/context/analytics suite | 277 passed, 9 optional skips |
| Converter plus legacy Markdown/caption suite | 52 passed |
| Final download and analytics checks, including added Court error case | 31 passed |
| Real Docker Python/spreadsheet regression (`LEXINTEL_PYTHON_LIVE=1`) | 20 passed |
| Frontend preview, saved results, renderer and legacy block review | 47 passed across 4 suites |

These runs overlap; their counts are not additive. Optional container tests
were subsequently exercised in the real Docker run. Remaining optional Celery
integration checks were not enabled for this round.

Ignored local evidence is under
`artifacts/live-case-assistant/document-drafting-20261108/`: initial/revision SQL
audits, `published-source.json`, `download-audit.json`, downloaded files,
`word-qa/`, PDF page PNGs, `converter-qa/`, and `final-preview.png`.

V1 remains focused on documents and tables. Direct user editing, rich workbook
formatting and a general executable multi-file workspace remain deferred.
