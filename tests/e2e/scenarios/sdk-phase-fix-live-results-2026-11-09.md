# SDK phase parsing fix — local verification, November 9, 2026

The application continues to use the SDK's structured-output parser. This change
aligns the local Conda runtime with the existing `openai==3.26.0` repository pin
and adds regression coverage; it introduces no application parser, prompt,
schema, model, retry, fallback or drafting-behavior change.

## Environment and regression evidence

- OpenAI SDK: **1.102.0 → 3.26.0**. Pydantic remains **2.9.2**.
- All five backend services were restarted with the `lexintel` interpreter.
  Their startup logs confirm SDK **3.26.0**; diagnostic capture is disabled.
- The required `h11` update exposed an older local `httpcore`; it was aligned
  with the existing **1.0.9** repository pin.
- The first live preparation attempts exposed a separate transport error:
  `TypeError: process() takes no keyword arguments`. The new `httpx2` transport
  calls Brotli's decoder with `output_buffer_limit`, unsupported by the local
  Brotli **1.0.9**. Updating that existing optional dependency to **1.2.0**, the
  minimum required by `httpx2[brotli]`, resolved the error. Both uploads then
  prepared successfully through the normal UI Retry action. No application
  workaround or compression-header override was added.
- Both exact previously captured response envelopes now parse successfully,
  including when Brotli-compressed and served through native `httpx2` mock
  transport. Each replay uses one mocked HTTP request and the unchanged draft
  schema.
- **107 backend tests passed**: 100 across response helpers, diagnostics,
  drafting, downloads and exports, plus seven preparation-summary tests.
  Fourteen new actual-SDK mocked-HTTP cases cover both
  typed helpers, commentary before final JSON, legacy unphased messages,
  diagnostic capture enabled/disabled, invalid final JSON, refusal and
  incomplete output. Compatible dependency versions and repository pins were
  otherwise retained; no full environment reinstall was performed.

## Live scenario

The unchanged [spreadsheet-to-Markdown scenario](flexible-document-drafting-live-case.md)
completed through the normal local UI with real LLM calls and Docker
Python execution. Inputs are the same `invoices.xlsx` and headerless accounting
`ledger.csv`; the initial prompt matches the earlier diagnostic prompt
byte-for-byte. Fresh matter **18**, task **256**, Deep mode, `gpt-6.1-sol`:

`http://localhost:3000/case-assistant/108014fa-3bf7-4e41-b1f1-2cab01078ef7`

Initial workflow: `wf_9867f367-d059-4ebf-9a92-5ceddaee3b1d`.

The first controller catalogue contains exactly the two accepted files. Both
preparations are ready at generation 2 after the dependency-related retry.
Independent read-only database checks confirm that stored originals and fixture
hashes still match.

## Observed results

- **Initial authoring and revision each succeeded on their first attempt.**
  Both verification operations succeeded. There were **zero authoring SDK
  validation failures**, versus the reproduced failures with SDK 1.102.0.
- Python inspected the relevant last worksheet with row-4 headers and all eight
  headerless CSV records. Formula evaluation used a working copy. The agent
  corrected two ordinary script result-serialization errors (`datetime` and
  `PosixPath`) without tester intervention, then saved the complete analysis.
  The writer received that full calculation artifact directly.
- The published report contains three actual tables with all six invoice rows,
  three vendor balances and three exceptions. All values match the independent
  oracle: **$4,660 invoiced; $1,700 applied; $2,960 outstanding; $2,000 overdue
  across four invoices**. Leading-zero IDs, pending/reversed/unmatched treatment,
  precise source references and the undated-ledger limitation are preserved.
- The revision changes exactly the requested executive-summary sentence and
  P8 Treatment cell. A source diff confirms that every other word and table
  value is unchanged. Initial revision `e42a7765-2a6e-48e3-bae8-70f36c739127`
  remains saved; revised `b66308d4-bdbf-4b5f-b6ff-9406dab38622` references it
  as its parent. Revision workflow:
  `wf_0ea7cfbe-2a41-4404-ac39-670e50d0d094`.
- Reload retained the revised report and saved results. Preview renders tables;
  Source displays Markdown headings and emphasis. The downloaded Markdown is
  byte-for-byte identical to the authoritative source.
- **Markdown, HTML, Word and PDF downloads passed.** Markdown, HTML and DOCX
  each contain three tables; Word uses native editable table rows/cells.
  All three rendered DOCX pages and all three PDF pages were visually inspected:
  no clipping, overlap, missing glyphs or broken tables. The narrow Treatment
  column wraps, with its complete text readable.
- Downloading the original workflow's saved Markdown export again produces
  exactly the initial bytes and leaves the revised draft current. All export
  hashes match their browser-downloaded files. **Six download attempts, six
  successes, zero failures, zero LLM calls/tokens.** Overall LLM counts and
  tokens remain unchanged during exports.
- Stored originals and fixture SHA-256 values match throughout. Earlier
  analysis/artifacts remain unchanged; no workflows or preparations remain
  active. Local services remain running with the updated SDK.

Independent audits passed **90 initial checks, 97 revision checks, 113 final
checks and 11 browser/source checks**. This verifies the local environment;
no server deployment was changed.

## Local evidence

Ignored test evidence is under
`artifacts/live-case-assistant/sdk-phase-fix-20261109/`, including runtime
versions, process startup logs, compressed response replay results, regression
results, preparation failure/retry evidence and independent workflow audits.
`downloads/` contains the six actual browser downloads; `qa/` contains table
comparisons and all rendered-page evidence. `sdk-fix-completed-ui.png` shows the
published revised report, its rendered table and the download controls.
