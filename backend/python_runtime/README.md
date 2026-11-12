# Python analysis runtime

## Upload and agent context

Workspace accepts XLSX and CSV in chat and matter uploads. Preparation
extracts cell-addressed searchable text and a deterministic workbook profile
before the document becomes ready. One preparation model call summarizes the
profile and bounded samples for navigation; it does not analyze the whole grid
or recalculate formulas. Original spreadsheet bytes remain available while the
uploaded document exists.

The first controller call and `list_documents` use the same compact catalogue:
accepted file/version IDs, filenames and paths, scope/current-attachment flags,
navigation summaries, sheet/header/sample hints, and extraction warnings. The
agent selects file IDs after seeing the user's request. `python_run` can inspect
uncertain layouts and then analyze the complete selected originals. Source
search/read tools retain sheet/cell/range locations alongside text documents.

## Execution host setup

Build once on each execution host:

```sh
docker build -t zyx-agentic-llms-python-analysis:1 backend/python_runtime
```

The application calls `backend.util.python_execution.run_python`. The runner
starts a fresh container per call; it never evaluates submitted code in Flask
or in the Celery interpreter. Configure `AGENT_PYTHON_IMAGE` and optionally
`AGENT_PYTHON_DOCKER_HOST` for the execution host. A missing Docker engine or
image is reported as an operation error, with no host Python fallback.
Each container also enforces its own execution deadline, so a lost host worker
cannot leave an analysis running indefinitely.

`AGENT_PYTHON_STORAGE_DIR` defaults to `instance/workspace/python`; web and worker
processes must share this durable storage for later file reuse and downloads,
as they already share document source storage. `AGENT_PYTHON_TIMEOUT_SECONDS`
defaults to 120 and is bounded by the assignment's remaining deadline. Both
settings can be supplied through Flask configuration or environment variables.

`python_run(code, file_ids, artifact_ids)` uses the existing agent tool loop.
Python errors and timeouts are saved observations, so the controller can amend
its script and call again. Cancellation stops execution. Each completed step
retains the code, exact input references, result/error, library versions, and
derived-file references in existing SQL artifacts; no schema migration is
required. CSV/XLSX exports are new files served through an authenticated
workflow/artifact download endpoint. Originals are never replaced.

## Model-facing execution contract

Write normal Python with pandas, numpy, or openpyxl. Each call starts fresh.
The application supplies these globals:

- `input_paths`: dictionary mapping authorized `file_id` values to read-only
  input paths. Use `pd.read_excel(input_paths[file_id], sheet_name=...)` or
  `pd.read_csv(input_paths[file_id])` as appropriate.
- `input_artifacts`: dictionary mapping selected saved artifact IDs to their
  JSON content, or to an input file path for a saved file artifact.
- `output_dir`: `pathlib.Path` for new files to save as derived work products.
  Basic `.csv`, `.xlsx`, and `.json` files in this directory can be collected.
- `scratch_dir`: `pathlib.Path` for temporary working files. Scratch files do
  not become user downloads. Reload saved inputs/artifacts in later calls;
  Python variables do not persist between executions.
- `result` (optional): assign a JSON-serializable value for structured results.
  Convert DataFrames with `to_dict(orient='records')`, or write a CSV for larger
  results. `print()` is also available for observations.
- `recalculate_workbook`: helper for explicit workbook formula evaluation.

For formula calculation only when needed:

```python
from helpers import recalculate_workbook
from openpyxl import load_workbook

working_path = recalculate_workbook(input_paths[file_id])
workbook = load_workbook(working_path, data_only=True)
result = {"total": workbook["Summary"]["B4"].value}
```

`recalculate_workbook` creates a new working copy, evaluates its workbook
formulas with LibreOffice, and returns that copy's path. Formula text and original
saved values remain in the uploaded source. LibreOffice compatibility differs
from Excel; inspect error cells and report limitations for affected results.
Workbooks with external workbook dependencies cannot be recalculated without
those dependencies. Formula error cells are also reported in execution stderr.

The host retains submitted code, complete stdout/stderr logs, and result data in
the chosen attempt directory. Tool responses use explicitly marked previews;
the application owns durable artifact IDs, authorization, and downloads.

## Validation

Activate the project Python environment before backend tooling. The following includes actual
containers, pandas analysis, formula recalculation, error recovery, output reuse,
and an authenticated generated-file download:

```sh
ZYX_PYTHON_LIVE=1 pytest -q tests/test_python_execution.py tests/test_agent_spreadsheets.py
```

Spreadsheet preparation and source-context regressions can run without Docker.
Live container tests are explicitly skipped unless that variable is set. These
tests exercise real execution with deterministic controller/provider boundary
doubles; they do not claim a live LLM evaluation or production deployment.

## Document PDF exports

The same image now includes LibreOffice Writer. Rebuild it when deploying the
Markdown document feature. The web process uses a fixed Writer conversion
helper to convert a saved DOCX to PDF in a restricted container; it does not
execute model-written code for document downloads. Markdown, HTML and DOCX
exports do not need Docker. The `python_run` output types remain CSV/XLSX/JSON.

`DOCUMENT_EXPORT_STORAGE_DIR` defaults to `instance/workspace/exports`. Keep this
private and durable on the web hosts serving downloads. Each export record
stores its source revision, format, layout preset, file key and content hash.
The authenticated download route rechecks the revision's owner/chat/source
snapshot before returning a saved or newly converted file. Export conversion
does not call an LLM or modify the source revision.
