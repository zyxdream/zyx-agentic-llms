# Authority research — local live results

Completed on the Monica-agent branch using the normal local app and workers,
real LLM/CourtListener/web calls, Conda `lexintel`, OpenAI SDK `3.26.0`, and the
existing `lexintel-python-analysis:1` image. No test server or deployment.

## Requests

Uploaded `tests/e2e/fixtures/authority-research/research-brief.txt` through the UI
to matter **Authority research — local test 2026-11-09**, chat `257`
(`f4da4b05-f1a5-4ee9-8b7f-d22db9f70eb8`). Used the research and authoring prompts
from `authority-research-live-case.md`, then a link-only revision and a short
source-reopening request. A separate chat (`258`) provided the invoice control.

| Request | Workflow | Result |
|---|---|---|
| Full uploaded-brief research | `wf_b5db8a27-f28b-4c14-80d1-e1978d835171` | Completed; editor unchanged |
| Memo from saved research | `wf_1a1a61b6-ef60-4ae0-ba45-8380b9dd40f6` | Completed; Markdown memo and findings table published |
| CaseViewer link revision | `wf_0dd5fcd6-a68a-46c8-9adf-460da9463b4b` | Completed; only case URL destinations changed |
| Reopen Brown source | `wf_1bba7065-0764-4c9d-b8a8-718d0e52a321` | Completed; both source URLs saved; memo unchanged |
| Ordinary invoice report | `wf_91a1c720-ed5d-44b6-bede-7127ab400079` | Completed without legal lookup/search/reading or verification tools |

## Observations

- Research covered all 1,284 canonical characters and retained six original
  occurrences. Brown's reasoning and disposition uses remained separate; the
  conflicting Miranda caption remained attached to its actual original context.
  Independent checks confirmed the recorded original positions and quotations.
- CourtListener identified the reporter citation as Brown. The agent separately
  investigated the intended Miranda name, retained alternative records, and
  selected relevant opinion text. Missing Placeholder results remained unresolved,
  with coverage limits stated rather than an automatic invalidity verdict.
- The agent read Brown's relevant text, selected Miranda and Neiman passages,
  the official federal and Pennsylvania section pages, and a relevant official
  Pennsylvania act PDF excerpt. It distinguished selected passages from unread
  opinions, other same-name decisions, footnotes and unassessed historical effects.
- The next request's first controller input contained the saved research catalogue.
  Authoring received actual saved passage artifacts, including Brown text
  containing both the holding and concluding order, alongside the generated
  research assessment. It did not rely solely on case metadata or the upload.
- Earlier research snapshots stayed unchanged. A new source-read snapshot carried
  the original CourtListener URL, CaseViewer URL, cluster/opinion identity and
  source locations. The source-reopening request preserved the published memo.
- Preview, Markdown Source, reload and a CaseViewer navigation worked. Brown's
  CaseViewer displayed the expected case identity and opinion. Absolute case and
  original-source links are clickable in hydrated agent chat history.
- All four downloads preserved text, the findings table and all six distinct
  link destinations. The corrected PDF contains 18 link annotations over two
  pages. All DOCX/PDF pages were rendered and visually inspected.
- Downloads added export artifacts without adding agent/research steps. The
  uploaded original and fixture retained SHA-256
  `ad0c493a9324b49ca340d2288a7b93d7ae167d248dc55b5328cd91c67d2aa06b`.

## Bounded fixes exposed by the live test

1. Passage artifacts initially omitted the existing CaseViewer URL, so the first
   memo used CourtListener URLs. Passage metadata now carries both URLs and case
   identity. A follow-up revision used the saved viewer links; an independent
   comparison proved all other Markdown content stayed unchanged. A subsequent
   live source read confirmed the new metadata.
2. LibreOffice 7.4 preserved the visible link styling but omitted PDF annotations.
   Controlled tests isolated the requirement for a defined Word Hyperlink
   character style. Applying that standard style restored all destinations with
   identical PDF text and page count. No image upgrade or converter/security
   change was needed. Renderer-versioned export caching prevents reuse of older
   generated files while preserving those saved artifacts.
3. The shared legacy research Markdown renderer stripped absolute Markdown links
   from agent chat. Agent-marked responses now use the existing isolated,
   sanitized document renderer. Reloading the same saved response confirmed both
   clickable links. Legacy research/guard rendering remains unchanged.

The PDF text QA initially merged adjacent table-cell tokens; layout-aware text
extraction resolved that inspection artifact. The missing PDF annotations were
independently confirmed and fixed, rather than waived by the QA change.

## Automated checks and evidence

- Final selected backend suite: **392 passed, 23 skipped**, including the real
  offline Docker PDF hyperlink regression. Existing deprecation warnings remain.
- Focused frontend suite: **70 passed across eight suites**, including saved
  research, source views, chat links, history hydration and legacy research.
- SQL-backed tests cover interrupted lookup checkpoints, failed-range retry,
  ownership and same-chat continuation. Provider-boundary tests cover cancellation,
  limits, redirects, PDF extraction gaps and selective opinion reading.
- `git diff --check` passed.

Ignored local evidence is under
`artifacts/live-case-assistant/authority-research-20261109/`: workflow audits,
screenshots, saved downloads, source hashes, converter diagnosis and
`qa/live-checks.json` / `qa/download-inspection-final.json`.

No manual cancellation during a live network read was performed in this round.
Historical-version certification, exhaustive later-treatment review and a
comprehensive independent quotation/proposition verifier remain outside scope.
