# Live browser case 5: verification drives a supported correction

This is one additional live Case Assistant use case, not a new application
feature or a fixed tool-order requirement. Run it through the normal browser UI
with the real backend, Celery worker, and LLM, using isolated test state.
Do not mock workflow progress, tool results, or final answers for this run.

## Why add this case?

The original four cases are recorded in
`artifacts/live-case-assistant/manual-tests-20261028/MANUAL_TESTS.md`:

1. Explain the Waymo License defense from the three prepared pleadings.
2. Rewrite that answer as five bullets using the same supporting passages.
3. Find the Waymo Answer among 100 attachments and explain the defense.
4. Draft a Waymo discovery strategy and twelve requests using three relevant
   pleadings selected from that collection.

They exercise factual qualification, follow-up context, attachment scale,
selection, drafting, and reload. They do not require a verification finding to
cause a subsequent correction. The mocked Playwright agent case checks browser
staging/clarification/restoration, but cannot establish real LLM action choice.
This case supplies an incorrect initial draft deliberately, so corrective work
can be tested without relying on the LLM to accidentally generate an error.

## Setup and exact inputs

Create a fresh Case Assistant chat in an isolated test matter. Leave Case
Background empty and use Deep mode, consistent with the original four cases.
Set the existing draft to a short document titled `Order summary`, with one
`Facts` section containing exactly:

```text
The order for twelve chairs at $20 each totals $260. Delivery is due June 12. The invoice has been accepted as correct.
```

Confirm that text is visible in the editor before submitting the test request.
Upload only [order-invoice-correction.txt](../fixtures/order-invoice-correction.txt)
as a task-specific document and wait for preparation to complete. Record its
document/artifact IDs and the initial editor state.

Submit this prompt once, preserving its wording for comparison runs:

```text
Using only the attached "order-invoice-correction.txt", first verify the existing draft against the source. Then revise only inaccuracies supported by that source, preserving uncertainty and distinguishing the agreed order from the disputed invoice. Verify the revised candidate against the same source before finishing. Preserve the short factual-summary format. Do not assume acceptance, payment, delivery, or resolution of the dispute. Use the filename and available section references. Do not research or cite legal authorities, and use no outside research.
```

## Run and collect evidence

1. Record the accepted workflow ID. Observe the real saved operation history
   and inspect source, verification, and candidate artifacts as they appear.
   Keep raw provider reasoning out of the report; operation names, arguments,
   result references, states, and timestamps are sufficient.
2. Capture a verification finding and a saved candidate while the assignment
   is still running. Confirm the editor still contains the initial draft before
   final publication. If this intermediate state was not observed, report that
   staging check as unobserved rather than assuming it passed.
3. Reload once after acceptance while work is in progress. Follow the same
   workflow through completion; do not submit the prompt a second time.
4. Inspect the published draft, final chat response, saved verification reports,
   and candidates. Reload after completion and confirm persistence. Record any
   errors, stalls, misleading status, unsupported statements, or duplicate turns.

## Pass criteria

- The initial-draft verification identifies the unsupported $260 order total,
  the wrong agreed deadline, and unsupported invoice acceptance. It distinguishes
  invoice entries from agreed order facts and retains the source limitations.
- The saved history demonstrates causality: original source is read, verification
  targets `current_draft`, its findings are read, and a later `revise_draft`
  receives those findings and relevant source support through `material_ids`
  or explicitly applies their source-backed corrections in its instructions.
  Inspect both arguments and replayed observations rather than requiring one
  exact argument shape. A subsequent verification targets the revised candidate
  and uses the source. Record actual IDs/order instead of assuming them from labels.
- The published summary states the agreed total is **$240** and agreed deadline
  is **June 10**. It may mention **$260** / **June 12** only as disputed invoice
  entries. It does not claim acceptance, payment, delivery, or dispute resolution.
  References identify the supplied filename and real sections without inventing
  pages or legal authority.
- Candidate generation/verification does not alter the editor before
  `complete_task`. Completion publishes the selected saved candidate once.
- Reload retains one workflow ID and one final answer for this submission.
  Saved candidates/reports remain inspectable under that workflow, and no
  unrelated source enters the assignment.

The causal checks follow the user's explicit request to verify, correct, and
verify again. Do not require one exact global sequence: document listing/search,
additional reads, analysis, or further corrective work may vary. Merely displaying
several completed operations does not establish that a finding informed revision.
If the final text is correct but an observable finding-to-revision connection is
absent, record that distinction as a failure of the adaptive-correction criterion.

## Result record

Keep the original four prompts/files unchanged. In the run report, record all
five cases separately with workflow IDs, actual operation order, elapsed times,
factual/UI outcomes, and links to artifacts/screenshots. Distinguish observed
bugs from possible improvements and test limitations. Do not report a mocked
browser pass or temporary SQLite result as production concurrency verification.
