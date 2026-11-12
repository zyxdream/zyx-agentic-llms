# Production document preparation and scoped retrieval

Implemented on `codex/document-preparation`. Operational cutover and production
validation are described in [the runbook](DOCUMENT_PREPARATION_RUNBOOK.md).

- Required durable preparation for Case Assistant and Case Background; no disabled
  mode or legacy upload fallback. Existing endpoints delegate to the same service.
- Attachment-specific parsing retains readable source blocks and locations and
  records original extraction gaps independently of summary coverage.
- Luna/medium prepares source-grounded orientation, larger coherent sections,
  structured section records, detailed document digests and compact file cards.
  Every readable range is processed; underlying source evidence remains stored.
- Immutable artifacts and resumable work checkpoints live outside chat JSON.
  Polling returns compact cards and coverage counts; details load on expansion.
- Snapshot v2 freezes authorized document/artifact versions and accepted coverage
  gaps. Backend/UI enforce readiness; acknowledgments are bound to user/chat/version.
- Planner cards, source/section search, selective detail review and exact original
  excerpts connect preparation to query-time relevance. Exhaustive tasks visit
  every requested section; oversized answer evidence uses bounded aggregation.
- Known textual model requests receive context preflight checks. Exact-source
  edits cannot silently substitute sampled source text when they exceed a window.
- UI shows headlines, previews, key information, expandable digests and explicit
  coverage acceptance. Stable IDs and saved composer selections survive reload.

Limits remain 100 chat attachments / 1 GB per chat, with existing per-file and
Case Background limits. Doc Search retains its separate ingestion route.

Verification includes offline SQLite integration and mocked-provider tests,
frontend tests, and isolated browser workflows. Live Luna summary fidelity and
production storage/MySQL behavior remain deployment-specific verification steps.

The deferred [multiple batch document editing design](BATCHED_DOCUMENT_EDITING_PLAN.md)
addresses oversized exact-source edits. A [saved Luna output example](DOCUMENT_PREPARATION_EXAMPLE.md)
shows the current preparation artifacts and their role in relevance lookup.
