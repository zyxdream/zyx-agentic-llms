# Case Record mixed-matter live suite

Nine conversations / ten prompts adapted from the November 19, 2026 Case Record
handoff. The suite tests mixed-matter document review, scope control, chronology,
admissions, exhibit references and contract comparison.

## Inputs

Thirteen source documents live under the ignored directory:

```text
instance/evaluation-inputs/case-record-mixed-matter/source-documents/
```

Six generated/checker files are held out and must never be uploaded:

- `Tarantino LexText Output (Answer, RFP, RFA, ROGs).docx`
- `COMPLAINT-Waymo-checker.csv`
- `Tarantino_v_Gawker-checker-nofact.csv`
- `gawker_rfp_LexIntel_draft145.pdf`
- `lexintel_waymo_rogs.pdf`
- `tarantino_gawker_RFP_14.pdf`

`RFP (Uber)-image.png` is not uploaded because the current upload policy accepts PDF, DOC, DOCX, TXT, XLSX and CSV, not PNG. It remains in the original handoff as a capability reference.

The original matter had a 21st pre-existing extracted-text source. This suite
uses 13 source documents, so “all documents” means 13, not 21.

## Projects and scopes

| Project | Documents | Conversations |
| --- | --- | --- |
| Tarantino selected | Tarantino FACTS.docx, Tarantino v. Gawker - COMPLAINT.pdf | 01, 02 + follow-up, 03, 07 |
| Mixed matter - all documents | all 13 source documents | 04, 06, 08 |
| Selected SaaS agreements | Common Paper agreement, YC Form SaaS agreement | 05, 09 |

Separate projects keep selected-scope tasks from contaminating the all-document
scope.

## Run

Use the normal signed-in Workspace browser. Create the three projects, upload
only the listed project documents, then run each conversation in order. Save
final responses, run IDs, tool errors and timings under ignored
`artifacts/case-record-mixed-matter-<timestamp>/`.

Do not upload held-out files, reviewer checks or this README. Do not treat the
other platform's answers as ground truth; evaluate against
`reviewer-checks.json` and the source documents.

## Prompt notes

Prompt 03 uses an allegation/established-fact column and makes every listed date
conditional on support. Prompt 05 is scoped to the two selected agreements.
Prompt 07 is intentionally underspecified and is a robustness retry of 03.
