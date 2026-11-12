# Separate workspace persistence — local validation

Implemented on `Monica-agent`; no production migration was run.

## Storage and publication

`WorkspaceDraftState` owns the current draft, review snapshot and version.
`ConversationTurn` owns ordered exchanges and their metadata. Workspace JSON is
a read projection; explicit draft, append and auxiliary operations stage changes
without committing independently. Both workflow execution versions commit draft,
answer and workflow completion together. Recovery uses the unique canonical
workflow-to-turn association.

Editor mutations require an expected version. The UI serializes its saves per
workspace, retains conflicted content for copying and offers explicit reload.
Switching workspaces closes the old title/section editor; late responses cannot
apply to the newly selected workspace. No automatic merging or model/tool changes
were added.

## Checks

- Conda `lexintel`: **717 passed, 6 skipped** across focused storage, publication,
  editor, controller, legacy workflow, conversation context, uploads, Word add-in,
  research, spreadsheet and download suites. Provider/broker boundaries were
  mocked where appropriate; SQL persistence was real.
- Focused frontend Jest suites: **25 passed**.
- Opt-in real worker/broker checks subsequently enabled: **5 passed** (duplicate
  agent delivery and legacy chord publication on memory/isolated Redis transports,
  plus Word Redis scripts).
- Final compatibility review: **21 publication/editor tests passed** after fixing
  the older section update/rename route and rejecting malformed full-draft payloads.
  **95 Word tests passed** (one opt-in Redis skip, exercised separately above)
  after initializing new Word workspaces at creation rather than at first read.
- Local Playwright browser scenarios: **9 passed**, including four new persistence
  cases: completed answer during an editor save and reload; HTTP 409 with retained
  content and actual clipboard copy; serialized saves with successive versions;
  and a late response after switching workspaces. Five existing nearby workflow,
  attachment, candidate and workspace-switch scenarios also passed.
- Publication rollback was injected at turn and completion writes. Legacy failure
  responses were checked not to become completed Retry markers. Repeated delivery,
  post-commit failure recovery, review actions, all-history retention and immutable
  revision downloads were covered by the backend suites.

The six skips are existing opt-in integration or local-fixture cases, not newly
skipped regressions.
Existing library deprecation warnings remain.

## Local cutover

Stopped local web/worker writers and backed up `instance/myapp.db` using SQLite's
backup API before schema revision `b9c0d1e2f3a4`. Dry-run reconciliation preceded
the committed backfill: **262 drafts, 212 turns, 44 workflow-backed answers**.
All existing completed workflow answers were present; no missing answer needed
recovery on this particular database (missing-answer recovery is tested).

The reconciliation report lists sixteen history/metadata length differences and
one workspace with multiple legacy state rows. Original source bytes were retained
for every workspace. A repeat import reported every workspace already initialized;
turn counts stayed unchanged and SQLite integrity was `ok`.

Private operational evidence and the backup remain in the ignored directory
`artifacts/workspace-persistence-cutover-20261110/`. The original data is not part
of this checked-in report.

## Real LLM browser smoke test

Created synthetic task `Persistence cutover smoke — 2026-11-10` (workspace 266).
The ordinary local controller and workers used their unchanged Fast model settings.

1. Asked for a Markdown draft titled **Persistence Smoke**, containing only
   “The review meeting is scheduled for Monday.” Controller called `create_draft`,
   received a saved immutable candidate, then called `complete_task`. The database
   held one completed workflow/turn and draft version **2**.
2. Asked for a chat-only confirmation of the title and meeting day. Controller
   called `read_sources` for the saved draft, then `complete_task` without a
   candidate. The second completed turn was appended; draft/version stayed **2**.
3. Reloaded the browser and confirmed both turns and the draft. Downloaded
   `Persistence Smoke.md` through the normal download control and native Save
   dialog, then compared its exact 64 bytes with the published Markdown.
   The draft, version and two unique workflow associations remained unchanged.

The normal frontend is at `http://localhost:3000`. The backend uses local port
**5001** because macOS AirPlay also answered on 5000. Only the ignored generated
frontend runtime configuration was pointed to 5001; no macOS setting or committed
brand configuration changed. See `AGENT_CONTROLLER.md` for restart instructions.
