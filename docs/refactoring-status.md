# Refactoring scope and remaining work

## Current status (2026-11-18)

Application commit `99dcae3` is the current fixed-code baseline. The host runtime
is Python 3.11. Model choices are role-based in `config/models.json`: preparation
and utility work use `gpt-6-luna`, while controller, analysis, writer and verifier
use `gpt-6.1-sol` medium. Automatic dependency version PRs are disabled; updates
follow the quarterly batches and compatibility gate in
[dependency updates](dependency-updates.md). The live three-workflow legal
benchmark completed 10/10 turns with zero context-budget errors and zero
whole-run retries; the S1 drafting request that previously aborted now resolves
duplicate research snapshots before dispatch. Recorded suites and baselines are in
[the live registry](../tests/live/README.md); setup and operation are in the
[runbook](runbook.md); the agent design is in [agent harness](agent-harness.md).

The sections below are the historical refactor record. Their test counts and
"not yet run" statements describe the state at the time of that pass, not the
current tree.

Local Git history was rewritten on 2026-11-18, before the first push, to remove
private case documents and legacy branded assets. Commit hashes in older notes
were remapped to the rewritten history. An ignored backup bundle of the
pre-rewrite history is kept under `instance/backups/`; do not push it.

## Included in this implementation

| Area | Result |
| --- | --- |
| Local environment | One `dev-monica` public configuration, private root `.env`, app-owned SQLite baseline, namespaced Redis queues and results, private storage paths. Dedicated Redis process/port, independent local accounts, own Python environment, private copied provider/OCR credentials; no original-project services reused. |
| Agent design | Original LLM-controlled sequential decision/tool loop retained in `agent_tasks.py` and `ai/agent_tools.py`, with SQL leases, saved steps/artifacts, context compaction, publication guards, cancellation and explicit retry. |
| Helpers | Required `everything.py` state/publication/brief helpers moved to `workspace/state.py`; uploaded-original revision helpers moved to `documents/revisions.py`; role model settings moved to `ai/model_settings.py`. `everything.py` removed. |
| Celery | Agent tasks, document preparation and brief updates retained. Version-0 planning/extraction chains, chords, work rows and execution-version selectors removed. Beat reconciles pending deliveries. |
| Model boundary | Provider protocol, capability declaration and normalized decision/checkpoint result added. Existing OpenAI transport, retries, structured-output and reasoning replay retained. Only the existing provider is supported in this pass. |
| Legal expertise | Research, sources, citation handling and court formatting grouped in `domains/legal`. Legal tools and specialist instructions can be disabled. Source links open official/provider sources instead of an application CaseViewer. |
| Core capabilities | Conversation, uploads, source preparation/retrieval, web research, Python/spreadsheets, analysis, drafting/revision, verification, clarification, saved results and exports retained. |
| Frontend | Minimal purple/pink Workspace shell. Project/conversation selection, scope-aware uploads, preparation status/exclusions, original-text inspection, progress, clarification, cancel/retry, safe result rendering, saved artifacts, Markdown editing with version conflicts, downloads. |
| Removed products | Guided drafters, standalone Smart Search/Doc Search, standalone Hallucination Guard, reviewer, CaseViewer, Word add-in, voice/practice, business dashboards, marketing/tenant assets and old deployment/release automation. Agent source retrieval, citation verification and DOCX exports remain. |
| Database | Independent initial migration for the retained tables. Canonical documents and ordered conversation turns remain separate from auxiliary JSON state. Operational usage counters retained; guided/Hallucination Guard metrics removed. |
| Regression coverage | Retained active agent/ingestion/legal/export/storage checks, new Workspace API and migration checks, frontend component and shell behavior checks. |

Removed files were archived outside the project during implementation. The original source project and its databases/uploads were left untouched. Workspace now uses its own environment, credentials, local accounts and storage; previous private settings were archived without being loaded. No commits or pushes were made for this refactor.

## Deliberately deferred

1. **Second provider and full cross-provider proof.** The runtime now has an adapter boundary, but checkpoint formats, structured helpers, context budgets and capability negotiation must be exercised with a second real adapter before claiming model independence.
2. **Dynamic skills, tool installation and MCP.** This pass creates module boundaries and an explicit registry; it does not implement plugin discovery, skill packaging, installation or external tool permission policies.
3. **New domain packages.** Healthcare and insurance need their own instructions, tools, data sources and evaluation tasks. No domain quality claims follow from the architecture alone.
4. **Production operations.** Hosting, TLS, secret management, database sizing/multi-host suitability, backups, monitoring, rate limits, deployment automation and service-level testing need a deployment-specific plan. Existing task limits and Docker isolation were preserved, not certified as sufficient for every deployment.
5. **Visual/product work.** Finished identity/logo, richer document editing, collaboration, responsive interaction polish and onboarding remain future work. This is the basic working shell.
6. **Optional helper cleanup.** Older shared document/OCR utilities and their dependencies remain where removing them could endanger retained behavior. They do not restore the removed product routes or fixed pipelines.
7. **Live acceptance.** Mocked browser workflows and real isolated Redis/Celery/Docker replays are now covered. Paid-provider semantic evaluation, cloud OCR smoke testing and production load testing remain separate work. Local authentication and a real frontend/backend first-run browser smoke check are covered by the isolation follow-up.

## Preservation checks

The important contracts are saving a request before dispatch, immutable source versions, one active run per conversation, lease-based ownership of worker writes, separate conversation/document identity, version-checked editor saves, atomic draft/turn publication, durable clarification, and cancellation that fences late workers. Tests exercise these contracts using real SQLite transactions and mocked external boundaries.

## Initial refactor verification (superseded by the replay results below)

- Backend: **442 passed, 7 skipped** with `ZYX_E2E=1` and the external-network guard.
- Frontend: **83 passed** across seven suites.
- Frontend build: passed with `CI=true`.
- Independent baseline: upgrade, metadata comparison and downgrade passed against a temporary database. The new app-owned `instance/zyx-agentic-llms.db` was initialized separately.
- Startup: health endpoint responded and only six application Celery tasks registered (agent advancement, document preparation/reconciliation, run reconciliation/failure handling, and conversation brief updates).
- Source audit: no version-0 selectors/work models or chain/chord orchestration remain in backend code; no unresolved Python global references were found; `git diff --check` passed.

At that stage, Docker, cloud OCR, a live model session and remote CI had not been exercised. The follow-up verification now includes real Docker and Redis/Celery execution, 457 backend passes with no skips, 83 component passes, and 54 browser workflows passing twice. See [the current replay report](workspace-replay-results.md). Paid-provider semantic evaluation, cloud OCR and remote CI remain unrun. The working tree remains uncommitted for review.

## Local isolation follow-up

The previous shared-service development instructions have been replaced. Workspace uses frontend 3010, backend 5010, dedicated Redis 6390 and its own account database; the original application remains unchanged. The complete matrix now passes 469 backend, 86 frontend component and 54 browser tests, plus a real first-run/reload smoke check. Local authentication and isolation regressions are included. See [local isolation details and verification](local-isolation.md).

## Draft continuity (4A)

Implemented: one persistent working draft, rejection of older editing/publication
bases, explicit confirmed-version rollback through chat, and atomic confirmed
snapshots on manual save/final publication. Existing candidates are retained.
[Behavior and validation](draft-versions.md).

## Workspace editing (4B)

Implemented: focused text replacements within the existing revision call, full
rewrites for broad transformations, all-or-nothing patch validation, metadata
preservation and no-op revision handling. Full candidates and 4A continuity remain.
[Behavior, validation and remaining work](draft-edits.md).

The subsequent general drafting update unifies uploaded-first editing with 4B.
Deferred: native Word-layout editing, fuzzy matching, automatic long-document
batching and semantic verification. No new version-history UI is included.

## General drafting and material purposes

Implemented: per-operation material purposes, controller guidance to assess actual
results against the user request, compaction continuity, and shared uploaded-first /
Workspace patch/rewrite editing. Existing tools and optional verification remain;
no task-specific writing workflows or semantic publication gate are introduced.
[Behavior, interfaces and validation](general-drafting.md).
