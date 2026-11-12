# Draft validation diagnostics — November 9, 2026

The same local XLSX/CSV financial-review scenario reproduced the authoring
failure twice with diagnostic capture enabled. The exact cause is now captured:
the installed SDK tries to parse a plain-language commentary message as the
draft JSON object, before reaching a valid final-answer object.

No fix, schema change, prompt change, extra model instruction, retry change,
fallback rule or output filtering was applied.

## Minimal capture and verification

`backend/ai/parse_diagnostics.py` adds opt-in capture through
`LLM_PARSE_DIAGNOSTICS_DIR`. With the variable unset, the previous SDK call runs
directly. When enabled, the SDK's raw-response interface retains the HTTP body,
then invokes the same SDK parser. A validation failure saves the exact response
body, error details, traceback, schema, response/request IDs, status and usage.
The original exception is re-raised into the existing error handler, which
still returns the same generic tool observation.

Capture directories use mode 0700 and files 0600, outside public/download paths
and Git tracking. Request headers, credentials, prompts and workflow lease
tokens are not added to diagnostic metadata.

Conda `lexintel`: **70 tests passed** across diagnostic capture, existing
response helpers and document drafting. Actual SDK/MockTransport tests confirm
identical outgoing request JSON, schema, model, reasoning and output settings;
unchanged successful results, failure observations and retry counts; exact
failed-response bytes; private permissions; and unchanged original error when
capture storage fails. No SDK package was upgraded or edited.

## Unchanged local scenario

- Fresh matter 17; task 255, **Invoice reconciliation — diagnostic capture**.
- UI: `http://localhost:3000/case-assistant/3d8679c7-0b0a-47bf-9ed9-5839f5681063`.
- Workflow: `wf_609b75c2-08ae-4811-8b51-3691ba238b10`.
- The initial prompt matches the previous test byte-for-byte. The same two
  fixture files and reporting cutoff were used, with empty Case Background,
  an empty draft, Deep mode and `gpt-6.1-sol`.
- Runtime: OpenAI SDK **1.102.0**, Pydantic **2.9.2**.
- The first controller catalogue contained exactly the workbook and CSV.
  Both prepared successfully. Python completed before the authoring failures.
- Two `create_draft` calls returned the existing generic validation-error
  observation. A third succeeded without intervention. The controller then
  continued its ordinary verification/revision work.

After the two failures and their offline reproduction established the diagnosis,
the tester stopped the diagnostic run. This is a failed-stage diagnostic rerun,
not a claim that the full revision/download scenario was repeated to completion.
Saved analysis and candidates remain available in the stopped workflow. Upload
hashes remained unchanged and no active workflow/preparation remained.

Capture was enabled only on the local agent worker for this run. The worker was
restarted afterward without the variable; normal local services remain running.

## Exact failed response

Both responses had HTTP **200**, `status="completed"`, and
`incomplete_details=null`. Both contained a commentary message followed by a
final-answer message:

| Capture | Commentary | Final answer | Offline validation |
|---|---|---|---|
| `5dd477ec8db84419b870fbfd86381490` | `output[2]`, phase `commentary`, 303 characters | `output[3]`, phase `final_answer`, 5,379 characters | Commentary fails JSON parsing; final object passes the unchanged draft schema |
| `b87a6af995aa4877b5e21b3a2ae15d63` | `output[1]`, phase `commentary`, 293 characters | `output[2]`, phase `final_answer`, 5,426 characters | Same result |

The first commentary begins:

```text
The reconciliation shows $2,960.00 outstanding, including $2,000.00 overdue across four invoices.
```

The captured error is:

```text
pydantic_core._pydantic_core.ValidationError:
1 validation error for MarkdownDraftResult
Invalid JSON: expected value at line 1 column 1
type=json_invalid
loc=[]
```

This is a root JSON parse error on commentary prose. It occurs before any draft
property is validated. The actual final-answer JSON contains valid `title`,
`markdown` and `update_court_format` fields and passes validation without edits.
The returned `text.format` also records JSON Schema mode with `strict=true`.

## Confirmed failure path

```text
HTTP 200, completed response containing commentary + final_answer
→ SDK parse_response loops through every message/output_text
→ tries MarkdownDraftResult.model_validate_json(commentary_text)
→ json_invalid at line 1, column 1
→ parsing stops before the valid final_answer
→ existing app wrapper returns None
→ writer emits “The drafting provider did not produce a document.”
```

The traceback points to the installed SDK's
`openai/lib/_parsing/_responses.py:75`, then line 138, and
`openai/_compat.py:169`. Replaying the entire unchanged SDK parser against the
captured HTTP response reproduces the same failure. Separately validating each
message proves that the commentary fails and the final answer succeeds.

The evidence rules out a missing draft field or truncated final JSON for these
two captures. It identifies phase handling during SDK parsing as the failing
boundary. The original earlier run did not retain its raw responses, so these
captures directly establish the cause of the reproduced failures, rather than
recovering the discarded earlier responses.

## Evidence

Ignored private evidence is under
`artifacts/live-case-assistant/drafting-diagnostics-20261109/`:

- `private-responses/<capture>/response-body.json`: original HTTP response bytes.
- `private-responses/<capture>/diagnostic.json`: validation details and traceback.
- `failed-output-text/<capture>-0.txt`: exact commentary that failed.
- `failed-output-text/<capture>-1.txt`: exact final JSON that passed offline.
- `capture-analysis.json`: per-message validation and whole-SDK replay results.
- `run-metadata.json`: prompt identity, original hashes and runtime versions.
- `diagnostic-final-workflow.json`: saved request/tool/artifact state after Stop.
- `diagnostic-run-stopped.png`: browser evidence of the stopped run and saved work.

Raw-response hashes were preserved throughout offline inspection. The diagnosis
is ready for a separate fix decision; runtime parsing remains unchanged.
