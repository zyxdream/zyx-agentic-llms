# Live evaluation suites

This is the index for every opt-in evaluation that runs the real Workspace
application against real model, research and preparation providers. It records
which suites exist, how to run each one, and the frozen run that later results
should be compared against.

These suites are **not** part of CI and are separate from the deterministic
checks in `tests/` and `tests/e2e/`. Deterministic tests mock providers and use
temporary databases, Redis namespaces and storage. Live suites use the normal
signed-in Workspace account, the real local services and paid provider calls.

| | Deterministic checks | Live suites |
| --- | --- | --- |
| Providers | Mocked or local doubles | Real, paid |
| Database | Temporary per test | `instance/zyx-agentic-llms.db` |
| Redis | Owned temporary port/namespace | `127.0.0.1:6390` |
| Browser | Separate replay entry | Normally authenticated browser |
| Purpose | Regression proof | Quality, latency and whole-system comparison |
| Command | `pytest` / `npm test` | Scripted browser or staged manual page |

## Before running anything

1. Read the suite's own README. Each one lists its inputs, evaluator references
   and what must never be uploaded.
2. Confirm the owned services: frontend `http://localhost:3010`, API
   `http://127.0.0.1:5010`, Redis `127.0.0.1:6390`. See the
   [runbook](../../docs/runbook.md).
3. Use the normal signed-in account. Do not export cookies, tokens or browser
   authentication, and do not add an authentication bypass.
4. Expect paid provider calls. Preparing or staging a pack makes no model call;
   submitting a prompt does.
5. Keep evaluator keys, reviewer checks, manifests and reports out of every
   upload and prompt.
6. Never edit or delete a frozen run directory. Failed and partial attempts are
   evidence and are retained.

## Suites

### 1. Legal workflows v2 — the three-workflow benchmark

Pack: `tests/live/model-benchmark/`
Entry: scripted Playwright/CDP against the signed-in browser.
Inputs: four original PDFs (106 pages) plus one correction letter held out from
uploads.

| Workflow | Turns | Turn IDs | What it tests |
| --- | ---: | --- | --- |
| Waymo | 4 | W1–W4 | Compare original/amended complaints; provisional responses; focused edit; verify |
| Sullivan | 3 | S1–S3 | Review five motion paragraphs; investigate authorities; revise; explain corrections |
| Coomer | 3 | C1–C3 | Review opposition passages; resolve citations; propose replacements; filing handoff |

This is the canonical three-workflow set. Run all ten turns unless a workflow is
being retried after a documented failure.

```sh
.venv/bin/python tests/live/model-benchmark/prepare.py --validate-only
.venv/bin/python tests/live/model-benchmark/prepare.py --label openai-v2 --cdp-port 64954
node tests/live/model-benchmark/browser.cjs /absolute/path/to/new-run run
.venv/bin/python tests/live/model-benchmark/collect.py /absolute/path/to/new-run
.venv/bin/python tests/live/model-benchmark/report.py /absolute/path/to/new-run
```

`prepare.py` freezes the pack, prompts, input hashes, runtime configuration and
application source identity without contacting a provider. `browser.cjs` accepts
an optional final `waymo`, `sullivan` or `coomer` argument to run one workflow.
`collect.py` exports scoped read-only evidence and checks DOCX integrity.
`report.py` writes `REPORT.md` from recorded metrics; semantic findings require a
manual `quality-review.json`.

### 2. Waymo / Uber drafting evaluation

Pack: `tests/live/drafting/waymo-uber/`
Entry: manual browser through the normally signed-in account.
Inputs: four original PDFs (110 pages) plus one synthetic facts file.

Seven scenarios across ten turns: multi-document change brief and audience
rewrite, source-aligned response matrix, style transfer, new structured document,
conditional responses without invented facts, first-upload edit with follow-up and
rollback, and an optional mixed text/image case.

Follow `tests/live/drafting/waymo-uber/README.md` for the per-case upload and
prompt order. This pack has no scripted runner; it is a controlled manual
evaluation. It does not currently have a matched baseline on the post-refactor
code.

### 3. Live legal acceptance

Pack: `tests/live/legal/`
Entry: staged page plus browser, with the scoring key never served.
Inputs: 46 synthetic files across three matters (40-file commercial reconciliation,
employment overtime workbook, diversity-jurisdiction research).

```sh
.venv/bin/python tests/live/legal/stage.py --live
```

Open the printed `http://localhost:3010/_live_acceptance_legal/` URL, sign in
normally, and start a case. Staging copies only the declared upload inputs and
prompts; `fixtures/expected.json` stays out of the browser. Remove the generated
`frontend/public/_live_acceptance_legal/` directory afterwards.

## Recorded baselines

Run evidence lives under the gitignored `artifacts/` directory. The paths, run
IDs and source identity below are the durable record, so a later run can be
compared without keeping the artifacts in Git.

| Suite | Baseline run directory | Application identity | Configuration | Result |
| --- | --- | --- | --- | --- |
| Legal workflows v2 | `artifacts/model-benchmark-openai-budget-recovery-r2-20261118-202204` | commit `4d42f56` (source hash matches; `code-identity.json` records the pre-rewrite head `4a15ebe`, now `e9da487`, because the run started before the commit) | openai `gpt-6-luna`, medium effort, fast default | 10/10 turns; 3180.594 s; 275 LLM calls; 26,926,920 tokens; 12 tool errors; 0 run retries; 10/10 downloads verified |
| Waymo / Uber drafting | `artifacts/live-drafting-waymo-20261114-165411` | `66f6274` plus then-uncommitted changes; source-tree ID `bbdeca3b…` | openai `gpt-6-luna`, medium | 7/7 scenarios, 10/10 turns; one recovered Python tool error; all outputs and DOCX downloads verified |
| Live legal acceptance | `artifacts/live-legal-20261113` | `f4e933b` plus a then-uncommitted transport fix | openai `gpt-6-luna`, medium | 3 matters plus one revision; 4/4 runs completed after the transport fix; 44 files uploaded |

Baseline turn IDs in the r2 run (projects 38, 39 and 40):

| Turn | Run ID | Turn | Run ID |
| --- | --- | --- | --- |
| W1 | `wf_ea9b0be1-99c5-46dc-9d2c-1ce08e8dcc3a` | S1 | `wf_e4a919b7-f9d5-4be3-98ff-7864c94f0984` |
| W2 | `wf_afd159e0-a3b4-43e9-8140-8cb20ea8c3ba` | S2 | `wf_c77f83b8-9c16-44cb-bb1c-acd977da89a7` |
| W3 | `wf_152ab62f-a6f6-446f-8051-49c6aeca9960` | S3 | `wf_1c233c5d-ccb2-424d-8964-6f8a766cb12a` |
| W4 | `wf_11700318-635c-4149-9391-e0c33faa3f12` | C1 | `wf_4f382390-6c7f-4f27-a9ce-a3de2e95da0d` |
|  |  | C2 | `wf_96142d97-f378-47ec-beb9-87fb7f8248c2` |
|  |  | C3 | `wf_5e38d14a-a0bb-4541-9b83-4681a86e1996` |

The saved conversation IDs and per-turn evidence are in that run's `state.json`
and `results/evidence.json`. Reusing the same prompts and inputs in a new project
is the correct comparison; do not append to a frozen conversation.

Earlier v2 benchmark runs remain valid historical evidence but are not the
current baseline:

| Run directory | State | Why it matters |
| --- | --- | --- |
| `artifacts/model-benchmark-openai-v2-20261117-083206` | 10/10 turns, 24 tool errors, 3463.076 s, commit `449e754` | First v2 baseline before the completion-validation and material changes |
| `artifacts/model-benchmark-openai-condition-strength-20261118-131407` | 7/8 turns, 9 tool errors, 1642.893 s | Motivating failure: Sullivan `create_draft` exceeded the context budget and stopped the run |
| `artifacts/model-benchmark-openai-condition-strength-sullivan-retry-20261118-135111` | 3/3 Sullivan turns, 7 tool errors, 1532.021 s | Retry of the failed workflow before the recovery change |
| `artifacts/model-benchmark-openai-sullivan-coomer-page-corrections-20261117-153455` | 6/6 turns, 13 tool errors, 2680.999 s | Page-location correction pass |

## Other recorded live evidence

These are focused validations, not complete benchmark suites. Use them to find
the recorded behavior for a specific change; do not present them as a full
quality baseline.

| Artifact directory | Date | Scope |
| --- | --- | --- |
| `artifacts/live-material-purposes-20261114-203749` | 2026-11-14 | Repeated material purposes for one reference; live Cedar request |
| `artifacts/live-reliability-fixes-20261115-125603` | 2026-11-15 | Blocked research pages, Python result normalization, workbook checks, text fitting |
| `artifacts/live-spreadsheets-rerun-20261114-205359` | 2026-11-14 | Spreadsheet execution rerun |
| `artifacts/pinpoint-live-20261116-215720` | 2026-11-16 | Conservative citation and pinpoint reporting, three fresh projects |
| `artifacts/model-benchmark-citation-accounting-*` | 2026-11-16 | Citation-review accounting variants |
| `artifacts/model-benchmark-court-metadata-20261116-120919` | 2026-11-16 | Court metadata handling |
| `artifacts/model-benchmark-navigation-all-20261116-144028` | 2026-11-16 | Source navigation |
| `artifacts/model-benchmark-page-locations-20261116-105017` | 2026-11-16 | Page-location behavior |
| `artifacts/model-benchmark-completion-ids-coomer-20261117-101818` | 2026-11-17 | Completion ID validation |
| `artifacts/model-benchmark-openai-latest-review-word-count-20261118-103618` | 2026-11-18 | Word-count and review behavior |

## Recording a new run

Every run directory must be self-describing. `prepare.py` already writes
`code-identity.json`, `runtime-configuration.json`, `scenarios.json`,
`controlled-browser.json` and `state.json`; collection adds `metrics.csv`,
`metrics.json`, `results/`, `outputs/`, `confirmed-versions.json` and
`download-verification.json`. Add the following when the run finishes:

1. `REPORT.md` from `report.py` (recorded metrics and errors).
2. `quality-review.json` with the manual review, even when the verdict is
   partial. The report does not inherit another run's conclusions.
3. `RUN-NOTES.md` with anything the generated files do not capture: environment,
   harness changes, operator pauses, exposure to held-out material, and the
   exact source identity if it differs from `HEAD`.
4. The commit and application source hash. If the working tree was dirty, record
   both the commit and the source-tree identifier; do not claim the commit alone.
5. The provider, controller model, effort, preparation model and any helper model
   changes.
6. Every failed or partial attempt in a separate directory. Never resume a
   directory that another run already used.

Use a descriptive label and timestamp, for example
`--label openai-budget-recovery-r2`, which produces
`artifacts/model-benchmark-openai-budget-recovery-r2-<timestamp>`.

### Metadata to compare

| Field | Source |
| --- | --- |
| Application identity | `code-identity.json` → `head`, `application_source_sha256` |
| Runtime policy | `runtime-configuration.json` |
| Pack and prompt hashes | `scenarios.json`, `code-identity.json` → `runner_hashes` |
| Per-turn time, calls, tokens | `metrics.csv` / `metrics.json` |
| Tool errors and retries | `metrics.json`, `results/evidence.json` |
| Output and download integrity | `download-verification.json`, `confirmed-versions.json` |
| Substantive quality | `quality-review.json` |

## Comparison rules

- Hold the pack version, input hashes, prompt hashes, tool availability and
  runtime configuration constant. A changed helper model makes it a
  whole-system comparison, not a controller comparison.
- Compare quality before speed. Completion and download integrity are not a
  quality pass.
- A lookup, search result or model summary is not a verified quotation. Record
  what was read, what was established and what remains unchecked.
- An authority that could not be located is unresolved, not invalid.
- Live web coverage changes between runs. Record the exposure and do not claim
  independent discovery for material the agent found publicly.
- One pass is an observation, not a performance distribution.
- Token totals include repeated context and are not billing figures.

## Related documentation

- [Legal workflow benchmark pack](model-benchmark/README.md)
- [Waymo/Uber drafting pack](drafting/waymo-uber/README.md)
- [Live legal acceptance pack](legal/README.md)
- [Runbook](../../docs/runbook.md)
- [Agent harness](../../docs/agent-harness.md)
