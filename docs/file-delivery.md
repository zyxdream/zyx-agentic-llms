# Explicit generated-file delivery

Generated files are delivered by immutable IDs, independently of their filesystem locations and display filenames. Storage directories and existing download routes are unchanged; no database migration is required.

## Completion contract

The model-facing `complete_task` schema now requires `output_file_ids: list[str]`. The controller selects exact `derived_file` IDs returned by successful Python operations, including the final recalculated file when intermediate files have the same name. `[]` means no file deliverables. Previously saved tool calls without the field remain replayable as `[]`.

Publication resolves the selection to `{file_id, execution_id, filename, file_size}`. Each file must belong to the current request, be listed by its successful, completed Python execution, match its saved metadata, and have intact stored bytes. Repeated IDs are deduplicated in selection order. Filenames and creation order never choose a version. These checks establish file identity and successful execution; they do not certify the semantic correctness of a workbook.

Selection metadata, the conversation turn, optional document update and completed run result commit atomically. Recovery rebuilds the same selection from the durable turn. Existing ownership, source-version, cancellation and worker-lease checks still apply.

## Selected Excel file readiness

Before publication, the server inspects each selected generated `.xlsx` read-only.
It must open, retain a stored result for every formula, and contain no Excel error
cells. Zero results and cached empty strings are valid. Formula-free workbooks
need no recalculation. CSV/JSON keep their existing integrity checks.

The scan runs outside publication write locks and never calculates formulas,
executes macros, or retrieves external links. It is bounded by the existing
100 MiB output-size limit (also applied to expanded workbook contents), a
30-second cooperative scan limit, and the request's earlier deadline. Reaching
a limit fails verification. Under the normal publication locks, the application
rechecks ownership, execution, cancellation, source/document freshness, and the
selected file IDs and hashes before committing.

A failed file prevents the whole completion: no answer, document update, or file
selection is published. The existing recoverable tool error identifies the file,
reason and up to five affected cells. The agent can repair/recalculate into a new
`output_dir` file and select its new file ID. There is no automatic recalculation,
file substitution, omission, or extra model reviewer.

The completion step's existing JSON saves server-generated `file_verification`
reports atomically with publication: file ID, SHA-256 hash, check version, status,
and workbook summary counts. Public download metadata and routes stay unchanged.
A report covers those exact selected bytes, including after reload; inspecting a
scratch copy or another same-named file does not validate the delivered file.
It establishes basic readiness, not correct business rules, fresh formula values,
research accuracy, or visual quality. Historical completed downloads are unchanged;
older unpublished files receive the check when selected for a new completion.

## User interaction

Conversation answers display the selected files as download cards without opening Steps and saved results. Cards and existing result-panel controls share the authenticated Blob downloader. The existing route resolves run, execution and file IDs; neither the model nor the browser supplies a storage path. Download failures preserve the answer and permit retry; navigation prevents stale responses from starting downloads in another conversation.

The controller is instructed to reference files by name and supply their IDs, rather than invent `sandbox:`, localhost, or filesystem URLs. Unsupported Markdown URL schemes remain blocked. Historical answers are not rewritten and no final file is inferred for older turns; their existing saved-result downloads remain available.

## Storage boundary

Private files remain under `instance/workspace/sources`, `python`, and `exports`. Database file IDs, server-side storage keys, display names and download addresses remain distinct. A future private object-store adapter can change how bytes are retrieved while keeping the file IDs and download interaction stable; S3 integration and existing-path migration are deferred.

## Validation

Coverage includes required schema/legacy replay, explicit selection among same-named files, incorrect ownership/kind/execution relationships, missing or modified bytes, atomic rollback, completion recovery, reload, authenticated downloads, error/retry, safe filenames, and stale navigation. A real isolated Redis/Celery/Docker replay generates a CSV, publishes its selected ID, reloads the conversation and downloads the actual bytes, with provider calls mocked.

Earlier delivery pass: **524 backend tests** with isolated Redis/Celery/Docker enabled, **117 component tests**, **102 browser workflows**, and the production build. The browser checks include downloading the exact selected file after reload and recovering from a failed download. Reports are under ignored `artifacts/final-files/` and `artifacts/workspace-replay/`.

Final-file readiness pass: **218 affected backend checks** and **11 isolated
Redis/Celery replays** passed with provider calls mocked and real Docker/Calc
enabled. The new replay first rejects an uncached XLSX without publishing a turn,
then recalculates into a new saved same-named file, publishes its exact ID, and
verifies the downloaded bytes and stored hash. Focused regressions also cover
unreadable workbooks, missing/zero/empty-string caches, Excel errors, size/time
limits, same-name confusion, changed bytes/metadata, cancellation during the scan,
atomic rollback with a draft candidate, successful repair, reload and recovery.
No frontend code changed or browser/provider quality evaluation was rerun for
this readiness pass.
