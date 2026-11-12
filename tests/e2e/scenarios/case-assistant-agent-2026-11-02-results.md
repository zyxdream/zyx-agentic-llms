# Live Case Assistant agent results — November 2, 2026

**All five live cases passed their recorded checks and published one chat turn
each.** Browser observations and independent source/result audits are complete.
Recoverable tool errors, misleading messages, and quality/performance improvements
remain below; workflow completion alone was not treated as a factual-quality pass.

## Environment and method

- Application code: local `Monica-agent`, commit `d7905338`; migration
  `a8b9c0d1e2f3`. No application code was changed during this run.
- The user restarted the normal `dev-monica` backend, main/document workers,
  Beat, and frontend. This run uses `instance/myapp.db` and the normal local
  Redis workers, **not** the earlier isolated database copy.
- Prompts were submitted through browser control against the real UI/backend
  and LLM. No agent decisions, progress, tool results, or answers were mocked.
- Existing test chats 242/243 retained their prepared 3-file/100-file sources.
  All 103 preparation source hashes matched the repository sample manifest.
  The 100-document collection was reused, not reuploaded or reparsed.
- Case 5 used fresh test chat 244. Only its initial deliberately incorrect
  draft was seeded directly in SQL as fixture setup; its synthetic source was
  uploaded through the real native browser file picker and prepared normally.
- Baseline and atomic database backup were saved before submission. Evidence is
  local under `artifacts/live-case-assistant/agent-live-20261102-205421/`, including
  `baseline.json`, `before.db`, screenshots, reload records, and independent audits.

## Outcomes and saved workflow IDs

Times below are SQL workflow creation to terminal update, not preparation time.
All timestamps are UTC. Workflow IDs remain stable across browser reloads.

| Case | Result | Duration | Workflow ID |
| --- | --- | --- | --- |
| 1. Explain the Waymo License defense | PASS | 31.97 s | `wf_eb5488cf-551e-4df2-af5f-2871ee54c853` |
| 2. Rewrite as five client-facing bullets | PASS, with quality improvement below | 12.70 s | `wf_08ebda6f-7e56-4271-bab3-cb816b7a26fb` |
| 3. Find the Answer among 100 attachments | PASS | 23.13 s | `wf_3d2cdc83-2b77-4c79-9140-63705840ac24` |
| 4. Draft strategy and twelve production requests | PASS | 940.85 s (15m41s) | `wf_5e9f33e6-78ac-43f6-bb31-18956b075e0e` |
| 5. Verify, correct, and recheck an incorrect draft | PASS | 111.31 s | `wf_a6429e8d-8df8-40be-b7d3-e39805ce6d4a` |

1. **Case 1** ran `read_sources → complete_task` from 20:54:50.478935 to
   20:55:22.444314. Its 305-word answer preserves paragraph 184's exact wording,
   “Plaintiff’s products,” qualifications and alternative theories, cites supplied
   PDF page 22/paragraph 184, and does not establish a license as fact. Only
   Answer characters 52000:56000 were read; the nine-section editor draft stayed
   unchanged.
2. **Case 2** ran `read_sources → complete_task` from 20:58:41.222561 to
   20:58:53.923608. It produced exactly five bullets, reread a subset of the same
   supporting Answer range, retained qualifications/citations, and published one
   turn without changing the draft. Reload checks are saved in `test-2-reload.json`.
3. **Case 3** ran `search_sources → read_sources → read_sources → complete_task`
   from 20:58:19.571568 to 20:58:42.702577. It selected the correct Answer from
   100 accepted files and returned a 313-word response with the full exact quote.
   The first selected range stopped before the sentence ended; the agent chose
   an additional read before quoting it. Only 10,299 unique source characters
   were read, with no unrelated source or editor change. Reload records remain
   in `test-3-reload.json`.
4. **Case 4** ran from 21:00:06.784202 to 21:15:47.633888 (940.85 seconds).
   It read the complete originals of only the three correct Waymo pleadings
   (209,324 characters), saved analysis and a nine-section candidate, and chose
   verification before completion. Verification caught a technical paraphrase
   error (the receive path goes **via**, not **through**, the reflective surface)
   and an overinclusive express-denial citation range. Revision corrected those
   points and the title punctuation, and neutralized request wording to include
   contrary/inconclusive evidence. A final verification passed before publication.
   The selected candidate equals the published SQL draft/result and contains
   exactly twelve ordered RFPs, each with a substantive production demand,
   purpose/link, and source references. No unsupported license, completed patent
   withdrawal, or unrelated case fact was found in the final audit. Original
   editor state remained intact while candidates were staged. Browser reloads
   during and after execution preserved the same workflow; the final result
   count stayed six, including the existing historical turns. The analysis
   operation alone took about 272 seconds.
5. **Case 5** ran from 21:02:49.040182 to 21:04:40.347287. The incorrect draft
   said $260, June 12, and invoice acceptance; the final draft states agreed
   **$240 / June 10**, distinguishes disputed invoice **$260 / June 12**, and
   preserves uncertainty about acceptance, payment, delivery, changes, and
   dispute resolution. One final turn and completion marker were saved.

## Evidence of result-driven continuation

Case 5 demonstrates more than a predetermined multi-step display:

- Initial verification of `current_draft` found all three seeded errors. The
  agent read that report and passed both it and original source material into
  revision. An unsupported `preserve_original` target produced an explicit
  observation; the agent then selected `general_revision` successfully.
- Verification of candidate `e022e953-6d11-4328-9f85-c13db59eb045` found ambiguity:
  “disputed” might qualify the invoice total without qualifying the invoice date.
  After reading the finding, the agent changed the sentence to **“both disputed”**.
- The agent read and verified the new candidate
  `dc77ffab-f661-42c4-9484-5084358f8994`, read the final report, then selected it
  in `complete_task`. Thus an observed finding caused another correction/check.
- At 21:03:37.954 UTC, SQL showed a saved candidate while the initial editor
  draft and zero-turn chat remained unchanged. Final publication retained that
  original in `draft_before`; saved-candidate preview and reload evidence are
  recorded separately in the browser artifacts.

## Issues and improvements observed

- **Tool input contracts:** IDs for uploaded documents, preparation artifacts,
  and agent artifacts can be confused. `preserve_original` was attempted on a
  workspace draft/candidate and rejected, then corrected. Clarify ID types and
  target/mode compatibility in tool descriptions/schemas; no new limits added.
- **Misleading staged-result wording:** reused drafting responses say changes
  were made “to the current draft to the right” while only a candidate was
  saved. The editor correctly stayed unchanged, but the message implies otherwise.
- **Status wording:** `ready` means a tool observation has been saved, including
  recoverable error observations. It does not mean the requested operation
  succeeded. Distinguish successful results from errors in the displayed steps.
- **Material provenance:** a verification report questioned an artifact UUID
  because material content omitted the server-resolved agent-artifact ID.
  Explicit ID/type metadata would make supplied evidence easier to inspect.
- **Case 2 condensation:** 297 words versus case 1's 305 is weak shortening and
  client-language adaptation, although five bullets and source fidelity passed.
- **Long operations:** case 4's roughly 272-second analysis is a useful latency
  observation; the complete case took 15m41s across analysis, drafting, checks,
  and revision. Reduce redundant source/analysis input and expose operation
  timing before attributing the delay to Celery scheduling. This run alone
  does not establish representative performance.

There were four explicit recoverable tool errors: two material-ID mismatches
and one revision-mode mismatch in case 4, and one revision-mode mismatch in
case 5. The agent used their error observations to continue. No application
code, tool rule, limit, fallback, or retry policy was changed during testing;
the improvements above are recommendations, not fixes already applied.

## Limitations and evidence

Collected browser warning/error records were empty in this run. Initial
background-tab Send/CDP click problems were resolved by foreground interaction
and Enter; they are not a confirmed product submission bug. A missing ordinary
progress panel during one observation was not confirmed as a restoration defect.

These are local live acceptance cases, not production MySQL/concurrency tests.
They do not rerun 100-file upload throughput, OCR, near-1-GiB transfers, or all
provider failure paths. The concurrent local SQLite/Redis run does not establish
production MySQL, multiple-host, or load behavior.

The original prompts and case 5 specification remain unchanged. Detailed
`case-1-independent-audit.json` through `case-5-independent-audit.json`, per-workflow
JSON snapshots, source ranges, tool arguments/results, staging checkpoints,
screenshots, and reload evidence are retained in the local artifact directory
above. `test-4-production-requests-browser.txt` records the inspected request
section; `test-5-complete.png` shows the corrected editor after reload. Private
provider reasoning is not included here.
