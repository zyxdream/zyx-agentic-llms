# Draft versions: 4A

## Included

Each run has one application-owned `agent_context.working_candidate_id` in its
saved SQL payload. `working_draft` resolves to this candidate, or to the run's
initial editor snapshot before the first successful draft operation.
`current_draft` remains the initial snapshot for compatibility.

Successful create/revise/restore operations save immutable candidates and advance
the working reference in the same lease-fenced transaction as the tool result.
Failures, cancellation, reading and verification do not advance it. Creating a
second independent candidate, editing an older candidate or restarting from an
upload after the first revision returns an error before the writer runs. The
current candidate's exact ID remains accepted. Verification results pin the
resolved candidate ID rather than the moving alias.

The controller receives authoritative working state on every request, including
after compaction and reload. Incomplete older runs without a saved reference
recover it from their latest successful drafting step in sequence order. Pending
old tool calls still pass the current-base checks; completed runs are not rewritten.

`complete_task(candidate_id="working_draft")` publishes only this working candidate.
Its exact ID is also accepted; an older ID is rejected. Null leaves Output unchanged.
Only final publication writes Output. Existing editor permission, ownership, version
conflict checks and atomic draft/turn/publication recovery remain in place.

## Confirmed history and rollback

`published_draft_versions` retains complete confirmed editor snapshots, separate
from intermediate candidates. The first save retains the preceding current editor,
then each explicit manual save or final agent publication adds one snapshot. Listing
history is read-only. Research, model calls and keystrokes create no history entries.
The migration adds a table without rewriting accounts, documents or existing runs.

The model can use `list_draft_versions` to obtain metadata and exact `version_id`
values for this document. `restore_draft(version_id)` is instructed for explicit
user rollback requests only. It copies confirmed content, title and export settings
into a new working candidate whose parent is the current working draft. The original
version remains immutable. The user sees the restored content only after ordinary
final publication. Identical content produces no new candidate. No new UI or approval
flow is introduced. Uploads, chat history, generated file downloads and external
side effects are outside document rollback.

Older agent publications can be recovered from durable completed conversation-turn
metadata and their saved candidates. Unpublished candidates cannot be selected as
confirmed versions. Older manual versions that were never retained cannot be
reconstructed. No candidates or historical records are removed.

## Focused editing and rewrites: 4B

Workspace revisions now default to exact text replacements for focused changes.
The existing writer call selects patch or rewrite from the requested scope. The
application validates and applies an entire patch against one base snapshot;
missing, ambiguous or overlapping targets save no changes. Full rewrites remain
available for broad transformations. Title and export metadata remain unchanged
unless requested. An unchanged result creates no candidate.

Candidates still hold complete resulting documents. Only final publication changes
Output, and existing conflict/cancellation/rollback behavior is retained. The existing
version and publication controls remain unchanged. The subsequent general drafting
update adds material purposes and shares the first-upload editing engine.
[Editing behavior, tests and remaining limits](draft-edits.md).

Deferred: fuzzy patch application, document segmentation and automatic semantic
verification. Text outside the selected patch ranges is preserved exactly; the
model remains responsible for choosing the right passages and for rewrite quality.

## 4A validation

`tests/test_working_draft.py` covers the payroll correction/citation sequence, stale
edit and publication rejection, initial uploads, extra creates, immutable report
targets, failed/cancelled/retried revisions, old-run recovery, manual snapshots,
rollback/no-op/continuous ancestry, cross-document rejection, legacy publication
proof, save conflicts and atomic publication rollback. Context tests cover reload
and compaction. Migration tests check schema parity and preservation. The existing
mocked browser and real Redis/Celery replays cover editor save/publication races,
reload, retry, downloads and recovery.

Verified for this implementation: the full offline backend suite passed **563**
tests (**16 skipped**); the real Redis/Celery replay passed **8** (**1 skipped**);
and the affected browser replay passed **9** workflows. Providers were mocked.
The migration upgrade/downgrade and schema-parity checks passed. The local
`zyx-agentic-llms.db` was backed up and upgraded to `0004_published_draft_versions`;
all pre-existing table records matched before/after hashes, retaining one account
and five documents. Database integrity was `ok`, all six owned services restarted,
and the frontend returned HTTP 200 at localhost:3010. These 4A checks did not assess live-provider quality or 4B edit generation.
The subsequent 4B checks are recorded in the editing document.
