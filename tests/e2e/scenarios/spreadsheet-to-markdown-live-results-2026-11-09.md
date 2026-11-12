# Spreadsheet to Markdown: local live results — November 9, 2026

The core [combined UI scenario](flexible-document-drafting-live-case.md) was
completed locally using real uploads, LLM decisions, Python containers,
Markdown drafting, revisions and browser downloads. Content, revision and
export checks passed, with two reliability/traceability findings below. No
application code was changed during this run.

## Local setup and inputs

- Branch `Monica-agent`, including the existing uncommitted drafting changes.
- Restarted Flask, frontend, main worker, document worker, extraction worker
  and Beat after checking there were no active requests or preparations.
  Saved `instance/myapp-before-spreadsheet-drafting-20261109.db` first.
- Used the existing rebuilt `lexintel-python-analysis:1` image. Redis and
  unrelated Docker services were left running.
- Created empty matter 16, **Spreadsheet to Markdown — local test 2026-11-09**,
  and task 254, **Invoice reconciliation — Excel to Markdown**.
- UI: `http://localhost:3000/case-assistant/ad6076c0-77f8-447c-8b13-e36a3b1785ed`.
- Uploaded only the scenario's `invoices.xlsx` and `accounting/ledger.csv`.
  Both became ready on their first preparation. Native file selection was used
  because the browser extension's direct file-upload permission was disabled.
- The first controller input contained exactly those two files. Deep mode used
  `gpt-6.1-sol`. Provider decisions and tool executions were not mocked.

## Analysis, drafting and recovery

Python inspected all four sheets and eight headerless CSV records, preserving
row 1 and the leading-zero invoice IDs. It used working-copy formula evaluation
and checked invoice amounts against quantity × unit price. The prior-year $37
archive was excluded. The saved calculation contained the complete invoice,
vendor, payment-exception and source-location results.

| Independently checked result | Observed |
|---|---:|
| Current invoice records | 6 |
| Payment records | 8 |
| Invoice total | $4,660 |
| Matched posted payments | $1,700 |
| Unmatched posted payment | $90 |
| Outstanding | $2,960 |
| Overdue | $2,000 across 4 invoices |
| Vendor balances | Alpha $800; Beta $1,000; Gamma $1,160 |
| Excluded statuses | Pending $100; reversed $1,000 |

Three authoring operations failed with SDK `ValidationError` and returned the
generic observation “The drafting provider did not produce a document.” The
controller retained the analysis, saved a fallback Markdown report through
Python, and succeeded on its fourth authoring operation.

The tester issued Stop based on the repeated failures; the successful candidate
had just been saved, so Stop interrupted verification. UI Retry resumed that
saved candidate and completed verification/publication under the same workflow.
It did not repeat Python analysis or successful drafting. All 23 previously
ready steps and five saved artifacts retained their identities, arguments,
results and timestamps/content. Candidate staging before publication was not
visually captured and is not claimed as a separately observed checkpoint.

Initial workflow: `wf_3a21bea1-7450-4f75-8218-6459a12b9b39`.
Initial revision: `97c42650-5fe6-4c6e-83ae-5dd8a1bcddf9`.
Accepted request to completion was about 5m33s, including retries and the manual
Stop/Retry interval; this is not an uninterrupted model-latency measurement.

## Visible document, revision and downloads

Preview contained all three requested native HTML tables, headings, emphasis,
source references and numbered actions. Source showed the corresponding
Markdown, including 20 pipe-table lines. The report explained that payment
timestamps were absent, so its historical cutoff relied on the supplied status
snapshot. All numerical results matched the independent fixture oracle.

The exact revision prompt appended one sentence to the executive summary and
replaced P8's Treatment cell. The saved Markdown diff contained only those two
changes; all other wording, numerical values, cells, dates and saved layout
settings were preserved. The revision workflow verified its candidate before
publication and completed in about 52 seconds without authoring errors.

Revision workflow: `wf_8aea87fc-3c73-494e-a9cc-b4ab4fed1c3c`.
Revised revision: `3f35dab7-ccdb-4f20-83f4-078d297dbd63`.

Reload restored the revised draft and saved history. Actual browser downloads
of revised Markdown, HTML, Word and PDF matched their private stored export
hashes. UI source and Markdown download bytes matched the saved revision.
Downloading the earlier revision again through its saved export produced the
same bytes as its first download, without changing the current document.
Five export records were saved; the repeated original download reused its
record. Export LLM calls and tokens were both zero.

Both the downloaded DOCX and PDF rendered to three pages; every page was
visually inspected. The Word document contained three native tables, sized
8×6, 5×6 and 4×5 including headers/totals. All IDs, values and requested changes
survived export. There was no clipping, overlap, lost cell or orphaned header.
Repeating-header markup existed, but no individual table crossed a page, so
visual header repetition was not exercised. Equal column widths make the long
Treatment cells narrow and tall; the text remains readable.

## Findings to carry forward

1. **Intermittent authoring validation failures.** Recovery succeeded, but three
   failures added delay. Existing logs retain only the exception class, so the
   invalid field/content cannot be identified. The SDK raises before response
   accounting, so current token counters do not include those failed parsing
   responses; they must not be treated as complete billed usage. No speculative
   parser, schema or retry-rule change was made during this test.
2. **Incomplete artifact provenance after fallback.** The controller read the
   entire correct calculation artifact, but the final writer's supplied material
   was the fallback Markdown artifact. That artifact had empty `references` and
   `input_artifact_ids`, leaving no machine link back to the analytical result.
   The original calculations and exact source reads remain in tool history;
   report values and textual source references are correct. Direct analytical
   lineage into the final writer is explicitly incomplete, not a passed check.

## Evidence and scope

Independent audits passed: 61 initial content checks, 168 Retry-preservation
checks, 97 revision checks, 113 final export/integrity checks and six browser/
source checks. The provenance limitation is recorded separately and excluded
from claims of direct analytical lineage.

All fixture and stored-original hashes remained unchanged, both source revisions
were preserved, and no active workflows/preparations remained. The restarted
local stack was left running with the completed report open.

Ignored evidence is under
`artifacts/live-case-assistant/spreadsheet-drafting-20261109/`: preparation and
workflow snapshots, independent audit JSON, `provenance-finding-254.json`, exact
prompts/source captures, six browser downloads, Preview/Source screenshots, and
all Word/PDF page images in `export-qa/`.

The optional unrelated duplicate-filename extension and Court preset were not
rerun in this combined case; their earlier separate coverage remains described
in the existing spreadsheet and flexible-drafting reports. No hosted-server,
large-workbook, or concurrency claim is made by this local test.
