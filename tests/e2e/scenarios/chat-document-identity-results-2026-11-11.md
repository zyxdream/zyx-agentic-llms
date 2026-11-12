# Chat and document identities — local validation

Implemented on `Monica-agent`, building on the separate draft/history persistence
changes. No production migration was executed.

## Resulting ownership

```text
Matter
└── ChatThread (chat_threads; existing integer/public IDs preserved)
    ├── ConversationTurn[]
    ├── CaseAssistantWorkflow[]
    │   ├── AgentStep[]
    │   └── AgentArtifact[]
    ├── Chat attachments and auxiliary metadata
    └── DraftDocument (draft_documents; independent ID/public UUID)
```

Each chat owns one empty document slot at creation, matching the selected default.
Document content/review/version are separate from chat identity and turn ordering.
Existing guided, HG and Word containers retain their categories and behavior.
`Drafts` is a bounded model alias for legacy callers; there is one canonical chat
table. The old `workspace_draft_states` table is archival evidence and is excluded
from automatic removal by Alembic. Runtime code does not read it as current state.

Existing `draftId` routes continue to identify chats only. Current editor/agent
requests use explicit public `chat_thread_id` and `draft_document_id` fields.
Document access is constrained to its owning chat. Saved requests pin document
identity and base version; historical requests retain their existing hash checks.

Publication remains one transaction for the document, final turn, saved step and
workflow completion. The moved conversation counter requires Chat → Workflow →
Document lock order; focused tests check the new and legacy publication paths.

## Migration and local cutover

- Stopped local web/worker writers and backed up `instance/myapp.db` with SQLite's
  backup API. Backup integrity was `ok`.
- Rehearsed on a copy, then upgraded the local database to `c0d1e2f3a4b5`.
- Validated **263 chats, 263 owned documents and 214 conversation turns** at cutover.
- Compared all old columns in chat, turn, workflow, step/artifact, upload,
  preparation and analytics tables using ordered row hashes. Their values were
  preserved after accounting for renamed ownership columns.
- Compared document content, review snapshots, versions, update timestamps,
  conversation counters and legacy evidence exactly. Original archive bytes,
  workflow JSON, upload scope keys and revision IDs remained unchanged.
- Repeated reconciliation reported no unresolved identity/ordering records and
  no additional document or turn creation.

The old database already contained **816 orphan upload/chunk references**. The
before/after foreign-key findings matched exactly; this migration introduced none.
Those records were retained, with cleanup outside this refactor. SQLite integrity
was `ok`. MySQL was reviewed for rename/FK/index behavior; no live MySQL migration
was run.

Private backups, row-comparison reports, identity mappings and service logs are in
the ignored `artifacts/chat-document-identity-cutover-20261111/` directory.

## Real LLM browser scenario

1. Opened the existing `Persistence cutover smoke — 2026-11-10` chat using its
   unchanged public URL. Both earlier turns and its Markdown document loaded.
2. Created `Chat document identity smoke — 2026-11-11`. Before prompting, the
   database had **chat ID 267**, **document ID 264**, distinct public UUIDs, version
   **1**, an empty document and no turns.
3. Asked the unchanged Fast controller to create **Identity Check**, containing
   Consultation **$120**, Filing **$80**, and total **$200** in a Markdown table.
   `create_draft` saved a candidate, then `complete_task` published it. The workflow
   targeted document 264 at base version 1; publication produced document version
   **2** and one final conversation turn with explicit chat/document metadata.
4. Asked a chat-only question about the total. The agent read the current draft
   and completed without a candidate. Its base version was **2**; the same document
   stayed at version **2**, with a second final turn.
5. Reloaded and verified both answers and the rendered table. Downloaded
   `Identity Check.md` using the normal download control and native Save dialog.
   Its **104 bytes** matched the published Markdown exactly.

The normal app is running at `http://localhost:3000`, backed by local port **5001**
because AirPlay also listens on 5000. No macOS setting or committed brand
configuration changed. Restart details are in `AGENT_CONTROLLER.md`.

## Validation

- Full backend suite in Conda `lexintel`: **2,174 passed, 50 skipped**, with
  **11 subtests passed** (`pytest -q tests --ignore=tests/e2e --tb=short`). The skips
  are existing opt-in integration/local-fixture cases; relevant real-worker checks
  were enabled separately below. Existing deprecation warnings remain.
- Storage/migration checks passed, including canonical and legacy-only imports,
  pre-DDL validation, owner constraints, preserved revisions, FK-enabled SQLite
  upgrade/downgrade, independent numeric IDs and deletion cascades.
- **30 frontend Jest checks** and **10 local mock browser scenarios passed**.
  Coverage includes typed identifiers, 409 retained edits/copy/reload, queued saves,
  cross-chat responses and a response naming the wrong document.
- **5 real Redis/Celery checks passed**, including duplicate deliveries and legacy
  chord completion on memory/isolated Redis transports, plus Word Redis scripts.
- The actual Alembic autogeneration regression runs in a subprocess so its logging
  and default Celery configuration cannot affect later tests.

No sharing, cross-chat document editing, new tools, model changes or new execution
framework were added. Immutable revision artifacts remain unchanged; manual editor
versions remain a separate concurrency counter.
