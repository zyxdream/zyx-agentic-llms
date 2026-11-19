# Workspace mock-live replay workflow

These checks exercise application behavior with deterministic external providers. They never call paid model providers or production services.

Live, paid-provider suites are separate and opt-in; see
[the live suite registry](../live/README.md) for the three-workflow legal
benchmark, drafting pack and acceptance fixtures.

## Full backend and runtime replay

Requirements: the project-owned `.venv` (prewarm its tokenizer cache using `.venv/bin/python scripts/cache_tokenizers.py` before offline checks), `redis-server`, Docker, and the execution image described in the root README. The fixture launches and terminates its own Redis process on a random localhost port. Every test gets an independent SQLite database, queue namespace and storage directory. Existing Redis, queues and databases are left untouched.

```sh
ZYX_E2E=1 ZYX_REPLAY_E2E=1 ZYX_PYTHON_LIVE=1 \
  .venv/bin/python -m pytest -q -p tests.offline_guard \
  --junitxml=artifacts/workspace-replay/backend-results.xml
```

`test_workspace_replay.py` runs actual Celery tasks through Redis. Only authentication and external-provider boundaries are mocked. It covers idempotent submission, saved answers, clarification/reload/resume, cancellation while a provider is blocked, cancellation at a question, late-result fencing, explicit retry, completed-step reuse, upload preparation, source reads, candidate drafting/verification/publication, DOCX download, web research and optional legal research. The rest of the retained suite covers source and artifact scope, context compaction, leases, model/tool errors, editor publication races, legal provenance, real container execution, formula recalculation and PDF conversion.

## Browser replay

From `frontend/`:

```sh
npm run test:e2e
```

The Playwright server bundles a separate test entry under `tests/e2e/browser/`. Production `src/index.js` and its independent local authentication remain active in the application build. Test authentication is injected only into the separate replay bundle. Requests outside the local replay origin are blocked; the Workspace API is mocked by the browser fixture.

The browser matrix covers cancellation outcomes, confirmation/reload, retry, connection failures, late responses across conversations/projects, two-tab isolation, staged artifacts, version conflicts and wrong-document responses, uploads and source exclusions, 100/101-file limits, more than 200 status polls, source inspection, downloads and safe output rendering. It also checks that active runs have one status poller rather than repeated conversation metadata reads.

The browser matrix has 54 conversation/interface checks, 24 navigation/layout checks and 18 document-library/attachment checks. These cover folder naming without file upload, separate explicit folder import, creation/rename/name conflicts, deletion/busy responses, lazy lists, stale responses, per-conversation panels, unsaved Output edits, sidebar reflow, keyboard focus and desktop/mobile layouts. Input checks cover Project documents versus prompt attachments, direct file picking, folder review/cancellation/limits, partial transfer failure/retry, late-transfer removal fencing, reload/follow-up persistence and one shared catalog/poller. Former Fast/Deep cases verify minimal copy and model-free requests. Production defaults remain chat-first; older regression cases also exercise restored Attachments preferences. Real-worker replay requests omit model fields to exercise the saved backend default.

Results, traces on failure, screenshots on failure and JSON/HTML reports are written under ignored `artifacts/workspace-replay/`.

## Matched performance comparison

```sh
.venv/bin/python tests/e2e/benchmark.py --baseline d8e334c --repeat 5
```

This extracts the chosen pre-refactor Git commit into a temporary directory and alternates before/after subprocesses. Both execute the same twelve parametrized controller checks with identical model/broker doubles and external networking blocked. It records total process time and test-body duration separately. The baseline is the independent initial snapshot, not the original project checkout. Use an explicitly chosen compatible ref when comparing another change.

This benchmark measures local deterministic runtime overhead. It does not measure live model latency, cloud OCR latency, semantic model quality, production concurrency or service-level guarantees.

See `docs/workspace-replay-results.md` for the current observed results and the old-to-new coverage map.
