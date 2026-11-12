# Production document preparation

For the new version-1 analysis/drafting agent, start with
[the agent controller design memo and runbook](AGENT_CONTROLLER.md). Document
preparation described here remains active for both execution versions. The
fixed prompt planning/extraction/chord/answer descriptions below apply to saved
version-0 requests; new submissions use `agent_tasks.advance` and callable
operations. Source, snapshot, and conversation helpers remain shared.

Case Assistant attachments and Case Background always use durable preparation:
`queued → parsing → indexing → summarizing → ready`. The former
`DOCUMENT_PREPARATION_ENABLED` flag is ignored. There is no legacy ingestion
fallback when a dependency is unavailable. Doc Search keeps its separate pipeline.

## Cutover

1. Pause new Case Assistant/Case Background uploads and new prompt admissions at
   the deployment ingress. Drain legacy upload/metadata jobs and in-flight chat
   turns before replacing the workers. Do not mix legacy ingestion writers with
   preparation workers.
2. Activate Conda `lexintel`, then apply `flask --app backend.app:app db upgrade`
   against the intended deployment database. This includes revisions
   `d5e6f7a8b9c0`, `e6f7a8b9c0d1`, and `f7a8b9c0d1e2`. Startup does not create tables or backfill.
3. Configure private persistent `FLASK_DOCUMENT_PREPARATION_SOURCE_DIR`, shared
   by web/document workers at the same path. The default is the Flask instance
   directory's `document_sources`. Multi-host installations require shared
   storage; this implementation has no object-storage adapter.
4. Start `python -m backend.document_preparation_worker --loglevel=INFO`.
   It consumes `<CELERY_QUEUE>.documents` by default, with concurrency four and
   prefetch one. Override the queue with `FLASK_DOCUMENT_PREPARATION_QUEUE`.
   Bound total concurrency across all worker instances, not just each process.
5. Start `celery -A backend.make_celery.celery_app beat --loglevel=INFO`.
   Reconciliation runs every 30 seconds. Web, workers and beat must use the same
   database, source directory and queue configuration.
6. Run `flask --app backend.app:app prepare-documents-backfill`, optionally with
   `--project-id <id>`. Existing document IDs/history are preserved. Original
   source bytes are reparsed where available; otherwise canonical text is reused
   with an explicit original-coverage-unverified warning. Re-running is safe.
7. Wait for active backfill stages to drain. Investigate failed documents. Check
   database schema, MySQL FULLTEXT indexes, worker queue consumption, cross-host
   source access, and representative live Luna output before resuming traffic.
8. Deploy the matching frontend/backend and resume admissions. Confirm aggregate
   status returns `enabled: true`; uploads use bounded groups; direct prompt
   requests reject documents whose preparation is incomplete or failed.

The `FLASK_` configuration prefix still applies to operational policies; the old
feature flag does not control product behavior. Missing preparation tables or
unavailable status produces an explicit unavailable response and blocks Send.

### Attachment interface and older documents

The prompt composer shows filename chips only. Aggregate preparation status
drives the existing muted/spinning chips, ready blue chips, and failed red chips.
Case Background files stay in their existing list rather than appearing as new
prompt attachments. Preparation failures use grouped toast notifications with a
**Review files** action. Failed chips and Case Background filenames also open
this exception dialog for Retry, exclusion or removal. Partial or unverified
extraction uses available text automatically, without an approval step or upload
notification; genuine extraction limitations remain in file details and model
context. Ready chips are blue regardless of those limitations.

Removing coverage approval requires matching backend/frontend processes and a
browser reload, but no migration, document backfill or repeated Luna preparation.
Existing ready artifacts remain usable.

Schema migrations do not prepare old documents. Run the one-time backfill above
with document workers active, optionally scoped by `--project-id`. Stored text
can be reused when originals are unavailable, with unverified coverage retained
as information rather than requiring user acknowledgment. Opening a matter and
polling never start bulk preparation.
Explicit Retry can register one older file through the same idempotent backfill
path. Remove handles old files even before registration; matter deletion retains
the existing confirmation and applies to Case Background, while exclusion only
changes the prompt's scope.

## Preparation and recovery

The attachment parser preserves numbers, text in document margins, DOCX tables,
headers/footers and footnotes. It stores ordered block locations and extraction
outcomes. PDF pages are classified as native, OCR, verified blank, partial or
unreadable. Genuine extraction limitations and unsupported embedded objects are
recorded for reference. Vector drawings alongside readable native PDF text do
not establish missing text and are not reported as extraction gaps. Historical
vector-only warnings are filtered when read without modifying stored artifacts.
Search chunks preserve contiguous canonical text and bound tokenization even
for a long paragraph with no word boundaries. DOC extraction and legacy text without original evidence can have
unverified coverage. OCR text coverage does not guarantee semantic fidelity.

PDF parsing checkpoints pages, with at most eight new pages per task. Luna work
checkpoints orientation, section and reduction results, with at most one provider
operation per task. A continuation releases its lease and returns to the document
queue. Completed work is reused by source, parser, model, schema, prompt and
preparation configuration. Leases/generations fence cancelled and stale workers.
Database transactions are closed before provider calls.

Luna uses medium reasoning, approximately 28K-token source cores with about 512
source tokens of overlap, coherent boundaries, and bounded-memory tokenization.
Source inputs stay within 32K tokens; framing/schema/orientation count toward a
40K input ceiling. Reducers have a 48K ceiling. The generated-token allowance is
32,768 including reasoning; stored section records are bounded to 6K tokens.
Oversized/incomplete section output is subdivided rather than truncated. Reducers
retry with stricter output targets and fail explicitly if no valid record fits.

Immutable artifacts retain detailed digests (normally 1–2K tokens), previews
(200–400 tokens, shorter for short sources), key information, section references,
source maps, model/version settings and coverage. Compact cards and coverage
counts are stored separately so aggregate polling does not read source maps or
return detailed digests. Rich v2 metadata has no three-sentence restriction.

Summary format revision 3 removes model-generated quotations and exact source
references from Luna preparation. The prompt requires source-only summaries with
names, dates, amounts, qualifications, allegations/findings and uncertainty kept
distinct. These summaries guide file/section selection; they are not verified
answer evidence. Code attaches the source ranges read for each section and
definition, including definitions supplied from earlier orientation sections.
Prompt-specific answering reads original text. Older saved extraction requests
still validate their quotations; new compact findings validate source block IDs
and are explicitly paraphrases, not verified quotations.
The summary revision participates in checkpoint keys, so incompatible summary
work is regenerated on Retry while parsed text and indexes are reused. Existing
ready artifacts and their older block references remain usable; no migration or
mandatory re-preparation is required. Deploy matching web/document/extraction
workers, retry a few failed documents and inspect their summaries, then use Retry
or the existing backfill command for remaining failures. Backfill skips current
ready artifacts and reuses active work.

Ready means all readable text has been indexed and summarized successfully.
Extraction limitations remain informational. Usable text proceeds automatically,
including after reload or in another browser. Legacy approval fields are ignored;
new prompts do not store or require approvals. Actual unreadable content continues to
qualify completeness and absence claims in answers and edits.
Retry is available for failed preparation and ready documents with extraction
gaps. Missing original bytes require reselecting the file. Successful page and
summary checkpoints are reused where valid; failed extraction pages are retried.

Source bytes are retained for seven days after terminal preparation by default;
canonical text, source maps and artifacts persist with the document. Retries that
need expired originals require re-upload. Removing a document fences work and
removes its canonical file; source cleanup follows retention. Do not restore old
writers as a rollback strategy; pause admissions and preserve the new records.

## Prompt scope and retrieval

Snapshot authorization and provider context checks in this section remain
shared. The planner batching, extraction, review, and reduction schedule below
describes version 0. Version 1 chooses `search_sources`/`read_sources` explicitly;
it does not automatically run that entire old retrieval schedule.

Snapshot v2 binds exact document IDs, source generations, immutable artifact IDs,
preparation version and exclusions to the authorized user/chat.
Admission, execution and confirmation resume revalidate it. New uploads do not
expand an accepted turn. All retrieval is restricted to exact snapshot IDs.

The planner sees cards grouped as current attachments, prior attachments and
matter background (450 tokens/card; 64K catalogue budget; larger catalogues batch).
Raw source and structured section search supplement planner candidates. Detail
review batches have at most eight documents and 48K input tokens. Weak/no-hit
selection scans remaining section summaries/facets. Focused evidence reads use
original passages, neighboring context and definitions; exhaustive work visits
every requested source range. Evidence caches include scope/artifact/model/query.

Shared textual Responses adapters preflight known model context limits, including
schemas and output reserves. Prepared source context is never trimmed by
beginning/middle/end sampling. New submitted prompts save original source ranges in SQL and assemble the
readable text directly when it fits. Only oversized exhaustive requests extract
compact findings. Original passages and complete saved findings remain available
in the authorized, paginated evidence report. Exact-source edit operations retain their source; if the exact draft
alone exceeds the edit window, the turn fails explicitly and requests a targeted
section rather than silently editing sampled text. Existing preserve-original
anchored editing uses one combined edit payload. Multiple edit batches are a
deferred extension described in [the editing plan](BATCHED_DOCUMENT_EDITING_PLAN.md).

## Legacy version-0 Case Assistant extraction tasks and saved progress

This section describes the retained fixed workflow. Version-1 requests instead
save `AgentStep` / `AgentArtifact` and continue through `agent_tasks.advance`;
see [the active code reading order](AGENT_CONTROLLER.md#active-entry-point-and-reading-order).
Shared ownership, status, and publication helpers are still required by both.

### Reading the backend code

Follow these functions in order when studying a submitted prompt:

1. `views.everything_api` builds the authorized document snapshot and calls
   `case_assistant_workflow.submit`. This commits a `CaseAssistantWorkflow` SQL
   row before publishing a task. Its ID is the `workflow_run_id` the browser
   receives and continues polling across retries and reloads.
2. `case_assistant_tasks.plan_prompt` calls the existing
   `everything._everything_phase1_body` to classify the request and select files.
   `plan_sections` saves the required source ranges as `CaseAssistantWork` rows.
   Each row points back to the prompt through `workflow_run_id`.
3. `dispatch_group` creates Celery signatures for `process_section` and a chord
   callback, `after_extraction`. A signature describes a future task invocation;
   publishing the chord schedules its group, then the callback after success.
   Every task has a separate Celery ID and execution timeout.
4. Original ranges are saved ready in `CaseAssistantWork.material_json`, with
   `result_id=NULL`, without model calls or duplicate summary records. When extraction or
   weak-search review is needed, `process_section` reads bounded source and saves
   findings or search guidance in `DocumentSummarySegment`, links
   `CaseAssistantWork.result_id`, and
   commits the section's `ready` status with that link. Redis carries small
   result references; the SQL records retain the actual evidence and progress.
5. `after_extraction` checks SQL again. Split sections require another group;
   weak focused searches may search once more as described below. Oversized
   analytical findings may require one consolidation round. `prepare_answer`
   saves a bounded `file_extractions_str` while original results remain available
   through `case_assistant_routes.evidence`.
6. `answer_prompt` calls `everything._everything_phase2_body` with that saved
   evidence. Preserve-original editing receives it in both
   `select_preserve_original_targets` and `edit_preserve_original_blocks` as
   read-only `supporting_evidence`. The primary draft's text is loaded separately.
7. `case_assistant_routes.progress` reads aggregate SQL status. Independently,
   `reconcile_case_assistant_workflows` marks expired attempts and missing
   dispatches failed. Progress reads apply the same deadlines even if workers
   are stopped. Neither check schedules work; only explicit Retry resumes it.

### Original source context and conditional follow-up search

New admissions save server-owned `_source_context_version=1`. Earlier requests
without this version retain their previous extraction and review behavior,
including through confirmation and explicit Retry. No migration, new worker,
document re-preparation, or context-memory change is required.

Direct-source work stores its document/artifact IDs and character positions once,
in `CaseAssistantWork.material_json` (`context_method=source`, `format_version=1`).
The ready state does not require a generated result. `source_context.read_work_value`
reads these positions for final context, retrieval review and the evidence viewer.
Actual findings, review results and legacy quotes still require a valid
`DocumentSummarySegment` through `result_id`. Existing range-only result links
remain readable only when their document and positions match the work record.

After deploying matching web, main and extraction workers and draining old prompt
workers, the following one-time maintenance command reports duplicate range-only
results. Run from the repository root in Conda `lexintel`:

```bash
flask --app backend/app.py case-assistant-cleanup-source-ranges
```

This defaults to dry-run. To apply the verified cleanup:

```bash
flask --app backend/app.py case-assistant-cleanup-source-ranges --apply
```

The command clears old `result_id` links and deletes only exact
`original-ranges-v1` position records. It retains the work rows, document text,
all six tables, and real summaries/findings. It skips running requests, pending
confirmations, mismatched references and unexpected formats. Records with more
than 1,000 references are also skipped for manual review. Output reports counts;
warnings identify skipped result IDs and reasons. Scanning uses 100-record pages
by default (`--batch-size` accepts 1–1,000); each eligible deletion and its link
updates commit atomically. Re-running is safe and can clean previously active
requests after they finish. Cleanup never runs on startup or during polling.

`source_context.plan` chooses the minimum necessary processing:

| Request | Context supplied to answering or drafting |
| --- | --- |
| Whole-document comparison whose original text fits | Complete original text once, with compact filename/page labels; no extraction, review or reduction calls. |
| Focused question | Original matching passages, neighboring context and located definitions. |
| Exhaustive request whose complete original text cannot fit | Bounded extraction of concise findings, with original block IDs and code-supplied locations. |

The saved 64K budget applies across all supporting documents and includes labels,
coverage warnings and unresolved questions. Overlapping ranges are merged within
each document/artifact; identical text in different documents is not discarded.
Focused ranges are split into approximately 1K-token windows with bounded
neighbors (at most 1,536 tokens per window). A matching 28K preparation section
identifies where to search; it never automatically contributes its whole text.
Each document is read separately during assembly rather than retaining all 100
complete documents in memory. Budget omissions are explicitly disclosed in the
final context; focused retrieval never claims exhaustive coverage.

For example, the saved three Waymo/Uber pleadings contain approximately 47.4K
source tokens. Compact filenames/page labels and coverage notices yield less
than 50K supporting tokens. These go directly to drafting without producing a
larger collection of model-copied quotations. This offline fixture verifies
assembly and dispatch, not the quality of a new live answer.

Focused selection keeps three section hits and twelve original-block hits per
document. Existing saved hit IDs count toward later limits. Ordinary successful
searches proceed directly to answering. Only weak searches—no original matches,
low selection confidence, an unlocated explicit page/paragraph, or clipped source
boundaries—require `retrieval_review` work. Empty results cannot establish that
an exception does not exist. There is no automatic full-document extraction
fallback for focused requests.

`after_extraction` calls `retrieval_review.advance`, which saves bounded work and
returns. `process_section` reviews the affected original passages using saved
work references. Review guidance is not answer evidence. Review inputs remain
at most 40K estimated tokens and stored outputs at most 4K. Larger reviews use
bounded batches with child references. Failures require explicit Retry.

When needed, at most six targeted queries search original text and section
records separately. Round-robin merging retains initial hits and fills up to
six section hits and twenty-four block hits total per affected document. New
supporting documents must already be in the authorized prompt snapshot; they
cannot change the primary draft or editable targets. Uncovered original ranges
are saved ready without another quotation-extraction call. There is one expansion
round and no mandatory second review. Remaining questions reach the final model.

For oversized exhaustive requests, disjoint required source ranges cover all
readable text; overlap is context only. Fully framed extraction inputs remain
at most 40K tokens. Findings target 1–2K tokens and are capped at 6K including
references. Shorter source ranges have a smaller output cap, based on their source
tokens with a 256-token floor for schema/reference overhead. Incomplete or oversized output subdivides required work rather than
truncating it. Block IDs are checked against supplied sources; findings are
explicitly paraphrases, not verified quotations. Complete results remain in SQL.
An analytical result may use one bounded consolidation round. If a required
complete list still cannot fit, fail clearly with a request to narrow the scope;
do not silently drop items or repeatedly reduce them.

Progress displays “Checking extracted evidence” or “Searching additional source
passages.” The evidence endpoint includes retrieval state and remaining issues;
the UI displays these separately from quoted source passages. Empty results
cannot establish absence in unreadable or unsearched content.

Pause prompt admissions while deploying matching web, main and extraction
workers; all must understand the new source-context version. The retrieval-review
override only disables review and does not revert the new source format. Only
newly accepted requests adopt the source-context policy.
Requests without the source-context version keep the earlier extraction path;
the saved review override controls review only. Both policies persist through
confirmation and explicit Retry. No migration or
document re-preparation is needed. To roll back, disable new admissions to this
policy and let its active requests drain on compatible workers.

In staging, compare identical multi-document questions before/after expansion:
exceptions outside the first 3/12 hits, paraphrases and separate amendments.
Record additional validated passages, citation accuracy, remaining issues,
provider calls, token usage and elapsed time. Include 100-file questions and
worker loss followed by explicit Retry. Passing offline tests does not replace this live quality and
latency comparison before production rollout.

`case_assistant_workflow.execution` connects the task functions with existing
nested helpers. Its `@contextmanager` decorator supports this pattern:

```python
with service.execution(workflow_id, token, work_id=section_id):
    result_id = extraction_result(row, item, payload)
```

Before `yield`, it installs the IDs and ownership token in a Python `ContextVar`.
While the block runs, nested functions can read them through `current_execution()`.
The `finally` block restores the previous context on normal exit or an exception.
The ContextVar restoration marker is different from the SQL `lease_token`.
This function does not create a task or a database transaction. It does not
transfer context between workers; each task enters its own `execution` block.

The actual checks happen in `before_call`, `get_openai_client`, `save_segment`
and `guard_chat_write`. They read the SQL row identified by that context and
check whether the attempt may continue. A lease token identifies one worker
attempt until `lease_until`; a replaced token invalidates an old worker. SQL
row locks protect short updates until commit/rollback. Neither the lease nor
the ContextVar requires holding a database lock during a provider call.

`guard_chat_write` deliberately leaves its final locks open for
`DBMemory.__setitem__` to commit chat state and publication metadata together.
`task_llm_usage_scope`, often next to `execution` in the same `with` statement,
has a separate purpose: attributing model usage to the chat's analytics.

### Deployment and explicit Retry

Drain existing combined `everything_turn_task` and confirmation tasks before
cutover. Revision `f7a8b9c0d1e2` adds `case_assistant_workflows` and
`case_assistant_work`. Deploy the matching API, tasks and frontend together;
there is no fallback to an unsaved combined task when these tables are missing.
Existing task entry points remain available for already-queued legacy work.

The API saves the prompt and authorized document snapshot under `workflow_run_id`
before dispatch. A unique `active_draft_id` permits one active prompt per chat.
Planning saves its classification and reuses completed relevance reviews; each
planning continuation performs at most one new relevance-provider operation.
Required source sections are saved before their Celery chord is submitted.
Legacy extraction tasks save validated exact quotes; new extraction tasks save
compact findings with source block IDs in
`DocumentSummarySegment`; the chord carries only small result references.
Empty excerpts mean a completed section without responsive passages, not failure.

Start these worker roles, plus the existing document preparation worker:

```bash
conda activate lexintel
celery -A backend.make_celery.celery_app worker --loglevel=INFO --hostname=chat@%h
python -m backend.case_assistant_worker --loglevel=INFO --hostname=extraction@%h
celery -A backend.make_celery.celery_app beat --loglevel=INFO
```

The main worker consumes the default queue for planning and answering. The
extraction worker consumes `<CELERY_QUEUE>.extraction`, with concurrency four
and prefetch one. Beat publishes failure checks every 30 seconds to the main
queue; the progress endpoint also checks deadlines without a worker. There is
no separate recovery queue or worker. Run only one beat scheduler per deployment.
Bound total extraction concurrency across all hosts, not just within each worker
instance.

For this change, stop any old recovery worker and restart matching main and
extraction workers plus Beat so the updated schedule uses the main queue.
The old periodic task name remains compatible with queued messages, but its
implementation only fails stalled requests. Existing stalled requests are never
automatically resumed. No new schema migration or backfill is required here;
document preparation and its existing reconciliation behavior are unchanged.

Supported Flask-prefixed configuration:

| Setting | Default |
| --- | --- |
| `FLASK_CASE_ASSISTANT_EXTRACTION_QUEUE` | default queue + `.extraction` |
| `FLASK_CASE_ASSISTANT_REQUEST_SECONDS` | 21600 (six-hour overall deadline) |
| `FLASK_CASE_ASSISTANT_QUEUE_WAIT_SECONDS` | 1800 (30 minutes without worker progress) |
| `FLASK_CASE_ASSISTANT_EVIDENCE_TOKENS` | 64000 (combined evidence budget before aggregation) |

New prompts save the effective evidence budget when accepted. The default 64K
applies to answers, drafting and edits, including quotes, source references and
coverage notices. Existing requests retain their saved budget during failure checks,
confirmation and Retry. No schema migration or document re-preparation is needed.
When deploying, check that an existing explicit 24,000 override is removed or
updated to 64,000. Setting this override back to 24,000 rolls back new admissions
without changing accepted requests.

Each task retains the 1200-second soft and 1260-second hard execution limits,
separate from the whole prompt deadline. Provider calls inside Case Assistant's
`execution()` context explicitly use `timeout=None`, allowing long reasoning
responses without the previous four-minute network cutoff. This applies to
planning, document selection, extraction, review, reduction, answering, drafting
and editing. All calls and provider retries within a task share its Celery
execution allowance; each call does not receive a fresh 20 minutes. The soft
limit propagates to task failure handling; the hard limit stops a stuck process.
Calls outside this context, including Luna document preparation, keep their
existing client settings. Run these bounded tasks with the prefork worker pool
and verify effective task limits after deployment. Celery does not retry
prompt-task exceptions or redeliver prompt tasks after worker loss. Reported
exceptions and soft/hard timeouts fail the saved request immediately. If a worker
disappears without reporting failure, its 1320-second SQL ownership deadline
(beyond the hard task limit) fails the request. Missing dispatch/queued work fails
after the existing 1800-second queue-wait deadline, or the overall deadline if
earlier. Successful planning continuations, subdivisions and reductions schedule
their next bounded tasks normally, with new task IDs; these are not error retries.
Redis chord-result expiry stays longer than the configured request lifetime.

The answer callback checks SQL completion and the current group ID. Duplicate
or replaced tasks cannot overwrite current attempts. Explicit Retry creates a
new chord only for unfinished work; its workflow ID stays the same. Cancellation
is saved before revocation and checked before result and chat publication.
Source versions and the current draft are revalidated before publication.
Planning finishing does not release the active chat; completion, failure,
cancellation or the overall deadline releases it.

Final answer and edit size checks propagate `ContextBudgetExceeded` to
`answer_prompt`, which lowers evidence budgets to 32K, 16K, 8K, then 4K
from the default 64K. New focused requests repack saved original windows; full
source that cannot fit switches to bounded findings. Findings permit at most one
consolidation round. Saved original ranges and findings remain
available in the report. Draft preflight also checks the same request without
supporting evidence. If the exact source or fixed instructions cannot fit,
`can_reduce_evidence=False` stops the request with a narrower-scope message.
Budget failures do not publish a generic failed answer while reduction remains
possible. Legacy requests retain their existing reduction-round and progress checks.

For example, approximately 60K of assembled evidence now reaches the final
prompt without reduction. Complete original source above 64K uses bounded findings extraction; focused
passages share the total budget and analytical findings allow one consolidation round. The final request must still fit its branch's complete-input budget;
preserve-original editing retains its 104K ceiling including exact draft text,
instructions and schema. Preparation, retrieval limits and provider timeouts
are unchanged. Before rollout, compare representative 24K and 64K runs for
retained facts, citation accuracy, reduction frequency, token usage and latency.

The frontend polls saved status, restores it after reload, and preserves the
request ID through temporary connection failures. Polls return counts and
per-file failures, never passage bodies. Source coverage uses the union of
completed character ranges so overlap is not counted twice. Failed requests and
their evidence reports survive reload. Supporting passages, filenames, artifact
IDs and source locations reach both target selection and preserve-original
editing as read-only evidence. Only selected primary-draft anchors can be
replaced. Full prompt framing and schemas count toward edit budgets; large edit
operations still require a targeted section.

Offline verification, inside Conda `lexintel`:

```bash
TIKTOKEN_CACHE_DIR=/path/to/local/tiktoken-cache python -m pytest -p tests.offline_guard tests
LEXINTEL_RUN_CELERY_INTEGRATION=1 python -m pytest -p tests.offline_guard tests/test_case_assistant_workflows.py -k native_redis
```

The opt-in test starts its own temporary Redis and real Celery worker with
synthetic sources and mocked model responses. It never uses a deployment broker
or provider, and requires permission to create a local socket. Browser checks
use `npm run test:e2e -- case-assistant.spec.js` from `frontend/lex-intel`; set
`PW_PORT` to an unused port to keep an existing development server running.
Validate production Celery/Redis versions, MySQL concurrent claims, and live
extraction latency and quality in staging before resuming admissions. SQLite
and mocked-provider tests do not establish those production properties.

## Policies and verification

Upload quotas and preparation reconciliation below concern document preparation.
Agent task dispatch has its separate explicit-failure/Retry behavior described
in [the agent memo](AGENT_CONTROLLER.md#current-rules-and-limitations-to-keep-visible).

- Chat: 100 documents, 50 MiB/file, 1 GB (1,073,741,824 bytes) original bytes total.
- Matter background: 30 documents, 100 MiB/file, 1 GB original bytes total.
- Groups: five files / 100 MiB, with two browser transfers at once; multipart body
  guards allow another 2 MiB of framing. Quotas include pending/failed reservations.
- Reconciler: 30 seconds; unpublished dispatch retry: 60 seconds; untransferred
  admission expiry: one day. Four expired worker attempts require explicit retry.
- E2E queues remain inside their isolated namespace. Align proxy limits/timeouts
  with bounded transfers; do not raise multipart limits to accept an entire corpus.

Run Python tests inside Conda `lexintel`, with `tests.offline_guard` for offline
verification. Tests cover parser content, Unicode/core coverage, resumable units,
publication, best-effort extraction readiness, exact scope, retrieval beyond previews, migrations,
quotas, and browser reload/Send behavior. Mocked tests establish workflow behavior;
production MySQL concurrency and live Luna fidelity, cost and throughput require
staging measurements with representative document lengths. Do not infer those
properties solely from passing unit tests.

An opt-in live check uses synthetic documents and the application's configured
API credentials, without database writes:
`python -m scripts.check_document_preparation_luna --long` (inside `lexintel`).
The short contract checks payment/cap amounts, exceptions and allegation status;
the 32K-token fixture checks a middle-document amount, deadline, fraud exception
and a definition used before its declaration. It prints coverage, usage
and elapsed time. This is a smoke check, not a 100-document capacity benchmark.

## Conversation context and follow-up requests

The conversation builder is shared by both execution versions. Version 1 calls
it from `agent_tasks.initialize` and saves its result as the agent's initial
context. See [the agent memory notes](AGENT_CONTROLLER.md#context-and-memory-what-is-saved-and-what-the-llm-sees)
for within-assignment history replay and the remaining gap in carrying agent
clarification answers into later assignments.

New admissions save `_conversation_context_version=1`. Version-0 planning assembles and
checkpoints one conversation context for classification, document selection and
final answering: a compact brief/constraints (4K), the latest two exchanges (8K),
and relevant older passages (4K). Oversized exchanges are summarized from complete,
bounded inputs; ordinary follow-ups do not require a new summary call. Completed
summary steps survive planning continuations. Background brief updates record
which exchange they cover and cannot overwrite a newer brief.

Raw exchanges remain in completed request records. Legacy exchanges leaving the
100-turn workspace window are retained in existing `TextEntry` records. This
preserves available history going forward; already-discarded exchanges cannot be
reconstructed. No migration or backfill command is needed.

The following `context_policy` routing describes version 0; version 1 selects
explicit tool calls/material references rather than invoking this fixed policy.
`context_policy=reuse` reads the previous request's included source ranges after
checking the current chat, owner, document versions and exclusions. Empty retrieval
queries mean reuse only; targeted queries add newly retrieved ranges. `refresh`
searches for the new topic. Generated findings keep their question-specific cache
keys; follow-ups reference the saved result and read its original source blocks.
The 64K supporting-context budget and final request size checks remain in force.
Deploy matching backend processes; accepted requests without the new policy keep
their prior workflow behavior. No additional worker or automatic recovery is added.
