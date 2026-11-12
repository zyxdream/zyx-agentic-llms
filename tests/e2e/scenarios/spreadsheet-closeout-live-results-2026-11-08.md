# Spreadsheet local closeout — November 8, 2026

The real local UI/LLM run covered mixed XLSX/CSV inputs, duplicate filenames,
headerless data, active Python cancellation, same-workflow Retry, saved-result
reuse, and authenticated CSV/XLSX downloads. No hosted server was changed.

## Run and inputs

- Branch: `Monica-agent`; normal local app, Redis, SQL database and Celery workers.
- Matter: **Excel analysis live test — 2026-11-08**; task **251**,
  public ID `a5fabc2d-41dd-4b44-b15e-5d5ed437ed36`.
- Workflow: `wf_dfa229fb-67f6-46a5-b0fc-9714587268ad` throughout Stop and Retry.
- Real controller: `gpt-6.1-sol`; real preparation: `gpt-6-luna`;
  real Docker image: `lexintel-python-analysis:1`. No provider responses or tool
  executions were mocked in this UI run.
- `invoices.xlsx` is an unchanged copy of the earlier synthetic invoice workbook.
  The relevant last sheet has headers at row 4 and six invoice records.
- Two distinct uploads share the basename `ledger.csv`: eight headerless payment
  rows and three unrelated marketing records with a header. The native file
  picker selected files from different fixture folders, but ordinary file upload
  does not preserve those folder paths. This run verifies duplicate basenames,
  separate file IDs, and content-based selection, rather than folder metadata.

The first Python call inspected all three candidates. The calculation selected
invoice ID `ed50bf34-2754-48c7-81f4-4265c6f872ef` and payment ID
`055c50e1-ef9e-46fc-852b-ef59c6c6d9e1`, excluding marketing ID
`1450a6f4-cab4-4539-a1cb-2904c58a590a`. `csv.reader` retained all eight payment
rows, including row 1 (`P1,000101,400,posted`), and text invoice identifiers.

## Cancellation and bounded fixes

The LLM first saved a complete checkpoint CSV and context JSON. A separate call
imported those saved artifacts, printed `SPREADSHEET_CLOSEOUT_ACTIVE` with
flushing, and began the explicitly requested 45-second development delay.
Docker container `13ecb9be3061` was observed running before UI Stop. The step was
cancelled about 26 seconds after starting; the worker returned `stopped` about
0.36 seconds after cancellation committed. The container disappeared and the
incomplete attempt directory was removed. The four earlier artifacts remained.

The baseline exposed missing recovery behavior: cancelled runs had no Retry
action and disappeared from history after reload. The fix permits explicit
Retry for cancelled version-1 agent runs, requeues unfinished steps under the
same workflow ID, and restores cancelled turns with saved-result access. Existing
source/draft/active-request checks and legacy cancellation behavior remain.
The progress viewer also refreshes stale running states after Stop and ignores
older refresh responses when newer progress is available.

Two small defects exposed during this test were also fixed:

- Preparation rejected a model summary exceeding its existing five-key-point
  limit, but the output schema did not express that limit. The schema and
  spreadsheet prompt now communicate the same existing bound. A fresh UI upload
  through the restarted document worker became ready with five key points and
  one real preparation-model call; originals/formulas remained unchanged.
- A shared Markdown link renderer used the obsolete positional callback API,
  causing generated-file labels to display as `undefined`. The current token
  callback preserves readable labels. File downloads still use the existing
  authenticated saved-results controls.

After worker restart, UI Retry resumed the same saved tool step and arguments
in a fresh container. Reload during execution retained the running request.
All five earlier completed steps kept their IDs, timestamps, arguments and
results. All four existing artifacts kept their IDs/content hashes, including
both checkpoint file hashes. Only the interrupted Python operation reran.

## Results and checks

| Result | Independently checked value |
| --- | --- |
| Complete source data | 6 invoices, 8 payment rows |
| Invoice total | $4,660 |
| Posted payments | $1,790: $1,700 matched and $90 unmatched |
| Excluded payment statuses | $100 pending; $1,000 reversed |
| Outstanding / overdue | $2,960 / $2,000 |
| Vendor balances | Alpha $800; Beta $1,000; Gamma $1,160 |
| Original integrity | All three fixture and stored-original SHA256 hashes unchanged |
| Final workflow | 8 steps, 6 artifacts, one published answer, unchanged editor |

The checkpoint CSV downloaded through the UI while the workflow was cancelled.
The final XLSX downloaded after Retry. Both browser-saved files exactly match
their stored derived artifacts. The XLSX has nine sheets; its summary has one
header and 28 metrics.

- CSV: 1,488 bytes; SHA256
  `9b109aa90aee99233dcb1353f8915d37b1e1777768d88b774a0c38c1397f3a2e`.
- XLSX: 14,258 bytes; SHA256
  `74d251605cfe56deb13c1ac40e10764cd2e43a3b32b04c775a4b8f8d3515c6d6`.
- Independent checks: 173 cancellation-baseline checks, 194 saved-work/retry
  checks, 128 actual-download checks, 88 final-reload checks, and 15
  fresh-preparation checks passed. Reload retained all eight steps and six
  artifacts without another execution or published answer.
- Conda `lexintel`: 110 preparation checks, 162 controller/history/workflow
  checks (4 existing optional skips), and 20 runtime/spreadsheet checks with
  actual Docker containers passed.
- Focused frontend: 37 tests across five Jest suites passed, including
  cancellation state, refresh races, download handling, actual Marked rendering,
  and existing Research behavior. Final UI reload showed readable filenames,
  with no Retry or Stop control on the completed turn.

The final local app and workers remain running. No analysis containers or
active workflows/preparations remained after the checks.

Ignored reproducible evidence is under
`artifacts/live-case-assistant/spreadsheet-closeout-20261108/`: fixture/oracle
files, before/after SQL snapshots, independent audits, screenshots, worker logs,
the exact submitted prompt, and the actual browser downloads. The finite delay
is test input only; no delay or special workflow was added to the application.
This validates the local synthetic scenarios, not hosted deployment or general
workbook-size/concurrency limits.
