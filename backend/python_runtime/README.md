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
- `input_artifacts`: dictionary mapping selected saved result IDs to their
  already-decoded Python objects. A saved draft is a dictionary, so read
  `input_artifacts[draft_id]["draft"]["markdown"]` directly. Do not pass an
  already-decoded dictionary or list to `open()`, `json.load()` or `json.loads()`.
  Saved file artifact IDs instead map to read-only path strings, for example
  `pd.read_excel(input_artifacts[workbook_id])` after `import pandas as pd`.
- `output_dir`: `pathlib.Path` for new files to save as derived work products.
  Basic `.csv`, `.xlsx`, and `.json` files in this directory can be collected.
- `scratch_dir`: `pathlib.Path` for temporary working files. Scratch files do
  not become user downloads. Reload saved inputs/artifacts in later calls;
  Python variables do not persist between executions.
- `result` (optional): assign a JSON-serializable value for structured results.
  Nested Python date/datetime values are normalized to ISO strings, preserving
  timezone offsets. Other unsupported objects and NaN/infinity remain errors;
  arbitrary values are not silently converted to strings.
  Convert DataFrames with `to_dict(orient='records')`, or write a CSV for larger
  results. `print()` is also available for observations.
- `recalculate_workbook`: helper for explicit workbook formula evaluation.
- `check_workbook`: helper for nominated input/output checks on disposable copies.

For formula calculation only when needed:

```python
from helpers import recalculate_workbook
from openpyxl import load_workbook

working_path = recalculate_workbook(input_paths[file_id])
workbook = load_workbook(working_path, data_only=True)
result = {"total": workbook["Summary"]["B4"].value}
```

`recalculate_workbook` returns a `pathlib.Path`, not a structured JSON result.
Store it in a local variable; use `str(path)` if including it in `result`.
`check_workbook` returns an ordinary result dictionary. Every Python call starts
fresh, so import the modules used in that script again.
A default recalculation destination is in scratch and is not collected as a
download. For a deliverable, supply a different `output_dir` filename explicitly.
After openpyxl saves edits to a formula workbook, recalculate that final formatted
copy because saving discards formula caches. Deliver the file ID belonging to the
actual recalculated output, rather than its input or a scratch inspection copy.

`recalculate_workbook` creates a new working copy, evaluates its workbook
formulas with LibreOffice, and returns that copy's path. Formula text and original
saved values remain in the uploaded source. LibreOffice compatibility differs
from Excel; inspect error cells and report limitations for affected results.
Workbooks with external workbook dependencies cannot be recalculated without
those dependencies. Formula error cells are also reported in execution stderr.

### Generated workbook checks and text fitting

Keep each editable input authoritative and derive related outputs with formulas.
Preserve unchanged relationships during revisions. Identify fixed assumptions
explicitly; a displayed date/rate must not imply it drives a disconnected output.
For date-dependent calculations, derive elapsed periods from the displayed dates
unless the user explicitly requests a fixed period. Label a metadata-only date in
a fixed-period snapshot and explain its refresh requirements. Test the primary
drivers relevant to the assignment, rather than only one convenient input.
Choose a small representative set of relevant changes and independent expected
results from the user's rules, including outputs that should remain unchanged:

```python
checks = check_workbook(final_path, [
    {"changes": {"Inputs!B2": 12}, "expected": {"Summary!C4": 24}},
])
result = checks
```

References use `Sheet!A1` (quoted sheet names are accepted). Each case has `changes`
and a nonempty `expected` dictionary. An empty `changes` dictionary checks the
current outputs. Changes cannot replace formulas or introduce formulas. Each case
starts from the source, recalculates an isolated copy, compares expected cells
with absolute numeric tolerance `1e-8` by default, and reports Excel error cells.
The returned `passed` flag covers only those checks. Failed business comparisons
are findings, not Python exceptions; invalid check arguments remain errors.
The helper never infers business rules, fixes formulas, or edits the source.
Resolve failed checks or disclose their limits before publishing a deliverable.

For selected heading, summary, or note ranges that need text fitting:

```python
final_path = recalculate_workbook(
    scratch_dir / "draft.xlsx", output_dir / "final.xlsx",
    fit_text_ranges={"Summary": ["A1:D1", "A20:D23"]},
)
```

This opt-in finishing step wraps plain-text cells and increases insufficient row
heights at the existing column widths. A temporary measurement cell handles
merged text; no scratch sheet is exported. It does not merge cells, delete
columns, restyle the workbook, or alter values/formulas. Existing larger row
heights are retained. Rich text, rotated text, and differences between Calc and
Excel can require visual review. Inspect the final recalculated workbook, not an
intermediate file, and deliver its exact saved file ID. Avoid unnecessary repeated
calculated views while retaining the detail the assignment requires.

Rebuild this project's analysis image after changing runtime code. These helpers
run inside the existing isolated container; they add no agent tool, reviewer
model call, database schema, or separate workflow.

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
