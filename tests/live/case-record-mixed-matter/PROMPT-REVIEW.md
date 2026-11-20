# Prompt review

The ten prompts come from nine website conversations. The four worker failures
on the other platform were logging/worker failures, not evidence that the
prompts are defective.

| ID | Verdict | Change |
| --- | --- | --- |
| 01 | Good | None. Clear scope, output fields and allegation guardrail. |
| 02 | Good | None. |
| 10 | Good | None. Tests unavailable settlement/trial outcomes. |
| 03 | Good with edits | “Allegation/admission” changed to “allegation/established-fact”; all listed dates are conditional on support. |
| 04 | Good | None. |
| 05 | Scope fixed | Narrowed to the two selected agreements instead of “all documents.” |
| 06 | Good | None. |
| 07 | Intentionally underspecified | Kept verbatim as the default-wording retry of 03. |
| 08 | Good | None. |
| 09 | Good | None. |

No prompt in this set is an Excel/spreadsheet task. The only CSV files in the
handoff are evaluator checkers. A separate `tests/live/excel-damages/` suite
covers workbook and CSV work.
