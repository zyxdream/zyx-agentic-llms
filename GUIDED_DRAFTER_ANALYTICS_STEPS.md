# Guided Drafter Analytics Step Map

This file documents the meaning of `StepAnalytics.docType` and
`StepAnalytics.step` for Guided Drafter workflows.

An analytics row represents one backend execution attempt of a main step. It starts
with status `running` and finishes as `succeeded`, `failed`, or `cancelled`.
Retries create additional rows with the same `docType` and `step`. Upload/parse-only
jobs and routine within-step saves are not recorded. Auxiliary refinements do not
create rows; their LLM usage is added to the latest matching outer-step attempt.

## Answer

`docType`: `answer`

| Step | Recorded operation |
|---:|---|
| 0 | Analyze the parsed complaint and prepare the Answer workflow. |
| 1 | Draft paragraph-by-paragraph responses to the complaint. |
| 2 | Draft the selected affirmative defenses. |
| 3 | Draft the counterclaim, if requested, and prepare the introduction. |
| 4 | Save the approved introduction, finalize the Answer, and run its citation check. |
| 5 | Download the Answer as a Word document. |

## Complaint

`docType`: `complaint`

| Step | Recorded operation |
|---:|---|
| 0 | Process the submitted facts and prepare the complaint sections and suggestions. |
| 1 | Apply the selected causes of action and draft the claim sections. |
| 2 | Draft the selected prayer for relief. |
| 3 | Save the introduction, finalize the complaint, and run its citation check. |
| 4 | Download the complaint as a Word document. |

Regenerating citations refines the output of step `0`, so its LLM calls and tokens
are attributed to the latest Complaint step `0` row.

## Complaint Analyzer

`docType`: `Complaint Analyzer`

| Step | Recorded operation |
|---:|---|
| 0 | Parse the complaint and prepare its facts and causes of action. |
| 1 | Analyze the legal elements and pleaded facts for each cause of action. |
| 2 | Generate the selected-side recommendations and legal analysis. |
| 3 | Finalize the analysis document and run its citation check. |
| 4 | Download the analysis as a Word document. |

Regenerating citations refines the legal-element analysis, so its LLM calls and
tokens are attributed to the latest Complaint Analyzer step `1` row.

## Motion to Dismiss

`docType`: `motion to dismiss`

| Step | Recorded operation |
|---:|---|
| 0 | Parse the complaint and prepare the factual background and legal standard. |
| 1 | Analyze the claims and suggest dismissal grounds. |
| 2 | Draft a selected argument section. This may be recorded more than once when arguments are drafted in batches. |
| 3 | Save the final argument and generate introduction and conclusion suggestions. |
| 4 | Save the introduction and conclusion and finalize the motion. |
| 5 | Download the motion as a Word document. |

Rewriting the factual background is attributed to step `0`. Expanding or revising
an argument is attributed to the latest step `2` attempt. Neither action creates a
separate analytics row.

## Requests for Admission

`docType`: `RFA`

| Step | Recorded operation |
|---:|---|
| 0 | Generate proposed requests for admission from the parsed complaint. |
| 1 | Save the selected requests and generate definitions and instructions. |
| 2 | Save the introduction, definitions, and instructions and finalize the document. |
| 3 | Download the requests as a Word document. |

Further reduction refines the generated suggestions, so all of its LLM usage is
attributed to the latest RFA step `0` row.

## Requests for Production

`docType`: `RFP`

| Step | Recorded operation |
|---:|---|
| 0 | Generate proposed requests for production from the parsed complaint. |
| 1 | Save the selected requests and generate definitions and instructions. |
| 2 | Save the introduction, definitions, and instructions and finalize the document. |
| 3 | Download the requests as a Word document. |

Further reduction refines the generated suggestions, so all of its LLM usage is
attributed to the latest RFP step `0` row.

## Interrogatories

`docType`: `ROG`

| Step | Recorded operation |
|---:|---|
| 0 | Generate proposed interrogatories from the parsed complaint. |
| 1 | Save the selected interrogatories and generate definitions and instructions. |
| 2 | Save the introduction, definitions, and instructions and finalize the document. |
| 3 | Download the interrogatories as a Word document. |

Further reduction refines the generated suggestions, so all of its LLM usage is
attributed to the latest ROG step `0` row.

## Responses and Objections to Requests for Production

`docType`: `RFP_RESPONSE`

| Step | Recorded operation |
|---:|---|
| 0 | Prepare the response workflow from the parsed RFP document. |
| 1 | Generate request-specific objections and response choices. |
| 2 | Apply the selected or custom responses and finalize the document. |
| 3 | Download the responses as a Word document. |

## Responses and Objections to Interrogatories

`docType`: `ROG_RESPONSE`

| Step | Recorded operation |
|---:|---|
| 0 | Prepare the response workflow from the parsed interrogatories. |
| 1 | Generate interrogatory-specific objections and response choices. |
| 2 | Apply the selected or custom responses and finalize the document. |
| 3 | Download the responses as a Word document. |

## Related Standalone Tool

Standalone Hallucination Guard is tracked separately with `docType`
`Hallucination Guard` and step `0`. Each backend run records its terminal status,
completion time, Celery task ID, LLM attempts, actual returned tokens, and per-model
usage. Upload and parsing tasks are not recorded as Guard runs.

## Maintenance Rule

When adding, removing, or changing a Guided Drafter analytics event, update this
file in the same change as the corresponding main-step or auxiliary attribution.
