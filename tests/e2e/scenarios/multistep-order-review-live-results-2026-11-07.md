# Case 8 live results — November 7, 2026

**The complex case achieved its core requirements and published a source-faithful
memo. It also exposed intermediate wording, formatting, and efficiency issues
that the controller corrected through additional ordinary operations.** There
were no operation errors or manual Retry, but this was not an unqualified clean
first attempt. [The specification](multistep-order-review-live-case.md) contains
the six fictional fixtures, exact prompt/clarification, and arithmetic oracle.

## Run and evidence

Normal local app/SQLite/Redis/Celery, branch `Monica-agent`, HEAD `ba0d0215`, with
the preceding uncommitted verifier-prompt/unnamed-display fixes loaded. Idle
checks and a SQLite backup preceded the run; healthy existing services were
used. No product changes were made for this case. A fresh chat used Deep mode,
empty Case Background/editor, and browser upload of all six TXT files. The real
UI/backend/LLM executed the request; no APIs, decisions, results, or progress
were mocked. Assignment/chat model was `gpt-6.1-sol`; preparation `gpt-6-luna`.

Chat/draft **249**, public ID `ea1f0f5c-1d45-47f1-aff7-ea622b01ba3a`;
workflow **`wf_b5d2a204-82ce-4ab2-be1b-030cec5a06f9`**. Reporting cutoff inside
the fictional task was **September 12, 2026**, not the actual test date.

Local ignored evidence: `artifacts/live-case-assistant/multistep-order-review-20261107-0619/`.
Read-only SQL observation used `query_only` without importing app/recovery code.
Results below compare actual source/analysis/memo contents to the fixture oracle,
not merely the verifier's generated “PASS” verdict.

- [Final SQL snapshot](../../../artifacts/live-case-assistant/multistep-order-review-20261107-0619/case8-final.json), [fixture oracle](../../../artifacts/live-case-assistant/multistep-order-review-20261107-0619/independent-fixture-oracle.json), and [six-file preparation audit](../../../artifacts/live-case-assistant/multistep-order-review-20261107-0619/case8-source-preparation-audit.json).
- [Published candidate C](../../../artifacts/live-case-assistant/multistep-order-review-20261107-0619/candidate-3.txt), [final consolidated verification](../../../artifacts/live-case-assistant/multistep-order-review-20261107-0619/candidate-verification-3.txt), and [browser after reload](../../../artifacts/live-case-assistant/multistep-order-review-20261107-0619/case8-after-reload.jpg).
- [Table left side](../../../artifacts/live-case-assistant/multistep-order-review-20261107-0619/case8-financial-table-left.jpg), [right side](../../../artifacts/live-case-assistant/multistep-order-review-20261107-0619/case8-financial-table-right.jpg), and [timeline after reload](../../../artifacts/live-case-assistant/multistep-order-review-20261107-0619/case8-timeline-after-reload.jpg).
- [Independent final audit](../../../artifacts/live-case-assistant/multistep-order-review-20261107-0619/case8-independent-audit.json) confirms all 37 recorded checks; [prior-record integrity](../../../artifacts/live-case-assistant/multistep-order-review-20261107-0619/case8-prior-record-integrity-audit.json) confirms earlier test records were preserved. No active assignments or preparations remain.
- [Scoped runtime audit](../../../artifacts/live-case-assistant/multistep-order-review-20261107-0619/scoped-runtime-log-audit.json): 39 chat and 12 preparation HTTP responses, all 200; zero logged provider retries or execution errors.

## Expected versus achieved

| Expected behavior | Observed evidence | Outcome |
| --- | --- | --- |
| Ask audience before writing; preserve the actual clarification. | One audience question; reload retained it. Saved answer specifies internal audience, exact title, neutral tone, and “balance pending reconciliation.” Subsequent instructions/results preserve these requirements. | Achieved |
| Audit uploaded bad draft; inspect saved analysis and use supported findings. | All six uploaded files were read in full. The first report targets file 06, identifies its unsupported claims, and is read back. Two saved analyses followed; the corrected analysis and source/audit materials feed candidate creation. | Achieved, with corrected analysis wording below |
| Distinguish agreed amounts from invoice claims and unapproved credit. | Final memo: amended **$1,490**, invoice **$1,690**, discrepancy **$200 = $140 rate uplift + $60 rush**, confirmed deposit **$400**, **$1,090 balance pending reconciliation**, and **$950 only if credit later approved/applied**. The distinct $140 credit request is not payment or the rate uplift. | Achieved |
| Preserve receipt, acceptance, timing and payment limitations. | **20 physically received, including 2 damaged**; 18 merely had no damage recorded. Remaining 8 unverified and September 17 deadline future at cutoff. No replacement, acceptance, full performance, refund, or overdue balance inferred; both payment conditions preserved. | Achieved |
| Provide table, chronological timeline, gaps/questions and neutral internal memo. | Exact title; neutral internal-only memo; seven chronological dates; practical evidence requests. Final financial section has a multiline fenced plain-text table, allowed by the prompt. | Achieved after revision |
| Check the actual final candidate and use findings to correct it. | B's report identifies two genuine pinpoint citation omissions; C fixes only those and delivery-deadline wording. C's reports target C; consolidated check includes B and actual workpapers for requested comparison. Independent C/B diff confirms three intended edits only. | Achieved after additional check |
| Keep candidates private, publish once, persist through reload. | Checkpoints capture A, B, and C with empty editor/zero final turns. Final editor equals C, one final answer/completion marker, all six sources retained after reload. | Achieved |
| Stay within supplied evidence; preserve saved history. | Reports distinguish originals 01–05 from bad draft/generated workpapers; real filename/paragraph references; no outside research, legal authorities, or sending operation. Superseded artifacts remain saved. | Achieved |

## Saved artifacts and actual work

| Saved item / checked target | Artifact ID |
| --- | --- |
| Full six-file read | `22aba971-1553-4df5-b3b0-e83dd2fef716` |
| Uploaded-draft audit → file `6ff1ac7a-cfb1-4604-b387-87aee6ff6d70` | `062833dc-d927-4bc3-9ff1-c0f4b865035f` |
| First analysis, retained | `b8f42bb2-0b87-4db9-94da-c9fdeb4a80fc` |
| Corrected analysis used in memo | `5dd9a490-5c58-44ba-89eb-4d29e9c3242d` |
| Candidate A | `b8a4f7a2-23c4-4e65-88f3-b577c97385f0` |
| Candidate B | `b9c5284c-cb46-45a8-98ae-cefa13f504ae` |
| Verification → B | `82eeacc1-7e0e-4620-b97b-1d0783be1478` |
| Published candidate C | `c35b981b-caf5-4c84-aca8-d3e54a427e80` |
| First verification → C | `f66b951c-513b-4765-8272-a1666e939b59` |
| Consolidated verification → same C | `591bc663-83ba-4d61-b30b-3978159b1149` |

The agent chose: clarification → full source read → uploaded-draft audit/read
→ analysis/read → corrected analysis/read → A/read → B/read → verify B/read
→ C → verify C/read → consolidated verify C/read → publish C. There were
**20 decisions and 20 tool executions**: nine reads, four verifications, two
analyses, one creation, two revisions, one question, one completion. Each step
finished `ready`; revisions/checks were controller choices in response to
observations and the user's requested work, not an application verification gate.

## Performance

| Measurement | Observed |
| --- | --- |
| Six-source preparation, first created to last ready | **33.927 s**, separate from assignment |
| Accepted → terminal | **527.465 s** (8m47.465s), 06:25:39.935446 → 06:34:27.400519 UTC |
| Clarification wait | Approximately **44.4 s**, including human/browser waiting |
| Answer saved → terminal | **481.096 s** |
| Analysis tool intervals | **49.709 / 30.542 s** |
| Verification tool intervals | **47.157 / 59.597 / 30.638 / 22.880 s** |
| Create / two revisions / publication | **48.764 / 42.202 / 10.763 / 4.300 s** |
| Instrumented chat usage | **39 provider responses; 687,175 total tokens** |
| Operation errors / manual Retry / final answers | **0 / 0 / 1** |

SQL step intervals include scheduling/queueing and execution, not isolated
provider latency. Chat totals include instrumented helper work and later
conversation-brief updates, exclude document preparation, and aggregate repeated
input/output across calls: **687,175 is not the size of a single context**.
The separate 12 preparation HTTP responses are not included in the 39 chat responses.
No compaction checkpoint was created; this is not a failed compaction attempt.
No complete billed-cost estimate or new
latency/step limit is inferred from this one run.

## Recovered issues and improvements to consider

- **False capability prose in analysis:** the first saved analysis claimed it
  lacked file-saving/editor access and proposed a filename “ready to save,”
  despite its durable artifact. The controller noticed, requested a replacement
  analysis, read it, and used that version. This added ordinary model/tool work;
  the replacement analysis call alone used **30.542 s / 12,892 tokens**.
  It was not an execution error, automatic task retry, or manual Retry. The
  earlier artifact remains in SQL. The verifier-specific prompt fix held here;
  the analysis operation has the same content/status-boundary issue to address
  separately, without another LLM call or mandatory loop rule.
- **First candidate formatting:** its financial table was one flattened
  paragraph and timeline order needed correction. The controller read/revised
  it; B/C retain actual table line breaks and chronological entries. The exact
  generation/normalization cause was not isolated in this run.
- **Check-input assembly:** the first C verification honestly could not confirm
  the controller-requested B→C comparison because B was absent, nor inspect referenced workpapers whose
  contents were absent. Supplying them for a consolidated check resolved the
  scope limitation, adding **22.880 s** of verification plus surrounding work.
  Better-matched instructions/materials could avoid this overhead; no new
  verification requirement or rule was added.
- **Table usability:** browser DOM confirms nine preserved lines, `white-space:
  pre`, about **498 px** visible width versus **920 px** content width. Horizontal
  scrolling reached the invoice column and worked after reload. The requested
  plain-text table is readable, but needs horizontal scrolling in this panel.

The final memo/source comparison passes; intermediate corrections and substantial
cumulative usage remain material findings. This test did not exercise Excel,
outside research, external writes, cancellation/Retry, worker/delivery outages,
duplicate races, production locking, or context-threshold compaction. No code
fix or commit was made for this test case.
