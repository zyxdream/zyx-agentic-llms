# End-to-end test infrastructure

E2E runs are opt-in and must use a dedicated Celery queue. The application and
worker should be started with the same `E2E_*` values.

## Local Redis and SQLite

```bash
LEXINTEL_E2E=1 \
E2E_REDIS_URL=redis://localhost:6379/15 \
E2E_DATABASE_URL=sqlite:///e2e.db \
E2E_CELERY_QUEUE=lexintel-e2e \
python -m backend.e2e_worker --pool=solo --loglevel=INFO
```

Run the Flask process with the same variables. The queue name is intentionally
not `celery` or `default`, so an E2E worker cannot consume normal application
jobs by mistake.

The E2E worker entry point supplies the queue itself and rejects `-Q`/`--queues`
overrides, so the worker cannot be pointed at a normal application queue by
accident.

## AWS Redis and MySQL

Set `E2E_REDIS_URL` to the isolated AWS Redis endpoint and `E2E_DATABASE_URL`
to the E2E MySQL connection string. Use a unique queue such as
`lexintel-e2e-${RUN_ID}` for each run. Start the worker through
`python -m backend.e2e_worker`; it reads the same queue variable and supplies
the queue to Celery. The Redis endpoint must be a dedicated logical database or
instance for E2E data; a queue name alone does not isolate the application’s
direct Redis keys.

The replay-backed complaint matrix in `test_complaint_workflow.py` currently
covers the Northern District of California and Southern District of New York
fixtures. Both generated formats are checked through the shared PDF and Word
validators before the artifacts are collected.

## Shared test helpers

`tests/e2e/helpers.py` provides the common workflow plumbing:

```python
import os

from tests.e2e.helpers import ArtifactCollector, E2EClient

client = E2EClient(
    base_url="http://localhost:5000",
    access_token=os.environ["E2E_ACCESS_TOKEN"],
)
project = client.create_project("Complaint E2E")
draft = client.create_draft(project.public_id, "Complaint", draft_type=1)
result = client.poll_task(task_id)
result.require_success()

artifacts = ArtifactCollector(test_artifact_directory).collect_draft_artifacts(
    client,
    draft.public_id,
    prefix="complaint",
)
```

Use `E2EClient(flask_client=app.test_client(), access_token="...")` for
in-process tests. `poll_task()` retains state and progress history, and
`E2ETaskFailure` exposes a structured diagnostic that can be saved with
`ArtifactCollector.collect_task_failure()`.

## Browser complaint tests

The Playwright suite runs the real complaint drafter UI against deterministic
browser mocks. It covers both the Northern District of California and Southern
District of New York fixtures, including venue selection, all drafting steps,
and the Word download.

```bash
cd frontend/lex-intel
npm install
npx playwright install chromium
npm run test:e2e
```

These tests use `REACT_APP_E2E=1` only in the test server. That opt-in mode
bypasses Cognito, shortens task polling, and does not affect production builds.

The Case Assistant browser test covers the three-document workflow: attachment
upload readiness, prompt submission, an optional workflow confirmation, task
completion, and Word download.

The Answer browser test uses `fixtures/sample-complaint-gawker.pdf` to exercise
the complete `/AnswerDrafter/:draftId` workflow: complaint upload and parsing,
answer selections, affirmative defenses, counterclaim instructions,
introduction, hallucination review, feedback, and Word download.

The RFP browser test reuses the same Gawker complaint to exercise the complete
`/RfpDrafter/:draftId` workflow: complaint upload and parsing, request
generation and reduction, request editing, supporting sections, feedback, and
Word download.

The RFA browser test reuses the same Gawker complaint to exercise the complete
`/RfaDrafter/:draftId` workflow: complaint upload and parsing, admission
generation and reduction, admission editing, supporting sections, feedback,
and Word download.

The ROG browser test reuses the same Gawker complaint to exercise the complete
`/RogDrafter/:draftId` workflow: complaint upload and parsing, interrogatory
generation and reduction, interrogatory editing, supporting sections,
feedback, and Word download.

The RFP objections browser test uses `fixtures/sample-rfp-uber.pdf` to exercise
the complete `/ObjectionsDrafter/:draftId` workflow: RFP upload and parsing,
general and definition objections, request-specific responses, feedback, and
Word download.

## Live Case Assistant browser use cases

The original four live cases and exact prompts/files are recorded locally in
`artifacts/live-case-assistant/manual-tests-20261028/MANUAL_TESTS.md`.
[Case 5: verification-driven correction](scenarios/case-assistant-agent.md)
adds a tracked specification and synthetic fixture for testing whether actual
verification findings inform revision. These live cases use the real backend,
worker, and LLM in dedicated test chats; they are separate from the deterministic
API mocks used by the Playwright suite above. Mocked progress verifies the UI,
not real agent decisions or source fidelity.

[Cases 6–7: clarification memory and verification provenance](scenarios/agent-foundation-live-cases.md)
add two synthetic live user cases for requirements carried across assignments,
chat-only analysis, exact wording, and a report that stays tied to its checked
candidate after an optional unchecked wording revision. The specification
separates expected behavior, observed evidence, and measured performance.

[Cases 6–7 live results, November 6, 2026](scenarios/agent-foundation-live-results-2026-11-06.md)
records both cases' expected-versus-actual outcomes, exact workflow/artifact
references, timings, and remaining generated-status wording issues. Core
behaviors were achieved with zero operation errors; coverage limits are explicit.

[Verification/display fix retest, November 7, 2026](scenarios/verification-display-fix-results-2026-11-07.md)
records the focused prompt and unnamed-section fixes, regression results, and
a fresh live case 7 repeat with findings-only prose and immediately visible body.

[Case 8: conflicting order records and internal reconciliation](scenarios/multistep-order-review-live-case.md)
adds six short fictional records for audience clarification, an uploaded-draft
audit, saved analysis, conditional-credit arithmetic, conflicting delivery
evidence, a small financial table, and a checked final memo using current tools.

[Case 8 live results, November 7, 2026](scenarios/multistep-order-review-live-results-2026-11-07.md)
records the completed six-file reconciliation, three candidates, four verification
reports, observed self-corrections, table/reload checks, timing, and cumulative usage.

[November 2, 2026 live run results](scenarios/case-assistant-agent-2026-11-02-results.md)
records workflow IDs, timings, source/correction checks, observed issues, and
limitations. All five cases passed their browser and source/result checks;
the report also records the recoverable errors that prompted the fixes below.

[Tool-contract fix retest, November 2, 2026](scenarios/agent-tool-contract-fix-results-2026-11-02.md)
records the ID/provenance, candidate-status, and step-label fixes, automated
checks, and follow-up live outcomes separately from the original five-case run.

## Live semantic evaluation

`.github/workflows/live_semantic_evaluation.yml` runs alongside the other test
workflows for pushes to `main` and pull requests targeting `main`, and can also
be started with **Run workflow**. It requires the repository Actions secret
`OPENAI_API_KEY`. The job starts an isolated Redis-backed Celery worker, then
generates the complaint matrix's PDF and Word exports and asks the configured
OpenAI model to evaluate semantic fidelity against the fixture facts.

On failure, the uploaded artifact contains `semantic-evaluation.json`, the
generated PDFs and Word documents, rendered PDF page screenshots, per-case
`resolver-metadata.json`, and `celery.log`. The live runner never uses the
normal `celery` queue.
