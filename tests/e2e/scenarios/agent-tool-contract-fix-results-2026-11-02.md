# Agent tool-contract fixes: follow-up QA — November 2, 2026

**Cases 4 and 5 passed their browser and independent artifact checks, with zero
tool errors in both reruns.** A verification false positive remains documented below.
This memo records the approved follow-up fixes and retest. The separate
[original five-case report](case-assistant-agent-2026-11-02-results.md) remains
the historical record of behavior before these fixes.

## Changes being checked

- LLM-facing document catalogues, search results, and original-source references
  use `prepared_artifact_id` for a preparation version. `file_id` still identifies
  the uploaded source; `artifact_id` identifies a saved operation result.
- Tool descriptions explain which IDs belong in source ranges, material inputs,
  and revision targets. `general_revision` is described for `current_draft` and
  saved candidates; `preserve_original` remains an uploaded-draft mode.
- Operation materials include their server-resolved `artifact_id` and kind, so
  analysis/drafting/verification can identify the supplied saved material.
- Existing `read_sources` saved-body paging and offsets are unchanged. Accepted
  SQL snapshots retain their existing `artifact_id` keys; old records are not
  renamed or backfilled.
- A saved candidate now reports **“Draft candidate saved; editor unchanged.”**
  Rejected edits add an explicit warning. The drafting engine's original response
  is retained in the candidate's `generation_notes` instead of serving as the
  operation's public status message.
- Uploaded-preservation results keep their existing nested operation/rejection
  details. The old engine's `blocked` flag now reaches the adapter, which returns
  its clarification as an operation error instead of saving an unchanged draft
  as a candidate. This adds no automatic question or new revision restriction.
- In the steps UI, an error observation displays **Operation error**; a successful
  `ready` observation displays **Completed**. Historical messages remain intact.

No new database migration, tool restriction, verification gate, call limit,
automatic retry, or fixed action cycle was introduced by these changes.

## Environment and validation

The live retest uses the normal local `dev-monica` application, `instance/myapp.db`,
and local Redis/Celery queue. The main worker was restarted as PID 80727 with a
distinct hostname to load the changed Python code; the user's document worker
and Beat process were left running. After both requests completed, the idle main
worker was refreshed again as PID 83190 to load the uploaded-preservation handoff
correction. This was not the earlier copied-database run.

Automated checks passed: **61 backend tests** (32 agent-controller checks, two real
uploaded-revision bridge regressions, and 27 conversation-context checks) and
**9 frontend tests**. The bridge tests execute the real controller/adapter/phase-2
handoff, with the preservation helper/provider boundary doubled. They cover the
nested rejected-edit result and `blocked=True, error=False, draft=None` result
that workspace-target live cases 4/5 do not exercise. The combined backend run
passed in 32.05 seconds. These counts are distinct tests, not the sum of repeated runs.
Two existing `test_everything_full_content.py` checks also passed separately:
`test_phase2_preserve_original_response_annotates_citations` and
`test_run_preserve_original_uploaded_revision_reports_rejected_edits`.

The uploaded-preservation handoff correction was added during the live rerun.
Cases 4/5 use workspace targets; they do not exercise that uploaded-only branch.
Its coverage comes from the integration and existing helper tests above. The
worker was refreshed after the running request finished so local services also
load that final correction (`final-worker.json`).

Browser actions use the real UI, backend, worker, and LLM without mocked tool
results. The 100-file collection is reused rather than uploaded/prepared again.
Case 5 uses a fresh test chat 245 with the deliberately incorrect initial draft
as fixture setup. Evidence and baseline backup are under
`artifacts/live-case-assistant/agent-fix-retest-20261102-225533/`.

| Case | Result | Workflow ID | Duration |
| --- | --- | --- | --- |
| 5. Verify/correct/recheck the order summary | PASS | `wf_db2adfd0-ad42-4750-8aa2-9988dba0fd91` | 63.06 s |
| 4. Waymo strategy and twelve requests | PASS, with verifier caveat below | `wf_e2cb4aa7-0072-4f79-9b04-43273b1c6139` | 892.70 s (14m53s) |

## Case 4 evidence

The workflow ran from **22:56:41.668095 to 23:11:34.364381 UTC**. Its 17 tool
executions covered source reading, analysis, candidate creation, verification,
revision, another verification, saved-result reads, and final publication.

- Only the three intended Waymo originals were read, in full: 209,324 characters
  from `COMPLAINT-Waymo.pdf`, `Waymo v. Uber - FAC.pdf`, and
  `Waymo v. Uber - ANSWER.pdf`. The other 97 attachments supplied no evidence.
- **Zero tool errors.** Analysis used the saved source-material ID immediately;
  revision selected `general_revision` for a candidate on its first attempt.
  The verification report identified the supplied source artifact by its UUID.
- Verification found technical wording, a missing “at least” qualification,
  file-count versus event-count ambiguity, arbitration scope, and request
  targeting issues. Revision corrected these before publication. This is
  observed use of findings to choose another operation, not a mandatory cycle.
- The published candidate `2ac80b17-13ef-41b0-bbd6-45bcbcc70e2e` has nine sections,
  about 6,107 words, twelve disputed issues, and exactly twelve substantive
  production requests with purposes and source references. The paragraph 184
  quotation, procedural attribution, and license/uncertainty limits were preserved.
- Both candidate status messages use the corrected wording. A checkpoint while
  the first candidate existed confirms that the live editor still matched the
  baseline. Exactly one final turn was published; candidate and published draft
  match, with the original retained in `draft_before`.
- Browser reload during execution retained the running request. After completion,
  reload retained seven result cards before/after (six historical plus this new
  turn). **Refresh steps** retrieved all 17 saved steps with **Completed** labels.

**Verifier caveat:** the final verification demanded removal of the title's final
period. Its instructions supplied the expected title as an unquoted sentence,
making that punctuation ambiguous. The original user request quotes the title
with the period, and the published title correctly retains it. The final artifact
passes the original request; the final verification report is not an unqualified
clean pass. Quoting exact expected formatting values in operation instructions is
an improvement to consider; no new rule or automatic correction was added here.

## Case 5 evidence

The workflow ran from **22:58:52.517814 to 22:59:55.578815 UTC**. Its actual order
was source read, initial-draft read, verification, findings read, revision,
candidate read, verification, findings read, and `complete_task`.

- **Zero tool errors.** The first revision selected `general_revision` for
  `current_draft`, with valid saved source/finding IDs. Original verification
  findings informed correction; the revised candidate was checked before finish.
- The final summary correctly distinguishes the agreed **$240 / June 10** terms
  from the disputed invoice's **$260 / June 12** entries. Acceptance, payment,
  delivery, agreed changes, and dispute resolution remain unestablished.
- Source/material IDs were distinguishable; verification no longer raised the
  earlier missing-artifact-identity caveat. Unit checks separately verify exact
  envelope fields. Original engine notes remain saved in `generation_notes`.
- At **22:59:29.478 UTC**, while the workflow was running, SQL showed candidate
  `c2bae792-af3d-44ee-b1d6-5ffb9faa7a89`, the unchanged initial editor draft, and
  zero chat turns. Final publication selected that candidate and retained the
  initial draft in `draft_before`.
- Exactly one final chat turn was published. A post-completion browser reload
  retained one turn (`case5-reload.json`: 1 before, 1 after).
- The browser displayed the corrected candidate-status wording. Its screenshot
  was captured after completion: `originalDraftVisible=false` in the observation
  record does **not** prove an early editor write. In-flight staging is supported
  by the SQL checkpoint; no in-flight case-5 reload is claimed for this retest.

The 63-second run is one live sample, not a speed benchmark or a guarantee against future ID/mode misuse.

## Historical compatibility and remaining checks

The browser showed **Operation error** on three genuine recoverable-error rows
from the earlier case 4, and **Completed** on successful saved rows. Old drafting
messages were not rewritten, so historical text can still use the old wording.

At the final **23:13:52 UTC** integrity audit, all 16 baseline workflow records,
39 completed tool-history rows, and 18 artifacts matched their baseline hashes.
Model-output/encrypted-reasoning rows were excluded from this comparison.
These checks do not imply that the workspace is unchanged after new publication.

Both final artifact audits and reload checks passed with the observation limits
above. This memo makes no claim of a confirmed ordinary-progress restoration defect.

Local SQLite/browser checks do not establish production MySQL concurrency,
100-file upload throughput, OCR behavior, or every provider-failure path. Detailed
`case-4-independent-audit.json`, `case-5-independent-audit.json`, staging/reload records, historical-label evidence,
and `historical-record-integrity-audit.json` remain in the evidence directory.
