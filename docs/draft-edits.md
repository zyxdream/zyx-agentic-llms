# Workspace editing: 4B

## Behavior

The existing `revise_draft` tool keeps its arguments. Within the existing writer
call, a Workspace revision returns either `patch` or `rewrite`. Focused changes
use patches by default; a broad requested transformation can use a full rewrite.
No separate classifier, routing call or mandatory verifier is added. The model
selects the method from the request and supplied canonical draft, without numeric
size thresholds. A failed patch does not automatically fall back to a rewrite.

A patch contains literal `old_text` / `new_text` replacements, plus an explicit
`replace_all` flag for global substitutions. Single replacements require exactly
one occurrence; adding surrounding text disambiguates repeated passages. Insertions
retain the surrounding anchor in the new text; deletions use empty replacement
text. Global substitutions use normal non-overlapping string-replacement semantics.
All patches refer to one immutable base. Validation rejects missing, ambiguous or
overlapping edit targets before any candidate is saved. Feedback identifies the
failed replacement so the agent can read the working draft and retry.

The application assembles the complete Markdown result, preserving every character
outside the matched ranges. Successful candidates retain the full resulting body,
editing method and replacement operations. Title/export settings and other document
metadata are preserved unless requested; title-only and layout-only revisions are
supported. Existing section-based drafts use the existing Markdown adapter on edit.
Unchanged results create no candidate or history entry. Explicit deletion may leave
an empty body. Newly created documents retain the full-result creation schema. The subsequent
[general drafting update](general-drafting.md) also routes the first uploaded-draft
revision through this same engine, using accepted extracted text.

## Continuity, saving and failure

[4A](draft-versions.md) continues to own the current candidate, ancestry, publication
and confirmed rollback. Reading or verifying does not select an older edit base.
Rollback creates a new working candidate, which can then be patched normally.
Cancellation and failed tool results cannot advance the working draft. A newer
manual editor save remains protected by the existing publication-time conflict
checks; a private candidate can finish but cannot overwrite that save.

No public HTTP/API schema, frontend or database migration is required by 4B. Existing
candidate artifacts remain readable. Full snapshots remain the storage format;
patches reduce writer output for focused edits, not retained-version durability.
Existing input/context limits still apply.

## Validation and remaining work

`tests/test_document_edits.py` exercises exact single/batch edits, insertion,
deletion, section/paragraph changes, explicit global replacements, repeated phrases,
Unicode, Markdown tables, leading zeros and code. It checks all-or-nothing rejection,
no-op saves, title/layout/metadata preservation, correction followed by citation,
rewrite followed by patch, uploaded-original follow-up, rollback followed by patch,
provider retry, cancellation and manual-save conflicts. A real Redis/Celery replay
now goes through patch generation/application before publication and DOCX download.
Browser regressions retain draft staging, editor save, conflict, reload and download
coverage.

Uploaded-first editing is now shared with 4B; see the general drafting update.
Deferred: native original-file layout editing, fuzzy matching, automatic long-document
batching and semantic verification. Exact application guarantees preservation
outside chosen patch ranges. Model selection of those ranges and the quality of
rewritten passages still require evaluation; mocked tests do not establish live
model quality or latency.

Verified in this pass: **604 backend tests passed** (**16 skipped**), **8 real
Redis/Celery replays passed** (**1 skipped**), and **9 affected browser workflows
passed**. All provider boundaries were mocked. The worker replay performs an actual
patch-based Workspace revision before final publication and DOCX download. No
frontend build or live-provider quality/latency claim is made by these checks.
