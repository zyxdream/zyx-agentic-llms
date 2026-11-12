# File Upload Limits

## Side-by-side (per matter / project)

| | Case Assistant attachments | Case Background | Doc Search |
| --- | --- | --- | --- |
| **Scoped to** | Per draft/chat | Per matter (background corpus) | Per matter (search corpus) |
| **Files** | 100 / chat | 30 / matter | Unlimited (count) |
| **Pages** | Unlimited | Unlimited | Unlimited |
| **Total corpus size** | 1 GB / chat | 1 GB / matter | 1 GB / matter (UI only) |
| **Per-file size** | 50 MB (UI + backend) | 100 MB (UI + backend) | 50 MB (UI only); backend uncapped |
| **Types** | pdf, doc, docx, txt | pdf, doc, docx, txt | + msg, eml, png, jpeg, jpg |

Frontend guards for file count (Case Assistant, Case Background) and Doc Search total bytes mirror backend limits where enforced; see sections below for routes and workers.

## Chat Level (per draft/chat session — Case Assistant task-specific files)

| Limit | Value | Routes / workers |
|-------|-------|------------------|
| Max files per chat | 100 | UI, active upload API/worker, and preparation manifest admission |
| Max total size per chat | 1 GB | UI and active API; worker serializes quota check/insertion. Preparation additionally reserves pending/failed capacity at admission. |
| Max total pages per chat | None (unlimited) | Case Assistant page counts are stored, not quota-enforced |
| Max size per file | 50 MB | `everything/upload-files-async` (pre-staging), worker re-check |

## Matter Level (per project, shared across all chats — Case Background)

| Limit | Value | Routes |
|-------|-------|--------|
| Max files per matter | 30 | `upload-file-matter`, `upload-folder-matter`; frontend pre-check in `EverythingDrafter.js` |
| Max total size per matter | 1 GB | Preparation manifest admission; loose and folder files share the quota |
| Max total pages per matter | None (unlimited) | Case Background page counts are stored, not quota-enforced |
| Max size per file | 100 MB | Staging in matter upload routes (`MAX_MATTER_FILE_SIZE`) |

## Processing Capacity

| Parameter | Value | Purpose |
|-----------|-------|---------|
| Chunk target size | ~800 tokens | `backend/util/chunker.py` |
| Preparation source sections | Target 28K tokens, up to 32K with overlap | Every readable source range is processed; compact source markers retain larger coherent sections. |
| Preparation request input | 40K section / 48K reduction tokens | Includes instructions, orientation and schema. |
| MAX_FULL_CONTENT_CHUNKS | 100 (~80K tokens) | Legacy full-content helper cap; not the active Case Assistant relevant-extraction path. |
| BM25 retrieve top_k | 60 (~48K tokens) | Cap for keyword-based retrieval |
| Pages per chunk (approx.) | ~2.1 | Based on ~380 tokens/page |
| Cloud PDF OCR total timeout | 600 seconds / 10 minutes | `backend/util/cloud_pdf_parser.py` |

Chat-level and matter-level Case Background do not enforce total page quotas;
page counts are stored. Doc Search uses BM25 retrieval. Case Assistant uses
prepared document cards, structured section search, and original source evidence;
exhaustive work visits every requested section instead of sampling long files.

## Required durable preparation

Case Assistant and Case Background always use durable preparation. The former
feature flag is ignored; missing infrastructure blocks upload/prompt workflows
with an explicit error. Doc Search retains its separate ingestion route.

Selections are admitted atomically, including pending/failed capacity, then sent
in groups capped at five files and 100 MiB with two concurrent browser requests.
Per-request body guards allow another 2 MiB for multipart framing. Web requests
stage source bytes; document workers parse, index and summarize through Luna.

All readable source is processed in bounded sections. Detailed digests and
structured section records remain available alongside compact document cards.
Send and direct prompt admission require preparation readiness and explicit
acceptance of any extraction gaps, or exclusion of the affected document.
See [the preparation runbook](DOCUMENT_PREPARATION_RUNBOOK.md) for cutover,
recovery, model budgets and operational checks.

## Smart Search / Doc Search Level (per project/matter, shared across search sessions)

| Limit | Value | Routes |
|-------|-------|--------|
| Max files per matter | None (unlimited) | `smart-search/upload-file`, `smart-search/upload-folder` |
| Max total pages per matter | None (unlimited) | `smart-search/upload-file`, `smart-search/upload-folder` |
| Max total bytes per matter | 1 GB | Frontend pre-check in `EverythingDrafter.js` (`DOC_SEARCH_MAX_TOTAL_BYTES`) |
| Max size per file | 50 MB | Frontend pre-check in `splitDocSearchUploadableEntries` (`DOC_SEARCH_FILE_MAX_BYTES`) |

> Backend staging for Doc Search does not yet enforce per-file or total-byte caps;
> these limits apply in its UI. Case Background enforces its own count/size limits.

## Notes

- Page counts are stored in `UploadedFiles.page_count`; Case Assistant has no page-total quota.
- Case Background no longer enforces a total page quota.
- Unsupported file types are skipped and reported in `skipped_files`. Supported types: `.pdf`, `.doc`, `.docx`, `.txt`.
- The chat counter includes all saved/preparing documents, including earlier turns. Composer selections persist per chat in the current browser tab. On reload, untouched-task uploads are restored from server records even without browser state; prior prompt metadata distinguishes previously submitted files from new attachments. Removing an unsubmitted restored upload deletes that document and releases its quota.
- Case Background uploads enqueue one Celery task per staged file with `source='everything'`.
- Doc Search uploads enqueue one Celery task per staged file with `source='search'`.
- The global request limit remains unset; preparation routes apply a separate bounded request limit before reading multipart data.
