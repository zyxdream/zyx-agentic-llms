# Workspace frontend redesign

The interface now uses a chat-first layout, an indented project sidebar, and an optional document/source inspector. The agent runtime, local account, database schema, and independent ports are unchanged.

## Navigation and project creation

- The desktop sidebar is 280px wide. Project rows have an expansion control, folder icon, name, new-conversation button, and Rename/Delete menu. Conversation rows are indented and have a Rename menu.
- Conversation lists load when needed and are cached by project. Selection and expanded projects survive reload in per-tab session storage. Late list and mutation responses cannot reverse navigation or restore a deleted project.
- New project opens a focused dialog. Users can type a name directly, or choose a folder and use its name or a separate custom name. Native directory picking is feature-detected, with a directory-input fallback and manual naming always available.
- Folder selection reads only name metadata. It neither reads file bytes nor imports documents, stores an absolute path, or establishes live folder access.
- Deletion requires confirmation. Active requests, including clarification waits, and queued/in-progress document preparation block deletion. Nothing is removed from the sidebar until the server confirms success.

## Conversation and documents

The conversation and bottom composer share an approximately 820px reading width. User messages use pale lavender bubbles; assistant replies remain on the plain surface. Saved steps, results, clarification, Stop, Retry, and downloads retain their existing behavior.

The composer offers file attachment to the conversation or project, Fast/Deep selection, source readiness/exclusion information, Send, and Stop. Enter sends; Shift+Enter inserts a line break. IME composition does not submit.

Sources and Document open the right inspector. Its tabs retain source preparation/summaries/inspection/exclusions/retry/cancellation and document preview, explicit Markdown saving, version-conflict handling, and exports. Closing or switching inspector tabs preserves unsaved edits. Leaving an edited document requires confirmation; browser unload also warns about unsaved changes. Inspector state is restored per conversation and browser tab.

Below 1100px the inspector is a full-screen overlay. Project navigation always stays inline: the Workspace button toggles between the expanded sidebar and a persistent icon rail, resizing the conversation instead of covering it. The rail is 64px on desktop and 56px below 768px; the expanded sidebar is 280px on desktop and 220px on narrow screens. The chosen state survives refresh in per-tab session storage, and choosing a conversation does not close it. Menus, dialogs and inspector overlays retain keyboard navigation and focus restoration. Long project/conversation names truncate without displacing their actions.

## Theme and structure

The theme uses smoky violet `#62516F`, pale lavender-gray `#EAE5F0`, near-black `#1C1B1F`, white surfaces, and a `#F2F0F5` sidebar. Dark violet is an accent rather than a dark text-bearing surface. The main text/light-accent contrast is approximately 13.8:1. Sign-in and saved-result views use the same tokens.

The former large Workspace component now delegates navigation, sidebar/dialogs, conversation rendering, composer, inspector and shared interaction primitives to separate modules. The existing request identity, navigation epochs, late-response guards, SQL document versions and single active-run poller are retained.

## API and lifecycle behavior

No new endpoints or database migrations are required. Existing project/conversation CRUD is reused.

- Project creation or rename conflicts return HTTP 409 with `code: project_name_conflict` and a useful message.
- A busy project DELETE returns HTTP 409 with `code: project_busy`, `active_runs`, `active_preparations`, and a message. No work is cancelled implicitly.
- Project deletion, conversation creation, agent admission/retry, upload admission and preparation retry share a short project write lock. Ownership/existence are rechecked after acquiring it. This works with SQLite, where SELECT FOR UPDATE alone is ineffective, and keeps provider calls and file I/O outside the lock.
- A deleted conversation encountered during request admission returns 404 rather than creating orphaned work. Existing source versions, worker leases, publication fencing and cancellation remain authoritative.

## Verification

The retained backend matrix passed **482 tests**, including real Redis/Celery, Docker, and both orders of deletion/admission races. Following the sidebar-toggle update, the frontend passes **95 component tests** and **78 browser scenarios**, comprising all 54 previous replay scenarios plus 24 navigation/layout scenarios. The production build passes. The toggle checks cover inline reflow on desktop and narrow screens, remembered state, unsent-text preservation, keyboard focus, menus and cancelling an active request while collapsed. Screenshots and the current sidebar report are saved under ignored `artifacts/sidebar-toggle/`.

The live Workspace services were restarted on frontend 3010/backend 5010/Redis 6390. Every application table and account hash was unchanged, and the other application's listener PIDs remained unchanged. The existing signed-in browser session still renders Workspace. Verification used temporary test data and mocked external providers, with no paid model calls.

Reports live under ignored `artifacts/frontend-redesign/` and `artifacts/workspace-replay/`. Live folder access/import, automatic conversation naming, project pinning/archiving/reordering, and production deployment remain outside this change. No pushes were made.
