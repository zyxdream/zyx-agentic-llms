# Explicit generated-file delivery

Generated files are delivered by immutable IDs, independently of their filesystem locations and display filenames. Storage directories and existing download routes are unchanged; no database migration is required.

## Completion contract

The model-facing `complete_task` schema now requires `output_file_ids: list[str]`. The controller selects exact `derived_file` IDs returned by successful Python operations, including the final recalculated file when intermediate files have the same name. `[]` means no file deliverables. Previously saved tool calls without the field remain replayable as `[]`.

Publication resolves the selection to `{file_id, execution_id, filename, file_size}`. Each file must belong to the current request, be listed by its successful, completed Python execution, match its saved metadata, and have intact stored bytes. Repeated IDs are deduplicated in selection order. Filenames and creation order never choose a version. These checks establish file identity and successful execution; they do not certify the semantic correctness of a workbook.

Selection metadata, the conversation turn, optional document update and completed run result commit atomically. Recovery rebuilds the same selection from the durable turn. Existing ownership, source-version, cancellation and worker-lease checks still apply.

## User interaction

Conversation answers display the selected files as download cards without opening Steps and saved results. Cards and existing result-panel controls share the authenticated Blob downloader. The existing route resolves run, execution and file IDs; neither the model nor the browser supplies a storage path. Download failures preserve the answer and permit retry; navigation prevents stale responses from starting downloads in another conversation.

The controller is instructed to reference files by name and supply their IDs, rather than invent `sandbox:`, localhost, or filesystem URLs. Unsupported Markdown URL schemes remain blocked. Historical answers are not rewritten and no final file is inferred for older turns; their existing saved-result downloads remain available.

## Storage boundary

Private files remain under `instance/workspace/sources`, `python`, and `exports`. Database file IDs, server-side storage keys, display names and download addresses remain distinct. A future private object-store adapter can change how bytes are retrieved while keeping the file IDs and download interaction stable; S3 integration and existing-path migration are deferred.

## Validation

Coverage includes required schema/legacy replay, explicit selection among same-named files, incorrect ownership/kind/execution relationships, missing or modified bytes, atomic rollback, completion recovery, reload, authenticated downloads, error/retry, safe filenames, and stale navigation. A real isolated Redis/Celery/Docker replay generates a CSV, publishes its selected ID, reloads the conversation and downloads the actual bytes, with provider calls mocked.

Verification passed: **524 backend tests** with isolated Redis/Celery/Docker enabled, **117 component tests**, **102 browser workflows**, and the production build. The browser checks include downloading the exact selected file after reload and recovering from a failed download. Reports are under ignored `artifacts/final-files/` and `artifacts/workspace-replay/`.
