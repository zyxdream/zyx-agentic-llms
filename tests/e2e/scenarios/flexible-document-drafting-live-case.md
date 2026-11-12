# Spreadsheet analysis to a Markdown financial report

This local UI test joins two capabilities previously tested separately: real
XLSX/CSV analysis and the new Markdown document writer. It uses fictional data
from the already validated spreadsheet closeout fixtures. The core scenario was
run locally on November 9, 2026; see the
[observed results and remaining findings](spreadsheet-to-markdown-live-results-2026-11-09.md).

The earlier [live drafting results](flexible-document-drafting-live-results-2026-11-09.md)
already cover prompt-provided tables, revisions, reload and four downloads.
This scenario additionally checks whether calculated results make it into the
draft correctly. It adds no application rules or required general tool order.

## Inputs and setup

Create a fresh local test chat with an empty draft and empty Case Background.
Upload these two files and wait for preparation to finish:

1. [invoices.xlsx](../fixtures/flexible-document-drafting/invoices.xlsx)
2. [accounting/ledger.csv](../fixtures/flexible-document-drafting/accounting/ledger.csv)

The workbook's relevant sheet is last, its headers are on row 4, and its amount
formulas have no cached results. The CSV contains eight records with no header;
its columns are payment ID, invoice ID, amount USD and status. IDs have leading
zeros. These facts and the expected results below are for the tester; upload
only the source files, not the oracle or this test guide.

Use the normal local app and workers with real model/tool execution. Docker is
needed for Python execution and PDF export. Keep **November 8, 2026** as the
reporting cutoff, regardless of the day this test is run.

## 1. Analyze and draft

Submit this prompt:

```text
Using the two uploaded files, prepare an internal financial review as of November 8, 2026. Use Python to inspect the actual workbook/CSV layouts, calculate from the complete relevant data, and save the analysis results for the report. Treat only posted payments as payments applied to invoices. Keep pending, reversed, and unmatched payments separately identified, and preserve invoice IDs including leading zeros.

Publish a document titled "Invoice reconciliation review — November 8, 2026" in the draft panel. Use a general business report layout with:
- A short executive summary stating invoice total, matched posted payments, outstanding balance, overdue balance and number of overdue invoices.
- An invoice reconciliation table containing every relevant invoice, with columns Invoice ID, Vendor, Invoice amount USD, Posted paid USD, Balance USD and Due date.
- A vendor balance table.
- An exceptions table with columns Payment ID, Invoice ID, Amount USD, Status and Treatment. Include the pending, reversed and unmatched records; explain their treatment.
- A short methodology and limitations section, source references identifying the relevant sheet/rows, and a numbered list of next actions. Use normal Markdown headings and emphasis where helpful.

Use only the uploaded records; no outside research is needed. Do not modify the uploads. Finish with the report in the draft panel and a short chat explanation of the main findings. I will choose the download format afterward.
```

Observe actual saved steps/results: selected file identities, Python code and
outputs, draft candidate and publication. Check that the draft uses saved full
analysis results, rather than treating catalogue samples as the full dataset.
Wording and useful intermediate operations can vary.

## 2. Check the visible draft and Markdown source

Preview should show three actual tables with these records. Totals may appear
in extra footer rows, but all six invoice detail rows must remain present.

| Invoice ID | Vendor | Invoice amount USD | Posted paid USD | Balance USD | Due date |
|---|---|---:|---:|---:|---|
| 000101 | Alpha | 1,200 | 700 | 500 | 2026-10-15 |
| 000102 | Beta | 750 | 750 | 0 | 2026-10-20 |
| 000103 | Alpha | 500 | 200 | 300 | 2026-11-01 |
| 000104 | Gamma | 960 | 0 | 960 | 2026-11-20 |
| 000105 | Beta | 1,000 | 0 | 1,000 | 2026-10-25 |
| 000106 | Gamma | 250 | 50 | 200 | 2026-11-05 |

| Vendor | Balance USD |
|---|---:|
| Alpha | 800 |
| Beta | 1,000 |
| Gamma | 1,160 |

| Payment ID | Invoice ID | Amount USD | Status | Expected treatment |
|---|---|---:|---|---|
| P5 | 000104 | 100 | pending | Exclude from applied payments |
| P6 | 000105 | 1,000 | reversed | Exclude from applied payments |
| P8 | 009999 | 90 | posted | Unmatched; do not reduce a known invoice balance |

The summary must agree with the tables:

- Invoice total **$4,660**; matched posted payments **$1,700**.
- Outstanding **$2,960**; overdue **$2,000 across four invoices**.
- Total posted payments are **$1,790**, including the **$90 unmatched** record.
- Pending **$100** and reversed **$1,000** remain excluded.

References should identify `'Invoice detail'!A5:F10` and CSV rows 1–8, or
equivalent precise locations. The archive's $37 must not enter current totals.
Missing formula caches must not silently become zero invoice amounts.

Click **Source**. It should contain readable Markdown headings (`#`, `##`),
pipe-table rows with separator lines, and numbered-list syntax. Switching back
to **Preview** should render the same content as tables, headings and a list.
The source view is read-only; revisions go through the agent.

## 3. Revise a paragraph and one table cell

Save/download the first Markdown revision, then submit:

```text
Revise the current report with exactly these two changes:
1. Append this sentence to the executive summary: "Four invoices have overdue balances requiring follow-up as of November 8, 2026." Keep the existing summary sentences and totals intact.
2. In the exceptions table, change only P8's Treatment cell to "Unmatched posted payment — request the invoice reference before applying $90."

Keep all other content, table values, identifiers, dates and the general layout unchanged. Publish the revised document.
```

Check the requested paragraph/cell in Preview and Source. Every financial
value and the other table cells must agree with the first revision. The new
revision should retain the previous revision in saved results.

## 4. Reload and download

1. Reload without resubmitting. Confirm the revised draft and saved results
   remain available and the report is not generated again.
2. Download **Markdown, HTML, Word and PDF** from the same revised document.
   Compare IDs, values, tables and revision text across all four outputs.
3. Open Word: its three report tables must be native editable Word rows/cells,
   rather than paragraphs containing pipe characters. Inspect PDF pages for
   wrapping, clipped cells or orphaned table headers. Document length can vary;
   this small case does not guarantee a multipage table.
4. Open the first workflow's saved Markdown export and download it again. It
   should match the first download exactly, while the current draft stays on
   the revised version. Exporting must not call the LLM or rewrite the source.
5. Compare uploaded original hashes with
   [original-sha256.json](../fixtures/flexible-document-drafting/original-sha256.json).

Record observed results, screenshots, workflow/revision IDs and downloaded files
in a dated report. The complete arithmetic oracle is
[expected-results.json](../fixtures/flexible-document-drafting/expected-results.json).
Distinguish any unobserved checks from passed checks.

## Optional file-selection extension

Repeat in a new chat with
[marketing/ledger.csv](../fixtures/flexible-document-drafting/marketing/ledger.csv)
also uploaded. It has the same filename as the payment CSV but unrelated,
much larger marketing budgets. Ask for the same review. Totals must remain
unchanged and saved input identities must show the accounting records were
used. Ordinary file uploads may show only the shared basename; distinguish
files by ID and content rather than assuming folder paths survive upload.
