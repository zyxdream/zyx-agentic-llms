# Authority discovery modes — local live results, November 10, 2026

Ran on `Monica-agent`, application commit `e51c7685`, using the normal local
app, authenticated browser UI, workers, real LLM and configured providers.
Documents were fictional; controller decisions and provider responses were not
mocked. No application behavior was changed during this test round.

## Restart and database

- Restarted Flask, the main agent worker, document preparation worker, Beat,
  legacy extraction worker and frontend. Redis was already running.
- Conda `lexintel`: `/Users/yuxiao/anaconda3/envs/lexintel/bin/python`, OpenAI
  SDK `3.26.0`. Frontend `http://localhost:3000`; Flask `http://127.0.0.1:5000`.
- Normal database `instance/myapp.db`: current revision and migration head both
  `a8b9c0d1e2f3`. No database upgrade or new migration was needed.
- Frontend returned HTTP 200 and compiled successfully; authenticated application
  requests returned 200. Flask root returns 404 because no root route exists.
  Main, preparation and extraction workers answered ping; Beat was running.
- Updated the normal-local-server runbook in `AGENT_CONTROLLER.md` after those
  startup checks succeeded.

## Actual requests and results

Matter: **Authority discovery modes — local test 2026-11-10**. Chats:

- Overview/audit/source reading: `259`, public ID `b16e61fc-ded2-424f-843e-9b6de598df80`.
- Boundary/scope: `260`, public ID `5d752e52-639b-45d9-94ce-ccea6f435078`.
- Invoice: `261`, public ID `bdc0d969-407d-4b3e-b11e-9cbf5b4b615c`.

| Request | Workflow | Observed result |
| --- | --- | --- |
| General overview | `wf_77793b02-d050-4d03-830e-f8712513676e` | Chose overview; initial lookup selected both legal files, excluded invoice, retained 9 representative original-document examples: 6 from brief and 3 from reply note. Additional caption-based research preserved conflicts. |
| Brief-only occurrence audit | `wf_c5b25b26-1dc5-426f-8289-f4d6c3c797bc` | Chose audit; selected only brief `[0,1145)`. All 7 expected appearances found, including identical same-paragraph citations with different propositions, typo, page variant, short form, name-only reference and statute. |
| Whole long-file audit | `wf_4a9e495b-bb86-4b67-b5de-d5eba39b0ec2` | All 4 appearances found in 25,204 characters. Wrapped Brown reference `[19968,20016)` crosses extraction boundary 19,977 and was saved once with its newline intact. |
| Narrowed follow-up | `wf_d9ac3a8f-2286-4cbb-8edc-c634ee0b7686` | Explicit range `[19920,20145)`, 225 characters between markers; exactly 3 current-scope appearances. Outside Miranda reference stayed historical. |
| Invoice control | `wf_db014a8e-45a5-4e56-a37f-0000b5657f23` | Only `read_sources` then `complete_task`. Correct totals: billed $850, paid $390, outstanding $460. No legal tools. |
| Focused source-backed check | `wf_6257ed3b-7bce-44d1-a81b-981e5b5795f4` | Read selected Brown opinion passage and official statute text, then saved a corrected evidence note and completed. Generated-analysis issue and recovery described below. |

All six requests completed. All saved tool steps reached ready state, with no
unhandled tool/worker failures. The controller model was `gpt-6.1-sol` in Deep
mode. Completion does not establish general extraction recall or legal validity.

## One actual overview-to-audit data flow

| Point | Brief actual LLM input | Chosen action/output | Executed result and next decision |
| --- | --- | --- | --- |
| Overview initialization | User asks for a working map, representative examples only. Prepared catalogue lists brief (1,145 chars), reply note (668) and invoice; editor summary, tools and conversation included. | `read_sources` for the two legal files, not invoice. | Original text saved as `d553a979-930b-4287-9746-d6f71ab1ee19`; returned to next controller call. |
| Overview lookup | Goal plus original-text observation. | `lookup_authorities(mode="overview", targets=[brief, reply], research_id=null)`. | CourtListener lookup then discovery; initial saved research `9f6f0bc9-c357-4979-be8a-11a713bef5fa`: 5 grouped reference records, 9 original-document examples, complete selected-scope processing. |
| Discovery inside that tool | Purpose, exact window text, extraction/context ranges, source units, mode and compact CourtListener candidates/statuses. | Brief discovery returned 6 mentions; reply discovery returned 3. | Python validated individual source spans. This nested LLM call did not certify identity or holdings. |
| Overview continuation | Compact lookup observation with research ID, counts, candidates and coverage. | Read the saved research, investigate conflicting Miranda identity, retry a timed-out lookup with `384 U.S. 436`, then complete. | Original conflict remained visible; no opinion or statute text was claimed read. Editor unchanged. |
| New audit request | User asks for every brief appearance only; conversation and saved overview catalogue are available, alongside the same accepted-file catalogue. | Read brief `[0,1145)`, then `lookup_authorities(mode="audit", targets=[brief range], research_id="1e7457dc-d9df-4c63-b0a7-0e99ea7d0cc1")`. | Mode-specific discovery reran; overview checkpoints did not substitute for the audit. |
| Audit discovery | Exact brief, scope `[0,1145)`, 7 source units; CourtListener returned 200 for `347 U.S. 483` appearances and 404 for `347 U.S. 484`. | Returned 7 structured mentions. The repeated full citation uses `unit_id="u197_469"` with `occurrence_index` 1 and 2. | Python resolved those two uses to `[247,295)` and `[339,387)` with distinct equal-access and school-remedy propositions. |
| Audit continuation | Observation identifies saved research `8135074c-3a7a-4407-b715-adb1178a116d`, audit mode and brief-only scope. | `read_sources` loads the full saved record; `complete_task(candidate_id=null)` publishes a seven-row answer. | Prior overview findings remain historical. No reply-note/invoice audit and no editor edit. |

The tool observation is compact. The controller explicitly read the saved full
research before writing its detailed answer. File IDs, prepared versions,
research IDs, unit/index anchors and source offsets have different roles; Python
owns source-position validation, while the LLM chooses research actions.

## Source reading and recovery

The additional focused check exercised `read_authority` and web research:

- A literal Brown phrase search missed; the agent changed the phrase to
  `equal terms`. It read only opinion `105221`, passage `[6267,18267)`, saved as
  `d2d3ef57-7259-4368-9d07-95ac887e455b`.
- Web search used `site:uscode.house.gov 42 USC 1983 Civil action for deprivation
  of rights`; `read_authority` read the official page
  `https://uscode.house.gov/us/usc/119-124/t42/s1983/`, `[0,3281)`, saved as
  `339e2022-0a92-42be-a29e-bff1bc0ad974`.
- Those actual passage artifacts reached `analyze_material` and subsequent
  controller calls before the final substantive explanation. No blanket
  all-opinion retrieval occurred.

## Issues observed

1. **Recovered CourtListener timeout.** A lookup of a 188-character explicit
   Miranda research lead timed out after 20 seconds. Its snapshot recorded
   incomplete provider coverage. The controller retried with the bare citation
   and completed. An earlier broad CourtListener search also returned an
   unrelated candidate; it remained a candidate, not an established identity.
2. **False saving statement in generated analysis.** Analysis artifact
   `9bc6db8d-60d3-453a-9aa0-d79b471941ef` claimed no saving tool was available and
   the note was unsaved, although the application had saved it. The controller
   read it and requested a correction; artifact
   `5ba0e689-2be9-44fe-a2fe-da70b5c0cce5` is the corrected note. The initial
   erroneous note remains in history. No application fix was made in this round.
3. **Controller provenance false positive.** During that correction the
   controller also objected to “underlying artifact” IDs. Independent inspection
   showed both IDs existed as source-cache artifacts and were present in the
   writer's supplied passages. They were not fabricated. Distinguishing evidence
   passage IDs, source-cache IDs and file IDs remains a communication weakness.
4. **Cumulative counts need care.** The brief audit's combined record has 13
   saved uses, but 7 belong to its current scope. Narrowed boundary research has
   7 saved records across full/range history, but 3 current appearances. The
   generated answers reported current counts correctly; the saved-results UI
   could make current-scope counts easier to distinguish from cumulative ones.
5. **Browser upload automation permission.** Extension file-chooser uploads
   were unavailable without file-URL access. The native file picker worked; no
   browser permission was changed. Upload and preparation then followed the
   normal application path.

## Independent checks and evidence

- Every inspected saved source span exactly equals its pinned original slice.
  Independent expected-occurrence data checked the brief and boundary cases.
- All 5 uploaded records across 4 fixture files retain their original SHA-256;
  all 3 editor states are unchanged. Earlier overview research snapshots match
  their original saved read observations after later requests.
- Reload restored completed answers and attachments. The rendered code blocks
  retained the wrapped Brown newline. Saved results show latest mode/scope and
  retain older overview examples without relabeling them as audited.
- Ignored local evidence:
  `artifacts/live-case-assistant/authority-discovery-modes-20261110/` contains
  `startup.json`, `validation.json`, `original-integrity.json`, per-workflow
  initial controller inputs/steps/artifacts, screenshots and `llm-trace.jsonl`.
  The trace contains sanitized actual model inputs/outputs for allowlisted
  synthetic chats; credentials and private/encrypted reasoning are omitted.
- Temporary tracing was disabled after testing and the main worker restored to
  the ordinary Celery startup command. The app and workers remain running.

No cancellation, worker-loss injection, historical-law certification or
later-treatment review was performed in this live round. These synthetic cases
are not a comprehensive legal-reference recall benchmark.

## Follow-up: analysis saving and cache provenance fixes

After the two context fixes, the same synthetic chat completed a fresh focused
Brown/§1983 check through the UI, workflow
`wf_c7723565-e2ec-4f04-8de3-d29fe9420778`.

- The actual analysis input explains that the application saves its response.
  Source-read observations now expose the backing cache ID beside the passage ID.
- Steps 5 and 7 read Brown opinion `105221` at `[0,12000)` and the official
  statute page at `[0,3281)`. Both passage/cache relationships reached the
  controller and analysis LLM.
- Step 9 saved analysis `a33f9974-7eaf-4120-97fa-89541d40d21a`. It retained
  both legitimate cache IDs as provenance and made no saving-unavailable claim.
  The controller read it and completed without a correction call or cache-ID
  objection. Only one analysis call was needed.
- Reload restored the final answer and saved analysis. All three uploaded
  originals retain their hashes, and the editor is unchanged.
- The focused controller, authority integration, research, source and chunking
  suites passed: **163 tests** in Conda `lexintel`. The strict tool-schema test
  also passed after the final description wording adjustment.
- Sanitized actual LLM inputs/outputs, steps, artifacts, integrity validation and
  a UI screenshot are under ignored local evidence directory
  `artifacts/live-case-assistant/authority-context-fix-20261110/`.
  Temporary tracing was disabled and the normal worker restored afterward.
