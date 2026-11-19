# Waymo / Uber live drafting evaluation

Status: **executed live through the browser on 2026-11-14**. Seven scenarios /
ten turns completed using the current local server and real OpenAI providers,
after explicit user approval. All core source-fidelity, drafting, text-preservation
and rollback checks passed; one Python tool error recovered. All ten Outputs and
DOCX downloads were verified. Run times were 11–170 seconds (median 72 seconds),
excluding preparation; no matched historical performance baseline was run.

This pack has no matched baseline on the post-refactor code. Its recorded run is
listed with the other live suites in [the live registry](../../README.md).

The detailed local report and evidence are in the ignored folder
`artifacts/live-drafting-waymo-20261114-165411/report.md`. The new evaluation
project is `Live drafting - Waymo Uber - 2026-11-14 19:12:15` (ID 5).
Application code, original input documents and the other application's server
processes were unchanged during testing. This pack adds no task-specific application code.

## Inputs and storage

The four PDF samples are unchanged copies from the original project's static
folder. They total **110 PDF pages**. They are real case materials, not synthetic
fixtures, and are deliberately separate from `tests/live/legal`'s synthetic corpus.

The PDFs live only under the ignored project-local folder:
`instance/evaluation-inputs/waymo-uber/`. `documents.json` records exact filenames,
SHA-256 hashes, page counts, source provenance and the normal project upload set.
The original source files are unchanged. Nothing in this pack imports another
project's users, database, configuration, storage service or Git history.

The RFP bears a confidentiality legend; keep the inputs local. This pack does not
stage them in `frontend/public`, publish them or initiate external-provider calls.
The separate `inputs/fictional-client-facts.txt` is synthetic and is uploaded only
for the two new-matter/style cases. Never upload this README, `documents.json`,
`scenarios.json`, `reviewer-checks.json`, screenshots or evaluator notes as evidence.

## Cases

| # | Scenario | Turns | Selection |
| --- | --- | ---: | --- |
| 1 | Multi-document change brief, then audience rewrite | 2 | Core |
| 2 | Source-aligned response matrix with numbering traps | 1 | Core |
| 3 | Professional style transfer with separate factual authority | 1 | Core |
| 4 | New structured document from a format example and new facts | 1 | Core |
| 5 | Draft conditional responses without fabricated production facts | 1 | Core |
| 6 | First-upload edit, follow-up preservation and explicit rollback | 3 | Core |
| 7 | Optional mixed text/image reading and redaction-aware drafting | 1 | Optional |

All exact prompts are in `prompts/` and their ordering/input keys are in
`scenarios.json`. The initial comparison and its audience rewrite share a chat;
the focused RFP edit, follow-up and rollback share another. Other cases use fresh
chats so facts and drafts cannot leak from a previous task.

## Run through Workspace

1. Use `http://localhost:3010` and the normal signed-in Workspace account. Confirm
   API 5010 and Redis 6390 belong to this project. Leave the other application's server alone.
2. Create `Live drafting - Waymo Uber - <timestamp>` and upload the original
   complaint, FAC and Answer to **Project documents** once.
3. Open a fresh chat for a case. Attach only the files in that case's
   `prompt_document_keys`; the RFP and fictional facts are prompt attachments where
   specified. Wait for preparation, inspect warnings, then submit its first prompt.
4. Inspect progress, saved sources and material purposes. Follow-ups use the same
   chat/current Output without reuploading. Download DOCX after each confirmed edit
   in the revision case and preserve the corresponding Markdown/version metadata.
5. Review source accuracy, task fulfillment, source roles, unanswered facts and
   writing quality against `reviewer-checks.json`. A completed run is not proof of
   correctness. Use normal controls and do not send or file the generated work.
6. Capture run/model/timing/tool evidence in ignored `artifacts/`. The existing
   `scripts/live_acceptance_report.py --project-ids <new IDs>` can export scoped
   read-only run evidence. Record the current commit **and** uncommitted change
   identifier because the current implementation has not been committed.

Starting cases makes real provider calls. Preparing the pack alone does not
start any. Keep the historical-source cases source-only as prompted. They do not require
current-law research or later litigation outcomes. No timing baseline has yet been
measured for these documents.

## Source-specific evaluation traps

- The Answer expressly responds to the FAC and is on behalf of Uber and Ottomotto;
  the caption also names Otto Trucking, which must not be mistaken for an answering
  party in this document.
- The Answer has its own numbered preliminary statement. Its paragraph numbers are
  not the same identifiers as the FAC allegations it answers.
- The RFP has 27 pages in this copied PDF but court-stamp pagination runs from
  page 2 to page 28. State **PDF page** when using a one-based viewer page number.
- RFP PDF pages 9 and 10 have image bodies plus a small text court stamp; they are
  not blank. They contain visible requests and blacked-out text. The optional case
  distinguishes successful OCR/read coverage from honest disclosure of a gap.
- Existing paragraph segmentation and source retrieval are available. These cases
  assess the general agent's choices and output; they do not add a dedicated
  paragraph-response workflow or a mandatory second reviewer.

The private PDF manifest and evaluator key were checked locally. The execution
record above and its local report contain live quality, ingestion, timing and
token-usage findings. Dollar billing and full visual layout quality were not
measured.
