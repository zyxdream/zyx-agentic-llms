# Workspace mock-live verification

## Current baseline (2026-11-18)

At the current role-policy baseline, the offline backend suite recorded **962 passed, 20 skipped,
4 known pre-existing failures**; the four failures are unrelated to the current
work and are named in the [runbook](runbook.md#10-verification). Re-run the
commands rather than assuming these counts. The sections below record earlier
replay and isolation passes and their counts at the time.

The former Case Assistant browser suite was archived during the slim-down. It has now been adapted into the current Workspace API and shell, with a persistent test-only runner. Production authentication has no test-mode bypass.

## Latest isolation follow-up

After giving Workspace independent services, credentials and local authentication, the full matrix passed again: **469 backend tests, zero skipped; 86 frontend component tests; 54 browser workflows; production build passed**. A real frontend/backend first-run and reload smoke check also passed, and restarting Workspace left the other application running with unchanged listener PIDs. See [local isolation verification](local-isolation.md). The matched performance measurements below describe the earlier slim-down comparison; they were not rerun for the authentication/service-isolation change.

## Initial replay results

| Check | Result |
| --- | --- |
| Complete backend suite, with Redis/Celery and Docker enabled | **457 passed, 0 skipped** |
| Frontend component suite | **83 passed** |
| Mocked Chromium browser workflows | **54 distinct scenarios, repeated twice: 108 passed**, no skips or flaky outcomes |
| Production frontend build | Passed; test entry/authentication boundaries are absent from the production bundle |
| Matched backend replay benchmark | Five before/after pairs, twelve checks per run, all passed |
| File and workflow checks | Python syntax, CI YAML parsing and `git diff --check` passed |

The complete backend run took 95.68 seconds. The repeated browser matrix took 138.65 seconds. These are verification-run durations, not application latency measurements.

## What the checks prove

- **Browser replays:** real React components and Chromium, deterministic Workspace API responses, controlled delays/errors and downloads. These cover UI state, reload/navigation races and request contracts.
- **Worker replays:** real Flask routes, SQL transactions and Redis-backed Celery execution. Only identity and external-provider boundaries are mocked. Each run uses owned temporary infrastructure.
- **Docker checks:** actual restricted Python execution, spreadsheet analysis/recalculation, failure recovery, generated-file downloads and document-to-PDF conversion.
- **Unit and integration regressions:** the complete retained suite for the controller, source preparation/retrieval, context compaction, legal research/provenance, canonical document/turn persistence, ownership and atomic publication.

These deterministic checks cannot evaluate the reasoning quality of a real model or establish production throughput or a service-level guarantee. No paid provider, cloud OCR request, original-project database, existing Redis namespace or remote deployment was used.

## Original browser coverage mapped to Workspace

| Earlier contract | Current coverage |
| --- | --- |
| Selected-chat controls; returning to a run | Chat switching, reload and active-run restoration. |
| Late preflight/submission/confirmation/Stop/Retry/poll responses | Delayed request cases across conversations, project changes and newer runs in the same conversation. |
| Document persistence during completion and reload | Canonical backend publication interleavings plus explicit editor saves and answer restoration in the browser. |
| Conflicts, wrong-document responses, late editor replies | Unsaved Markdown retention, response identity checks and scope guards. |
| Queued autosaves and acknowledged versions | Replaced by explicit Save with disabled duplicate saves and version-checked subsequent saves. The old autosave interaction is no longer part of the shell. |
| Matter/project and multi-tab isolation | Empty/populated project switches, late metadata, independent tab selections and cross-tab source discovery. |
| Three-file upload, clarification and Word download | Upload readiness → saved question → reload → answer → publication → DOCX download. |
| Unnamed draft bodies and staged candidates | Flat Markdown/table preview across reload; candidates remain separate until publication. Old collapsible-section layout assertions are superseded. |
| Connection failures and durable extraction progress | Saved agent progress and result artifacts across reconnect/reload. Version-0 fixed extraction/chord behavior remains intentionally removed. |
| Actual cancellation/completion/failure outcomes | Cancel outcome matrix, confirmation cancellation, failed cancellation retry and late-result fencing. |
| Retry after reload; historical failures displayed once | Same-run retry with no duplicate submission, completed-step reuse, ordered restored history. |
| 13-file restoration, grouped transfer and 100/101 limits | Source-card readiness, staggered summaries, batched transfer and admission limits. Chip colors are replaced by explicit preparation status. |
| Failed/partial source inclusion, prompt exclusions | Blocking while unavailable, ready partial coverage without approval, per-prompt exclusions, recovery and earlier-file context. |
| Attachments restored after delayed/failing reads | Scope-aware restoration, reconnect, first prompt in a new conversation and delayed acceptance preserving another conversation's exclusions. |
| Fast/Deep and long-running polling | The chooser is removed. Browser checks now verify backend-owned selection; more than 200 polls, server timeout and one active status poller remain covered. Legacy policy compatibility is checked on the backend. The removed Doc Search product is not restored. |
| Saved evidence and file downloads | Source summaries/original text, staged document artifacts and generated Python files. |

## Issues exposed and corrected

1. A delayed preflight could submit after the user switched conversations. Admission and returned results now validate a navigation epoch and current control request.
2. Pending actions and late control responses could affect unrelated conversations or newer runs. Busy state and action results are scoped; cancellation remains available while an accepted run's transcript read is delayed.
3. Document saves accepted response identities from another document. The editor now checks both document and conversation identity and preserves unsaved text on mismatch.
4. Two tabs shared navigation choices through local storage. Navigation choices now use session storage per tab.
5. Active runs had both conversation and run polling loops. Only the run status loop remains; terminal status refreshes the conversation once.
6. The HTML preview sanitizer used a live detached element, allowing image error handlers to fire before removal. Sanitization now parses into an inert template before insertion. Chromium checks verify that hostile payloads do not execute.
7. Earlier files were being marked newly attached on every assignment, and prompt exclusions were not cleared after acceptance. Earlier files remain in `chatFiles`; only uncommitted files are marked as new attachments, and exclusions apply to the accepted prompt.
8. Starting the in-process Celery test worker changed the test process's logging level. The fixture now restores it, so combining suites preserves warning-diagnostic assertions.

## Performance comparison

Baseline: independent initial commit `d8e334c`, copied to a temporary checkout. Five alternating before/after repetitions each ran the same twelve parametrized controller checks with model and broker doubles. Median timing:

| Measure | Before slim-down | After slim-down | Change |
| --- | ---: | ---: | ---: |
| Whole replay process, including imports/setup | 7.711 s | 7.101 s | 7.9% faster |
| Test-body execution | 3.637 s | 3.217 s | 11.5% faster |

No slowdown was detected in this controlled sample. This comparison excludes model/network latency and production concurrency. Browser checks separately assert that conversation metadata is not repeatedly fetched during active-run polling. Cancellation is measured while the mocked provider is deliberately blocked, to verify that the cancel API does not wait for that call. The local cancel responses took **7.24 ms in flight** and **6.56 ms at clarification**; both are local test-client measurements, not Internet latency claims.

The saved benchmark command was also verified with three additional before/after pairs; all twelve checks passed in every run, with 8.4% lower median whole-process time. Raw samples for that runner check are in `artifacts/workspace-replay/benchmark-runner-check/`.

The original five-pair benchmark samples are saved under `artifacts/workspace-replay/performance/`. Reproduce them with `python tests/e2e/benchmark.py --baseline d8e334c --repeat 5` in the project Python environment.

## Runtime and scope notes

The local execution image was created by copying the current runtime code onto the already-installed local execution image, without network access. A clean rebuild from the Debian Dockerfile could not complete with networking disabled because its package layer was not cached. Runtime behavior was tested; a clean image build will be exercised by the configured CI workflow when it is run.

The browser runner, backend commands, artifact locations and isolation details are documented in `tests/e2e/README.md`. GitHub Actions is configured to run the full backend/worker/Docker matrix and mocked browser suite; it has not been run remotely because no code was pushed.

## Project documents and attachments follow-up

The current input/output separation passes **501 backend tests**, including the real isolated Redis/Celery and Docker checks, **108 component tests**, **96 browser workflows**, and the production build. The browser count includes 18 new document-library/attachment cases. Existing cancel, reload, clarification, retry, version conflicts, downloads, polling and stale-response checks remain covered. See [the behavior and verification details](project-documents-and-attachments.md).
