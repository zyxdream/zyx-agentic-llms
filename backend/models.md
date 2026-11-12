# Database Schema

## Entity Relationship Diagram

```
┌──────────────┐
│    User       │
├──────────────┤
│ id        PK │
│ email        │
│ cognito_user │
│ contact_info │
│ is_disabled  │
└──────┬───────┘
       │ 1
       │
       │ N
┌──────┴───────┐        1 ┌────────────────┐
│   Project     │──────────│     Drafts      │
│  (Matter)     │        N │    (Chat)       │
├──────────────┤          ├────────────────┤
│ id        PK │          │ id          PK │
│ name         │          │ date           │
│ date         │          │ docCategory    │
│ userId    FK │──► User  │ display_name   │
├──────────────┤          │ location       │
│ drafts    rel│──►       │ projectId   FK │──► Project
│ folder_   rel│──►       │ textEntries rel│──►
└──────┬───────┘          └───────┬────────┘
       │                          │
       │ (matter-level:           │ (chat-level:
       │  draft_id=NULL)          │  draft_id set)
       │                          │
       ▼ N                        ▼ N
┌──────────────────┐    ┌──────────────────┐
│  FolderUpload     │    │   TextEntry       │
├──────────────────┤    ├──────────────────┤
│ id            PK │    │ id            PK │
│ folder_name      │    │ date             │
│ draft_id    FK ? │    │ draftId     FK   │──► Drafts
│ project_id  FK ? │    │ textName         │
│ file_count       │    │ content   (LONG) │
│ total_chunks     │    └──────────────────┘
│ upload_time      │
│ files        rel │──►
└──────┬───────────┘
       │ 1
       │
       │ N
┌──────┴───────────────┐
│   UploadedFiles       │
├──────────────────────┤
│ id                PK │
│ unique_id    UNIQUE  │
│ original_filename    │
│ file_size            │
│ page_count           │
│ content       (LONG) │  ← parsed text
│ upload_time          │
│ draft_id        FK ? │──► Drafts    (NULL for matter-level)
│ project_id      FK ? │──► Project
│ folder_upload_id FK? │──► FolderUpload
│ relative_path        │
│ chunks          rel  │──►
└──────┬───────────────┘
       │ 1
       │
       │ N
┌──────┴───────────────┐
│   DocumentChunk       │
├──────────────────────┤
│ id                PK │
│ uploaded_file_id  FK │──► UploadedFiles
│ draft_id        FK ? │──► Drafts    (NULL for matter-level)
│ project_id      FK ? │──► Project
│ chunk_index          │
│ content       (LONG) │
│ token_count          │
│ char_count           │
│ source_filename      │
│ source_relative_path │
│ source_unique_id     │
│ created_at           │
├──────────────────────┤
│ ix_chunk_file_index  │  (uploaded_file_id, chunk_index)
│ ix_chunk_draft       │  (draft_id)
│ ix_chunk_project     │  (project_id)
└──────────────────────┘

┌──────────────────┐
│  StepAnalytics    │   (standalone, no FK cascade)
├──────────────────┤
│ id            PK │
│ date             │
│ userId           │
│ draftId          │
│ docType          │
│ step             │
└──────────────────┘
```

## Scoping Rules

Files and chunks can be scoped to either a **matter** (project) or a **chat** (draft):

| Scope | draft_id | project_id | Visibility |
|-------|----------|------------|------------|
| **Matter-level** | NULL | set | Shared across all chats in the matter |
| **Chat-level** | set | set | Only visible within that specific chat |

## Cascade Delete Behavior

| Parent Deleted | Children Removed |
|----------------|------------------|
| User deleted | (no ORM cascade defined — caller must delete Projects/Sessions first) |
| Project deleted | All Drafts, FolderUploads (and their UploadedFiles/Chunks), SmartSearchSessions, FileTags |
| Draft deleted | All TextEntries, draft-level FolderUploads, chat-level UploadedFiles/Chunks |
| UploadedFile deleted | All DocumentChunks for that file |

## Key Relationships

- `User` 1→N `Project`: via `Project.userId`
- `Project` 1→N `Drafts`: via `Drafts.projectId` (cascade delete)
- `Project` 1→N `FolderUpload`: via `FolderUpload.project_id` (cascade delete)
- `Project` 1→N `FileTag`: via `FileTag.project_id` (cascade delete)
- `Drafts` 1→N `TextEntry`: via `TextEntry.draftId` (cascade delete)
- `Drafts` 1→N `UploadedFiles`: via `UploadedFiles.draft_id` (cascade delete)
- `Drafts` 1→N `FolderUpload`: via `FolderUpload.draft_id` (cascade delete)
- `FolderUpload` 1→N `UploadedFiles`: via `UploadedFiles.folder_upload_id`
- `UploadedFiles` 1→N `DocumentChunk`: via `DocumentChunk.uploaded_file_id` (cascade delete-orphan)

## Durable document preparation

Preparation is required for Case Assistant and Case Background. Migrations
`d5e6f7a8b9c0` and `e6f7a8b9c0d1` add these models:

- `UploadGroup`: owned, idempotently admitted selections with a sealed manifest.
- `DocumentPreparation`: per-document stage, generation/lease fencing, source
  identity, canonical-file link, extraction manifest, published artifact pointer,
  full coverage and separately stored compact coverage counts.
- `DocumentSummarySegment`: typed parse/orientation/section/reduction checkpoints
  with state, generation, attempts and leases; exact source/model/prompt keys.
  Section search text has a MySQL FULLTEXT index. Query evidence checkpoints use
  separate kinds and include authorized scope/artifact/query in their keys.
- `DocumentPreparedArtifact`: immutable document results, compact card, source
  manifest and coverage stored separately. Accepted turns reference its exact ID.

Rich metadata schema v2 preserves full digests and section records without the
legacy three-sentence restriction. Snapshot v2 records authorized document and
artifact versions, exclusions and user/chat/version-bound coverage acceptance.
Preparation workers never rewrite chat-state JSON. See the root runbook for
lifecycle, backfill, source retention and cutover requirements.
