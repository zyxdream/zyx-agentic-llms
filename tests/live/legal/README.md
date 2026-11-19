# Live legal acceptance fixtures

All organizations, people, contracts, invoices, payroll records and case facts in this directory are **synthetic test material**. No existing customer or private legal documents were copied into this corpus.

These tests are an opt-in evaluation against the current Workspace application, its real local database and its live agent workers. They are separate from deterministic mocked browser/worker regressions. Running them makes real calls to configured model, research and preparation providers and can incur charges. Run only with explicit authorization.

Recorded runs and their application identities are listed in
[the live registry](../README.md). The baseline acceptance run is
`artifacts/live-legal-20261113`; focused follow-ups are recorded separately.

## Authentication and isolation

Use the normal authenticated session of the intended Workspace account and its current CSRF token. Account setup/login remains required; do not bypass authentication, replace an account password, embed credentials, or commit cookies/tokens. The user may authorize creating dedicated acceptance projects in that account. Preserve all other accounts, projects, documents and runs.

The default independent local services are frontend `localhost:3010`, API `127.0.0.1:5010` and Redis `127.0.0.1:6390`. Confirm the current project's configuration and service ownership before running. Never point this evaluation at another project's database, environment or queues.

## Input corpus

`fixtures/scenario-prompts.json` contains scenario names, project upload lists, prompt attachment lists, initial prompts and the commercial draft revision prompt. Its file paths are relative to `fixtures/`. Upload only those explicit input lists. Scope matters: Project documents are reusable within the project, while prompt attachments remain in their conversation.

| Matter | Project documents | Prompt attachments | Focus |
| --- | ---: | ---: | --- |
| Commercial service-fee dispute | 20 | 20 | Invoice reconciliation, duplicate records, credits/cash, amendment hierarchy, interest, XLSX calculation and versioned demand revision. |
| Employment overtime assessment | 2 | 2 | Nondiscretionary bonuses, straight-time already paid, weekly overtime, conditional remedies, research and an auditable XLSX. |
| Diversity-jurisdiction intake | 1 | 1 | Nested LLC citizenship, corporate dual citizenship, known overlap, missing facts and verified primary authority. |

The commercial case has **40 short files totaling 21,488 bytes**. It tests many-source admission, retrieval and reasoning; it does **not** measure throughput of 40 large PDFs or OCR-heavy documents. Across all matters there are 46 input files. The two JSON configuration/scoring files bring this directory to 48 files.

The jurisdiction fixture is the original LLC-citizenship scenario. It is distinct from the live removal-law scenario with a New York plaintiff, Delaware/Illinois corporate defendant, a served forum defendant and the October 20 service date. Do not apply one scenario's expected answer to the other.

## Private scoring key

**Never upload `fixtures/expected.json` to the agent.** It is the independent scoring key. Do not expose it through a temporary public fixture server or include its contents in a test prompt. The same applies to evaluator notes and final reports. Spreadsheet formulas in the input workbooks are ordinary matter records; prompts ask the agent to recalculate rather than trust cached totals.

| Commercial check | Expected result (USD unless stated) |
| --- | ---: |
| Unique invoices | 20 |
| Gross invoices | 178,750.00 |
| Credits | 7,250.00 |
| Cleared cash payments | 50,000.00 |
| Unpaid principal | 121,500.00 |
| Disputed INV-018 | 11,750.00 |
| Undisputed principal | 109,750.00 |
| Simple annual contract rate | 6% |
| Actual/365 days, October 1 to November 13, 2026 | 43 |
| Interest on undisputed principal, rounded once at the end | 775.77 |
| Initial demand: all principal plus undisputed interest | 122,275.77 |
| Revised demand: undisputed principal plus interest | 110,525.77 |
| Optional settlement: 10% off undisputed principal, interest waived | 98,775.00 |

Only the executed amendment changes the original agreement's rate. The duplicate INV-007 copy, PAY-001 bank confirmation, unsigned penalty proposal and old sales estimate must not create additional debt or additional cash. All credits and cash predate the common due date, avoiding changing-principal accrual ambiguity.

| Employment check | Expected result |
| --- | ---: |
| Actual hours in eight workweeks | 353 |
| Hours over 40, measured separately each week | 35 |
| Base hourly rate | $24.00 |
| Base wages already paid | $8,472.00 |
| Nondiscretionary bonuses already paid | $706.00 |
| Total already paid | $9,178.00 |
| Regular hourly rate in each week | $26.00 |
| Additional half-time overtime premium unpaid | $455.00 |
| Conditional equal liquidated damages | $455.00 |
| Conditional combined amount | $910.00 |

The employment facts expressly stipulate FLSA coverage, nonexempt status, and straight-time paid for every actual hour, including hours over 40. Adding a further 1.5 times the regular rate would double-count already-paid compensation. Equal liquidated damages are a conditional legal scenario, not an assured recovery; applicable defenses and conditions require analysis.

## Evaluate outputs and preserve evidence

Record run IDs, project/conversation IDs, actual model/provider policy, preparation timings, source counts by scope, tool calls, clarification, completion/failure status and end-to-end runtime. Check every material figure against the key independently. Verify that research actually retrieved the cited primary authorities, and distinguish successful tool transport from accurate legal analysis.

For commercial revision, compare the previous and revised editable output and downloaded versions. Confirm the prior version remains retrievable, the new figures are correct, and unchanged facts survive the revision. Save completed answers, calculations, generated files and version metadata in ignored artifacts for audit; do not infer output quality from a completed status alone.

Do not mock model/research responses in this opt-in live round. Do not send correspondence, file court documents, or treat generated drafts as authorized real-world legal work. Temporary public fixture-serving copies should contain only explicitly selected synthetic upload inputs and should be removed after the run. Builders, previews, runtime dependencies, credentials and evaluator artifacts are intentionally absent from this reusable corpus.

## Re-running against the local app

Run `.venv/bin/python tests/live/legal/stage.py --live` from the repository root. Open the printed localhost URL in the browser where you are normally signed in. Each Start button creates a new timestamped project and makes real provider requests; staging alone makes no model calls. The page uploads only files listed in the scenario manifest, and the scoring key is not served. Keep the page open during preparation. Inspect the resulting projects in Workspace and run `scripts/live_acceptance_report.py --project-ids <new IDs>` to export scoped read-only evidence.

The original jurisdiction fixture differs from the removal scenario exercised on November 13; the run report records the exact live facts and oracle. Remove the generated `frontend/public/_live_acceptance_legal/` staging directory after finishing. Never install authentication bypasses or copy session credentials into the launcher.
