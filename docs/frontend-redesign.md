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

Empty conversations show only “What’s next?”. The composer is labelled “Message” and uses the placeholder “Message Workspace…”, without task examples or a model selector. The paperclip directly attaches files to the message; those files remain available for follow-ups within the conversation. Pending chips expose status and removal, and sent messages show their attachments after reload. Project uploads live in Project documents. Enter inserts a line break; Shift+Enter sends. A compact “shift+enter” hint sits beneath the send arrow. IME composition does not submit.

Project documents and Output open the right inspector. The composer’s Attachments link opens its conversation file list. The right dock keeps opened views as individually closable tabs, with a separate hide control and no upload destination selector. Input views retain preparation, summaries, reading, next-message exclusions, retry and removal/cancellation; Output retains preview, explicit Markdown saving, version-conflict handling and exports. Closing or switching views preserves unsaved edits. Leaving an edited output requires confirmation; browser unload also warns about unsaved changes. Inspector state is restored per conversation and browser tab, including the old Sources/Document preference names. See [the document and attachment follow-up](project-documents-and-attachments.md).

The inspector always stays beside the conversation. Space constraints first collapse Projects to its rail, then narrow the right dock, then hide the dock when a 320px conversation and 280px dock cannot both fit. Automatically hidden views return on widening; manual hiding stays respected. Dividers support pointer/touch and keyboard resizing. Projects defaults to 280px (220–360px); the dock defaults to 400px (280–640px). Preferred widths persist per browser tab. Project navigation always stays inline: the Workspace button toggles between the expanded sidebar and a persistent icon rail, resizing the conversation instead of covering it. The rail is 64px on desktop and 56px below 768px; the expanded sidebar is 280px on desktop and 220px on narrow screens. The chosen state survives refresh in per-tab session storage, and choosing a conversation does not close it. Menus, dialogs and dock tabs retain keyboard navigation and focus restoration. Long project/conversation names truncate without displacing their actions.

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

Following the copy/model-settings update, the full matrix passes **496 backend tests**, including real Redis/Celery, Docker, deletion/admission races, server defaults, and legacy/resubmission settings preservation; **96 component tests**; and **78 browser scenarios**. The browser matrix contains 54 conversation/interface checks and 24 navigation/layout checks; obsolete model-selector cases now check minimal copy and model-free request payloads. The production build passes. Sidebar checks cover inline reflow on desktop and narrow screens, remembered state, unsent-text preservation, keyboard focus, menus and cancelling an active request while collapsed. See [the copy/model-settings follow-up](model-settings.md).

The live Workspace services were restarted on frontend 3010/backend 5010/Redis 6390. Every application table and account hash was unchanged, and the other application's listener PIDs remained unchanged. The existing signed-in browser session still renders Workspace. Verification used temporary test data and mocked external providers, with no paid model calls.

The document/attachment follow-up passes **501 backend tests**, **108 component tests**, **96 browser scenarios**, and the production build. Its browser matrix includes 18 input-library/attachment cases alongside the existing 78 workflows. Reports live under ignored `artifacts/frontend-redesign/`, `artifacts/workspace-replay/` and `artifacts/workspace-documents/`. Explicit folder import is available from Project documents; live folder synchronization, automatic conversation naming, project pinning/archiving/reordering, and production deployment remain deferred. No pushes were made.
