# Project documents, prompt attachments, and Output

The interface separates reusable project inputs, conversation attachments and authored output. Existing accounts, conversations and documents are preserved; no database migration or infrastructure change is required.

| User control | Backend identity | Availability |
| --- | --- | --- |
| Project documents | Prepared uploads with `scope: matter` | Automatically included in eligible request snapshots across the project’s conversations, unless excluded for that request. |
| Composer paperclip / Attachments | Prepared uploads with `scope: chat`; newly sent IDs in `promptAttachments` | Attached to a message and retained in that conversation’s `chatFiles` for follow-ups. |
| Output | `DraftDocument`, separate from uploaded inputs | Current conversation’s editable working document, with expected-version saves and existing exports. Other saved artifacts remain in the conversation’s steps/results. |

Project documents are available through the agent’s document catalog and search/read tools; whole files are not automatically pasted into every model prompt. Retrieval scope, preparation versions, immutable run snapshots, ownership and worker fencing remain authoritative. Internal source endpoints and `matter`/`chat` scope names remain compatible.

## Interface behavior

- The toolbar has Project documents with a project-only count, and Output. Opened views remain as tabs in a resizable right dock. Each tab has a close control; Hide right panel preserves the tab list. The composer’s Attachments link opens conversation-only files; the paperclip goes directly to a file picker.
- Input views retain preparation states, summaries, original-text inspection, retries, removal/cancellation and “Use [filename] for next message” checkboxes. Exclusions survive reload until the next accepted request, then reset. Composer blockers identify which input view needs attention.
- Unsent attachment chips expose status/removal. Removed files cannot reappear in submissions after reload or late transfer completion. Submitted attachments appear beside their message using compatible historical metadata names; the backend also accepts `filename` during metadata normalization.
- Output retains Markdown editing, saved version checks, conflict handling and exports. Closing the panel or opening either input view keeps unsaved edits. Existing navigation/unload guards remain active.
- One shared preparation hook/catalog serves both input lists. Opening an input view refreshes it without starting another poller. Legacy Sources panel preferences map to Project documents; Document maps to Output. Legacy single-view preferences become a one-tab list.

## Folder import

Project documents offers Upload files and Upload folder, even before a conversation exists. Directory selection uses the browser’s directory-file input. Unsupported browsers can still upload individual files.

Folder review lists supported nested files with relative paths, skipped entries and reasons, selected bytes, and applicable quotas. Users may deselect files before uploading. Cancelled/empty selections reserve nothing. Only relative paths and the folder name enter existing upload manifests; imports do not store an absolute computer path or establish synchronization.

The existing file types and quotas are unchanged. Unsupported formats, Office lock files, empty files, oversized entries and invalid relative paths are skipped visibly. Capacity checks account for pending reservations. Reselecting an interrupted folder can resume matching entries without reserving their quota twice, and folder identity prevents unrelated folder names from consuming each other’s pending entries. Ambiguous pending matches require explicit cleanup. Partial transfer failures retain successful files and expose retries for failed ones.

Folder names are added to catalog responses with one bounded query when folders are present. Existing folder IDs and source identities are unchanged. The New project folder chooser remains naming-only.

## Verification

- **501 backend tests passed**, with `ZYX_E2E=1 ZYX_REPLAY_E2E=1 ZYX_PYTHON_LIVE=1`, external provider calls blocked/mocked, real isolated Redis/Celery and Docker checks enabled, and temporary databases/storage.
- **108 component tests passed** and the production build compiled successfully.
- **96 browser workflows passed**: the existing 78 workflows plus 18 input-library/attachment checks. These cover scope isolation, folder review/import/cancellation/limits, partial failures, pending removal versus late transfers, reload/follow-ups, metadata compatibility, unsaved Output edits, project-only uploads, stale refresh failures, narrow-screen focus, and one shared catalog/status poller.
- Desktop and mobile screenshots were inspected. The compressed JavaScript bundle increased by approximately 1.6 kB; this is not a production load or provider-latency benchmark.

Backend XML is under ignored `artifacts/workspace-documents/`; browser reports/screenshots are under `artifacts/workspace-replay/`.

Workspace’s six owned local services were refreshed after confirming no active requests or preparation jobs. Frontend 3010 and API 5010 returned HTTP 200. Every application table fingerprint stayed unchanged, including the existing account, project and conversations; other-project listener PIDs were unchanged.

Folder synchronization, attachment-to-project promotion and a combined gallery of all generated artifacts remain deferred. No pushes were made.

## Adjustable tabs follow-up

Projects, conversation and the right dock now share one horizontal layout at every window width. Draggable dividers resize the side areas; the conversation receives the remainder. Keyboard arrows resize in 10px steps, and Home/End choose bounds. Width preferences live in per-tab session storage independently of each conversation’s ordered open tabs, active tab and visibility.

Repeated opening selects an existing tab. Closing the active tab selects its left neighbour, or the first remaining tab. Closing the last hides the dock. Hiding keeps all tabs; closing/reopening Output keeps unsaved text in memory until navigation or page unload, where existing warnings apply. Hidden/inactive tab contents stay mounted, preserving scroll, expanded summaries and independent file-reader ranges. Conversation changes reset mounted view state and retain existing navigation guards.

When preferred widths cannot fit, Projects collapses first; the right dock then narrows to 280px, preserving at least 320px for conversation. If both cannot fit, the dock hides and a short widening notice appears. Growing the window restores automatic reductions without changing saved widths. A manually expanded project list remains accessible on narrow windows when the dock is hidden. No full-screen inspector replaces the conversation.

This follow-up changes no backend APIs, accounts, data or services and makes no pushes.

Validation for the adjustable-tabs follow-up: **110 component tests**, **99 browser workflows**, and the production build passed. Browser coverage includes tab deduplication/closing/reopening with unsaved Output, independent readers, pointer/keyboard resizing, saved widths, narrow-window hiding/restoration and the retained agent/cancellation/reload workflows. Backend code was unchanged in this follow-up.
