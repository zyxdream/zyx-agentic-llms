# Agent foundation live results — November 6, 2026

**Both new user cases achieved their core behavior, with zero operation errors
and no duplicate final responses. A generated-report wording defect remains.**
Clarification survived into a later assignment; report R stayed attached to
candidate A while separately unchecked revision B published normally. This is
case-specific evidence, not a claim that the foundation is bug-free.

Follow-up: the [November 7 fix retest](verification-display-fix-results-2026-11-07.md)
records the corrected report wording and unnamed-section display. The evidence
below describes the original run before those fixes.

The run was November 6, approximately 23:52–23:59 America/Chicago (November 7
UTC in evidence). [Cases 6–7](agent-foundation-live-cases.md) contain the exact
prompts/setup. The existing five cases were not rerun in this session.

## Environment and evidence

Local branch `Monica-agent`, HEAD `ba0d0215`; only test documentation changed.
The normal local app, `instance/myapp.db`, Redis, and Celery were used. An idle
check found zero active workflows/preparations; a baseline SQLite backup was
taken before restarting Flask, main/document/extraction workers, Beat, and
frontend together. Commands/PIDs are saved in `service-restart.json`.

Both cases used browser control, fresh chats, empty Case Background, Deep mode,
and UI upload of `order-invoice-correction.txt`. Real backend/worker/model work
ran without mocked APIs, progress, or tool results. Main-worker logs confirm
`gpt-6.1-sol`; preparation coverage records `gpt-6-luna`. SQL observation used
read-only connections and `query_only`, without app/status/recovery imports.

Evidence/backup folder: `artifacts/live-case-assistant/agent-foundation-20261107-0552/`
(ignored local artifacts). Primary evidence:

- [Case 6 SQL](../../../artifacts/live-case-assistant/agent-foundation-20261107-0552/case6-current.json), [independent audit](../../../artifacts/live-case-assistant/agent-foundation-20261107-0552/case6-independent-audit.json), and [follow-up browser](../../../artifacts/live-case-assistant/agent-foundation-20261107-0552/case6-followup.jpg).
- [Case 7 SQL](../../../artifacts/live-case-assistant/agent-foundation-20261107-0552/case7-final.json), [independent audit](../../../artifacts/live-case-assistant/agent-foundation-20261107-0552/case7-independent-audit.json), [staged browser](../../../artifacts/live-case-assistant/agent-foundation-20261107-0552/case7-candidate-staged.jpg), and [saved report preview](../../../artifacts/live-case-assistant/agent-foundation-20261107-0552/case7-report-preview.jpg).
- [After-reload integrity audit](../../../artifacts/live-case-assistant/agent-foundation-20261107-0552/after-reload-integrity-audit.json): editor, final results, step/artifact hashes, turn counts, and analytics remained unchanged; zero active workflows/preparations at the end.
- [Case 7 after reload](../../../artifacts/live-case-assistant/agent-foundation-20261107-0552/case7-after-reload.jpg) shows the persisted final draft with both body paragraphs expanded.

## Case 6: clarification memory and analysis-only output

Chat/draft **246**, public ID `5228b2bd-ffa2-4213-9a1f-456d007ec18c`.
First workflow: `wf_ab39d801-eb66-46fb-9cd4-fa98d8abf089`;
follow-up: `wf_32671fea-cb6b-44de-bb10-737649f5460d`.

| Expected | Actual | Outcome |
| --- | --- | --- |
| Ask scope, save actual answer, resume the same workflow. | One question asked agreed terms versus disputed invoice. Step `5d96e4e4-02af-40ab-82af-26d5d884b827` contains the complete Q/A; that workflow completed. | Achieved |
| First answer gives only selected amount/date/source; omits the next-turn heading preference. | `$240; delivery deadline: June 10 (order-invoice-correction.txt, § 1, “Agreed order”).` No heading. | Achieved |
| Next assignment receives the actual clarification, including wording absent from the first answer. | Its saved controller conversation contains the question and full answer verbatim, including `Order status.` and the final-period instruction. | Achieved |
| Exact heading, two bullets, agreed scope and source uncertainties. | `Order status.`; two actual browser list items; twelve × $20 = $240 / June 10; unknown acceptance, payment, delivery, agreed changes, resolution preserved; real sections 1/3 cited. | Achieved |
| Editor remains blank; reload keeps one answer per assignment. | Both candidate IDs are null; no draft candidates or permission check. Two final turns, one completion marker per workflow; previous result/artifact hashes preserved. | Achieved with baseline qualification |

The empty editor normalized from `{}` to `{"title":"","sections":[]}` on
answer-only save: no substantive edit, but initial/final hashes differ. It then
remained unchanged between answers/reload. This does not test preservation of
an existing nonempty document or the guard blocking an attempted publication.
Actual tools: first `request_user_input → read_sources → complete_task`;
follow-up `read_sources → complete_task`, both answer-only. Source file ID:
`6b1b2eaa-40b1-4fb1-a7ca-a8d6fa5bb1a1`.

## Case 7: the verification report stays about candidate A after revision B

Chat/draft **247**, public ID `59f7f99f-b2bd-4dc6-9c3b-3d0a9a70b4d3`;
workflow `wf_674717ef-b79b-473d-9cfd-ed21638ed8da`.

| Item | Actual artifact ID |
| --- | --- |
| First candidate A | `f00f38f9-4759-4917-870c-a1b56fad9829` |
| Verification report R | `e967d32c-74b7-4016-8a69-d2b1c86ac2fa` |
| Revised candidate B | `aea43a43-8684-421b-8cb3-a8a80e06b552` |

| Expected | Actual | Outcome |
| --- | --- | --- |
| A has exact title, two paragraphs and qualified source facts. | `Order status.`; agreed $240 / June 10 versus disputed $260 / June 12; unit count/price, listed-fee limitation and all source uncertainties retained. | Achieved |
| R identifies A consistently and records check scope. | Result, saved `target`, and step summary identify A; exact checking instructions, `check_citations: false`, `error: false` saved. Title-period check correctly passes. R's prose has the separate status defect below. | Association achieved; wording defect |
| Review findings, then change only the first paragraph. | Controller read R; revision targeted A with original-source material and R. B's first paragraph is one sentence beginning `Agreed terms:`; title/second paragraph unchanged. | Achieved |
| R stays about A; publish B without mandatory rechecking. | One verification/report only; R/ready-result hashes unchanged; final editor equals B. Final response says initial version was checked and published wording revision was not checked separately. | Achieved |
| Stage candidates and persist one final publication after reload. | Checkpoint 036 captured A/R with empty editor and zero final turns; browser staging agrees. Final/reload has B, one final turn/marker. B-before-publication SQL snapshot was not caught. | A/R staging and persistence achieved; B staging not independently captured |

Actual tools: `read_sources(original) → create_draft(A) → read_sources(A) →
verify_draft(A → R) → read_sources(R) → revise_draft(A → B) → complete_task(B)`.
These were the user's requested review/edit stages, not an application gate.
The guard returned `not_forbidden` from explicit permission to edit. This tests
permitted publication only; no outside research/citation audit ran. Source
file ID: `55e524f6-6bd8-4fb6-880f-efe5aa6aa503`.

## Performance and usage

| Saved SQL measurement | Case 6 first | Case 6 follow-up | Case 7 |
| --- | --- | --- | --- |
| Accepted-to-terminal | 19.117 s | 7.537 s | 64.109 s |
| Accepted UTC | 05:54:37.447099 | 05:55:19.570469 | 05:56:17.517860 |
| Terminal UTC | 05:54:56.563971 | 05:55:27.107572 | 05:57:21.626928 |
| Controller decisions / tools | 3 / 3 | 2 / 2 | 7 / 7 |
| Operation errors / final answers | 0 / 1 | 0 / 1 | 0 / 1 |

Source preparation was **15.832 s** (Case 6) and **14.065 s** (Case 7), excluded
from workflow durations. Case 6 reached its question step after **2.338 s**;
question creation to saved answer was **11.845 s** including human/UI waiting;
answer-to-terminal was **4.933 s** at millisecond precision. No SQL waiting-state
snapshot was caught; browser Q/A and durable same-workflow continuation exist.
Case 7 tool intervals: create **9.497 s**, verify **18.307 s**, revise **4.923 s**,
complete **4.295 s** (including editor permission check). Step intervals include
queueing/execution, not isolated provider latency. No arbitrary timing pass
threshold or benchmark claim is introduced.

Per-chat `TaskAnalytics`: Case 6 **7 recorded provider responses / 19,881 tokens**;
Case 7 **13 / 46,894**. These include instrumented chat work such as later
conversation-brief updates, exclude document preparation, and are not per-step
counts or complete billed-cost estimates. Runtime audit found 20 main-worker
and four preparation-worker HTTP responses, all 200; zero logged provider
attempt failures/retries or ERROR/CRITICAL/Traceback events. A single small
run is insufficient to compare operational cost or production latency.

## Observed issue, improvements, and limits

**Confirmed report wording defect:** saved R ends with “no report artifact was
saved because no file-saving tool is available in this interface.” SQL, its
returned artifact ID, browser **View saved result**, and revision material all
prove it was saved. The final controller response correctly calls it saved.
This is false generated status text, not an execution/persistence failure.
A minimal future fix is to ask the verifier for findings only and let the
application own persistence status; no new gate/call is needed. No fix was made.

**Known retained legacy wording:** A's private `generation_notes` says “To the
right” before publication; B's notes similarly describe live-editor changes.
The immediate tool/UI messages correctly say **Draft candidate saved; editor
unchanged.** This is an observed existing limitation, not a new regression.

**Usability:** the lone unnamed body section appeared collapsed under native
**Details** until clicked after reload; both paragraphs were saved and visible
on expansion. Consider its default presentation separately from agent logic.
The Case 6 accessibility snapshot's unusual bullet representation was checked:
the live DOM had two list items, so no markup defect was confirmed.

The independent audits' recorded checks passed, without erasing the wording
defect. No compaction threshold, delivery outage, worker crash,
lease expiry, duplicate race, cancellation/Retry, unavailable citation audit,
provider failure, or negative permission-guard path was induced. These runs do
not prove production concurrency or other models/tools. No product code was
changed for this run; these test documents were uncommitted at the time.
