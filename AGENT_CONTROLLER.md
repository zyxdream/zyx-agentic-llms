# Agent controller: design memo and runbook

This is the working reference for the analysis and drafting agent on this branch.
It describes current code, inherited behavior, and remaining work separately.
The comments/documentation update on 2026-11-02 added no execution changes.
The 2026-11-05 working-context update adds application-managed compaction within
an assignment, with the explicit context defaults below. The implementation
remains local until separately deployed.

For exact commands on this Mac, use
[Update and restart the normal local server](#update-and-restart-the-normal-local-server).

## Terms and responsibility

| Term | Meaning here |
| --- | --- |
| Assignment / workflow | One submitted user request, identified by `CaseAssistantWorkflow.id` (`workflow_run_id`). Several assignments can belong to the same chat. |
| LLM | The language model called through the Responses API. It selects tool names and arguments. A database record or Pydantic argument schema is not the LLM. |
| Agent controller | `backend/agent_tasks.py`: asks the LLM, executes validated requests, saves results, continues, pauses, or publishes. |
| Tool / operation | An application capability described to the LLM by a name and JSON argument schema, then executed by our Python code. OpenAI does not supply the Python body of `Operations`. |
| Celery task / worker | A task is one scheduled function invocation. A worker is the running program that receives and executes tasks. |
| Redis / SQL | Redis delivers Celery messages and coordinates legacy chords. SQL retains the application request, ownership, history, results, and progress. |
| Artifact | A saved source excerpt, analysis, candidate draft, or verification report belonging to one assignment. |

The LLM decides **what to do next**. Celery schedules **where/when Python runs**.
SQL connects separate task executions. These responsibilities can coexist; an
agent loop does not require either replacing Celery or running one long `while`.

## Active entry point and reading order

For a new Case Assistant request, follow these existing functions in order:

| Order | File and functions | What to learn |
| --- | --- | --- |
| 1 | [backend/views.py](backend/views.py): `everything_api` (`POST /api/everything`) | Authorize the chat, build the document snapshot, submit, dispatch, return the workflow ID. The HTTP request does not wait for drafting. |
| 2 | [backend/util/document_preparation.py](backend/util/document_preparation.py): `build_snapshot`, `validate_snapshot` | Freeze accepted document IDs, generations, artifacts, exclusions, and user/chat scope. |
| 3 | [backend/util/case_assistant_workflow.py](backend/util/case_assistant_workflow.py): `submit`, `prepare_agent_dispatch`, `execution`, `validate_execution`, `guard_chat_write` | Save the version-1 request and pending delivery; enforce ownership, deadlines, cancellation, and guarded publication. |
| 4 | [backend/case_assistant_tasks.py](backend/case_assistant_tasks.py): `dispatch_planning` | The shared routing point: version 1 goes to the agent; version 0 retains fixed steps. |
| 5 | [backend/agent_tasks.py](backend/agent_tasks.py): `advance`, then `initialize`, `model_request`, `dispatch`, `check_editor_write`, `publish` | Understand one step first, then its initial context, history replay, next-task scheduling, and guarded completion. |
| 6 | [backend/util/agent_context.py](backend/util/agent_context.py): `prepare_input` | Build the next LLM input from full SQL history or a saved working summary plus recent complete decisions/results. |
| 7 | [backend/ai/agent_tools.py](backend/ai/agent_tools.py): `TOOL_TYPES`, `definitions`, `arguments`, `Operations.execute`, then individual operations | Connect the LLM's function call to argument validation and real Python execution. |
| 8 | [backend/util/source_context.py](backend/util/source_context.py): `selected_spans`, `document`, `format_document`; [prepared_retrieval.py](backend/util/prepared_retrieval.py): `authorized_artifacts` | Existing source selection and authorized original-text reads reused by the tools. |
| 9 | [backend/everything.py](backend/everything.py): `run_draft_operation`, `OperationMemory`, `_everything_phase2_body`, `save_state_to_memory` | Existing drafting remains active; the adapter stages its output rather than publishing. |
| 10 | [backend/ai/base.py](backend/ai/base.py): `responses_completion`; [token_budget.py](backend/ai/token_budget.py) | Provider calls, existing retries, and full-request context estimation/validation. |
| 11 | [backend/models.py](backend/models.py): `CaseAssistantWorkflow`, `AgentStep`, `AgentArtifact`; [conversation_context.py](backend/util/conversation_context.py): `for_request`, `build` | What persists and how earlier conversation enters a new assignment. |
| 12 | [backend/case_assistant_routes.py](backend/case_assistant_routes.py): `progress`, `agent_artifact`, `confirm`, `retry`, `cancel` | Observe saved state, read artifacts, answer questions, or resume/stop work. |

Frontend reading follows `EverythingDrafter.js` (submission/polling/restoration),
`AgentProgress.js` (steps and saved results), and `RequestUserInputPanel.js`
(clarification). All three live in `frontend/lex-intel/src/everything/`.

## Old fixed workflow and its replacement

The saved `execution_version` selects the path. New `submit()` calls explicitly
save **1**. Existing rows retain **0** through the migration's default; retries
and confirmation do not turn old requests into new agents.

```text
Shared HTTP entry -> submit -> dispatch_planning
    version 0 -> plan_prompt -> dispatch_group/process_section chord
              -> after_extraction -> dispatch_answer -> answer_prompt
    version 1 -> agent_tasks.dispatch -> advance -> dispatch -> advance ...
```

The version-0 schedule is superseded for new requests, but is still used by old
saved requests. Do not delete whole files simply because they contain old steps.

| Responsibility | Old version 0 | New version 1 | Relationship |
| --- | --- | --- | --- |
| Choose the next action | `plan_prompt` calls `_everything_phase1_body`; saved classification drives the fixed schedule. | `model_request` asks the LLM using the goal and observed tool results. | Replaces orchestration; the old classifier is bypassed for new requests. |
| Find/read evidence | Planner plus `CaseAssistantWork`, `process_section`, and conditional retrieval/reduction helpers. | LLM selects `search_sources` / `read_sources` and can inspect results again. | Reuses source helpers, not the entire old planner or its automatic review/reduction schedule. |
| Wait and continue | `chord(group(signatures), after_extraction...)` waits for the fixed parallel group. | A saved tool result is replayed to the LLM; `dispatch` queues the next `advance`. | Both continue after results, but there is no one-to-one replacement of the chord callback by an LLM call. |
| Generate/revise | `answer_prompt` calls `_everything_phase2_body`. | `Operations.draft_operation` calls `run_draft_operation`, which calls the same `_everything_phase2_body` on private state. | Reuses existing generation/revision implementation. |
| Check a draft | Checks within existing code or requested workflows. | `verify_draft` saves findings for another LLM decision. | A separate callable operation, with no forced verify/revise cycle. Legal lookup/search/reading are optional agent tools; legacy publication retains its citation annotation. |
| Publish | Final answer task generates and saves the chat/editor result. | Draft tools save candidates; `complete_task` calls `publish` to apply the chosen candidate and final answer. | Separates performing an operation from finishing the assignment. |
| Save progress | `CaseAssistantWorkflow` + `CaseAssistantWork` + extraction results. | Shared `CaseAssistantWorkflow` + `AgentStep` + `AgentArtifact`. | Different operation records; same request ID and shared ownership/publication guards. |

### Legacy chord pattern

Simplified from `case_assistant_tasks.dispatch_group`; IDs, SQL claims, queue
settings, error callbacks, and retry options are omitted here, not removed from code:

```python
signatures = [process_section.s(item.id) for item in pending]
callback = after_extraction.si(row.id, group_id)
chord(
    group(signatures),  # Process document sections in parallel.
    callback,          # Run after all section tasks succeed.
).apply_async()
```

`.s()` creates a description of a future task call. `.si()` ignores the group's
returned result list: `after_extraction` reads saved SQL records. A failed
section can prevent that success callback from running; existing error handlers
and deadline checks expose the failure. The callback may schedule more fixed
work before the answer. It is application scheduling code, not the LLM.

### Current agent continuation pattern

Simplified from `agent_tasks.advance`; `service` is the imported
`case_assistant_workflow` module:

```python
# Each advance() execution performs either:
#   1. An LLM decision, or
#   2. The requested tool execution.

# The current transaction already contains the completed step result/artifacts.
# Prepare the next expected task ID, pending delivery, and lease release.
next_task_id = service.prepare_agent_dispatch(row)

# Save completion AND continuation together.
db.session.commit()

# Send that saved task ID; recovery can resend it if delivery is interrupted.
dispatch(row, task_id=next_task_id)
```

**Replaced earlier version-1 sequence:** the old code committed the step, called
`release_workflow()`, then let `dispatch()` allocate the next ID in another
transaction. A process loss between those operations could strand saved progress.
The current agent commits those SQL changes together. The shared release helper
still serves the legacy version-0 tasks.

The current task then returns. The next task loads the same workflow and its
ordered steps. If there is no unfinished step, `advance` creates a new LLM
decision step. An LLM decision saves requested tool calls as queued steps; a
later invocation executes the next tool. Completed tool results become input
for a subsequent LLM decision. This is the continuous loop across Celery tasks.

`complete_task` publishes and returns without queuing another step.
`request_user_input` saves a waiting step and returns until an answer arrives.
Execution failures stop with a visible reason rather than silently restarting.
Unclaimed task delivery is recovered as described below. One task
means one **controller decision or operation**, not necessarily one provider
call: initialization may compact prior conversation, a controller decision may
compact its working context, and old draft operations contain multiple internal
calls.

This design reuses our workers, SQL progress, deadlines, and reload/recovery
behavior. It adds queue/SQL overhead per step. A long asynchronous controller
could be another design, and an expensive parallel tool could later use a chord
inside it. Neither change is implemented here; version 1 currently has no chord.

### Pending delivery recovery: current rules

Version 1 reuses `planning_task_id`, `dispatch_pending`, and the lease fields.
`prepare_agent_dispatch()` only changes the current SQL transaction: it assigns
a fresh expected task ID, sets pending delivery, releases the current lease, and
does not commit or contact Redis. Initial submission, clarification answers,
explicit Retry, and completed-step handoffs save their pending delivery in the
same transaction as their other progress. Resubmitting the same accepted request
keeps its already saved ID.

`dispatch()` now sends that saved ID. **Changed meaning:** `dispatch_pending`
stays true while awaiting a successful worker claim, even if Redis already
accepted its message. A successful SQL claim checks running status, expected ID,
pending delivery, and no existing lease, then installs the lease and clears
pending together. Task IDs alone do not make Redis/Celery deduplicate messages.
The SQL claim prevents concurrent execution; the next handoff rotates the ID
before releasing ownership, so delayed copies of the previous task are stale.

**Changed error handling:** a Redis publication exception is logged and leaves
the delivery pending, rather than immediately failing the assignment. The
existing Beat reconciliation checks every 30 seconds and can resend unclaimed
version-1 pending tasks after checking existing deadlines. Each periodic send is
reserved by conditionally updating `updated_at` only when at least 30 seconds
have passed; the Redis call happens after releasing the SQL transaction. This
prevents overlapping checks from sending bursts. Normal immediate sends remain
immediate. Sending does not update `last_progress_at` or extend the assignment
deadline, so the existing 30-minute queue wait and six-hour assignment deadline
still apply. There is no new retry-count limit or timing setting.

Recovery only repairs delivery. It never releases a claimed task's lease,
requeues a running operation, or reruns a ready result. An error notification
for an unclaimed pending task leaves delivery recoverable; after claim, worker
interruption still fails visibly and requires explicit Retry. Version-0 chord
workflows keep their existing manual-recovery policy. Progress GETs check status
and deadlines but do not send tasks. Cancelled, failed, completed, waiting-for-
clarification, and actively claimed workflows are not delivery candidates.
Stopping or completing the assignment clears pending delivery. An existing
final-publication marker remains authoritative.

Automatic continuation requires **Beat, the main worker, and Redis**. If they
are unavailable, recovery waits for them to return and remains subject to the
existing deadlines. Current tools commit SQL artifacts with their ready step
result; “completed” here means durably saved in SQL. A provider call that returned
before its result committed is still unfinished work for manual Retry. Future
tools that change files or external systems will need their own way to recognize
already-applied changes; this delivery fix does not claim that guarantee.

### Which old code remains active?

- `case_assistant_tasks.py`: fixed `plan_prompt`, `dispatch_group`,
  `process_section`, `after_extraction`, `dispatch_answer`, and `answer_prompt`
  serve version 0. Its routing, task options, ownership, error handling, and
  reconciliation are shared and remain required by version 1.
- `everything.py`: legacy phase-1 classification and old task wrappers are
  bypassed by new requests. Phase-2 drafting, revision, normalization, citation
  handling, state helpers, and brief updates remain active.
- `source_context.py` and `prepared_retrieval.py`: source reading, selection,
  and authorization helpers are active. Their old workflow planning/reduction
  orchestration is not automatically invoked by agent tools.
- `util/retrieval_review.py`: the old fixed retrieval review schedule is not an
  agent tool. Do not assume its automatic follow-up search happens in version 1.
- `case_assistant_worker.py`: the extraction pool serves old version-0 work.
  `document_preparation_worker.py` and `document_preparation_tasks.py` remain
  active for document preparation used by both versions.

## How existing operations become function calling

Existing code is not automatically transformed into a tool. The new adapter
gives it explicit inputs/results and a place in the controller's saved history:

1. `Search`, `Read`, `Analyze`, `Revise`, etc. are Pydantic argument schemas.
   `TOOL_TYPES` associates each tool name with its schema and description.
2. `definitions()` sends those descriptions/schemas to the LLM, not Python
   source code. `model_request()` receives a native `function_call` containing
   the name, JSON arguments, and `call_id`.
3. `advance()` saves that call in an `AgentStep`. On the tool invocation,
   `arguments()` checks the registered name and validates its JSON locally.
4. `Operations.execute()` invokes the selected Python method. For example,
   `search_sources()` calls existing `source_context.selected_spans()` using
   the server-owned accepted document snapshot. It returns passage ranges;
   `read_sources()` reads the original passages and saves a source artifact.
5. The tool result is saved. The next `model_request()` includes:

   ```python
   history.append({
       'type': 'function_call_output',
       'call_id': step.call_id,
       'output': step.result_json,
   })
   ```

   `call_id` connects the operation's result to the LLM's earlier request. The
   LLM then decides what follows; the result does not call the LLM by itself.

For drafting, `Operations.draft_operation()` builds the payload understood by
the old phase-2 code. `run_draft_operation()` substitutes `OperationMemory` for
`DBMemory`, runs that code against a copied state, and returns a candidate.
The controller saves it as `AgentArtifact`. Existing generation/revision logic
is reused, but the controller now decides when to invoke it and when to publish.

The tools are `list_documents`, `search_sources`, `read_sources`, `web_search`,
`analyze_material`, `python_run`, `lookup_authorities`, `search_legal_sources`,
`read_authority`, `create_draft`, `revise_draft`, `verify_draft`,
`request_user_input`, and `complete_task`. The controller uses tool schemas/results;
existing source preparation and revision code still have format-specific limitations.

`web_search(query)` handles general information, news and case developments
without requiring documents or a legal-research record. It saves generated search
synthesis and observed source links as a normal artifact. The controller chooses
the legal-research tools for citation review and legal proposition support, and
can use both routes for a mixed request. Both web-search callers share
`backend/util/web_search.py`; legal research retains its legal instructions.
Search synthesis is distinct from full source passages read with `read_authority`.

### Tool identifiers and progress messages

The November 2 clarification fixes the names exposed to the LLM, not stored
identity or execution policy:

| Identifier | Meaning and accepted use |
| --- | --- |
| `file_id` | An uploaded document. Use it for source search/read ranges, or as the target of an uploaded-draft revision/check. |
| `prepared_artifact_id` | An immutable document-preparation version in new catalogue/search/source-reference metadata. It is not an input to `material_ids`. |
| `artifact_id` | A saved operation result in this assignment: source material, analysis, candidate, or verification. Use it for artifact reads and `material_ids`; only draft artifacts can be draft targets. `current_draft` remains the special reference to the initial editor snapshot. |

Read original file ranges with `read_sources` to obtain a saved source material
ID before supplying it to analysis/drafting/verification. Those operations now
receive each material's server-resolved `artifact_id` and `kind` with its content.
The existing mode restriction is explicit in the tool descriptions:
`preserve_original` accepts uploaded `file_id` targets; `current_draft` and
saved candidates use `general_revision`, including for targeted edits. Invalid
IDs/modes still produce explicit errors; the application does not convert them.

The accepted SQL document snapshot keeps its original `artifact_id` preparation
field. Existing stored inputs/results are not rewritten. Artifact reads preserve
the saved serialized body and character offsets; material ID annotations are
added only to operation inputs. Old histories/artifacts remain readable.

New draft tool results say **Draft candidate saved; editor unchanged.** The
drafting engine's original response remains in the candidate's `generation_notes`,
and applied/rejected operations remain saved. Rejections also add a short warning
to the candidate status message. The adapter accepts both existing result shapes:
workspace `applied_operations`/`rejected_operations`, and uploaded-preservation
`operations` with nested `edits_rejected`. An uploaded-preservation result already
marked `blocked` (for example, ambiguous target passages) becomes an explicit
operation error with its clarification message; it does not save the unchanged
workspace as a candidate. The agent chooses what to do next through its existing
error-observation path. This propagates the old engine's decision; it adds no
automatic clarification step or new revision restriction.
Only final publication changes the editor.
Historical result messages retain their original text rather than being backfilled.

In the progress UI, a saved error observation displays **Operation error**;
successful `ready` steps display **Completed**. SQL still stores `ready` for
both: it means a result is available, not that the operation succeeded. The UI
does not infer that an error was recovered simply because later steps exist.
That display distinction adds no migration, limit, or automatic fallback.

### Verification reports describe their recorded target

`verify_draft()` remains an ordinary optional operation. Its saved report already
records `target`; the immediate tool result and small saved step summary now
include that same target alongside the report's artifact ID. This gives the
controller an explicit report-ID → checked-target-ID association without another
read or LLM call. Existing compaction guidance now asks working notes to preserve
those pairs and keep a report about candidate A distinct from a check of B.

New reports also save `instructions` (the exact generated checking instructions),
`check_citations` (whether the audit was requested), and `error` (an execution-error
flag). These describe scope and execution; `error: false` is not a `checks_passed`
claim or a finding that every citation is valid. Existing findings/error behavior
is unchanged. Old reports are not rewritten and remain readable without these
additional metadata fields.

Revising A creates a separate candidate B; report A keeps target A. The controller
decides whether its findings are useful, whether to check B, or whether to finish.
`publish()` does not classify verification requirements, require a matching
report, schedule verification, or copy a checked status to a new candidate.
Old verification reports are not automatically invalidated. B can still be
published after only A was checked, or without any
verification. Existing editor-write/ownership guards remain unchanged. This adds
no tool, stage, UI, migration, or mandatory draft/verify cycle.

Validation passed 141 controller, context, editor-write, delivery, uploaded-result,
and conversation tests. Coverage includes A → verify A → revise B → publish B
with one verification call, ordinary publication without verification, legacy
report paging, and report/target associations surviving forced compaction.

### Verification prose and unnamed editor sections

The existing verification call is instructed to return findings only. The
application saves the report artifact and reports persistence outcomes through
the existing operation result/error path. Generated prose must not invent
save/publication status or a lack of file-saving capability. This is guidance
for that existing call, without text-stripping rules or additional LLM calls.
Historical reports are retained as originally saved.

Unnamed editor sections render directly with their content visible, including
after publication and reload. They do not use the browser's default collapsed
`Details` control or acquire a generated heading. Named sections retain their
existing collapse controls. This is a display change; saved document contents,
agent decisions, and publication behavior are unchanged.

### Execution errors, interrupted steps, and publication recovery

`verify_draft()` makes a separate LLM call from the controller. That existing call
now receives the saved original assignment and actual clarification question/
answer pairs, alongside the controller's generated checking instructions, target
draft, and selected materials. The shared `agent_context.clarifications()` reader
uses this assignment's ready answered steps through the verification step's
sequence; generated working summaries and failed question calls are not user
requirements. Original user wording takes precedence over generated checking
instructions, and agent questions only provide context for the actual answers.
This adds no LLM call, extraction step, publication guard, or mandatory check.
The focused validation passed 94 controller, context, delivery, and uploaded-result
tests, including three new cases for exact wording/ordered answers with raw or
compacted controller history and for an assignment with no clarification answers.
Provider doubles verify the request contents and single-call behavior; they do
not establish the correctness of an LLM's verification findings.

`verify_draft(check_citations=True)` preserves both its substantive findings and
the citation helper's full returned details in the verification artifact. If the
helper returns a top-level `error` or `error_message`, the operation result also
contains `error: true` and explains that the citation audit was unavailable.
The step stays `ready` because the error observation is saved for the next LLM
decision; progress shows **Operation error**, and the assignment can continue.
Citation findings, including invalid citations, keep their existing treatment.
This change records execution errors only; it adds no citation acceptance rule.

`case_assistant_workflow.stop()` saves workflow and interrupted-step states in
one SQL transaction, after first checking for an already published final answer:

| Event | Saved agent-step behavior |
| --- | --- |
| Assignment fails | Running/waiting steps become `failed`, with the reason in `summary_json.message`. Queued steps remain queued and unexecuted. |
| User cancels before publication | Queued/running/waiting steps become `cancelled`, with a cancellation reason. |
| Explicit Retry of a failed assignment | Failed steps and historical steps still marked running become queued; their temporary interruption summary/result is cleared. |
| Any of these events | Ready steps, including saved operation-error observations, and completed artifacts remain unchanged. An interrupted step receives no fabricated tool result. |

Retry remains an explicit action for failed assignments only. It preserves step
arguments, call IDs, and sequence; it does not automatically rerun ready errors.
Existing worker-ownership and saved task-ID checks still reject stale saves and
old failure notifications. Existing terminal workflows are not backfilled;
Retry also handles their historical steps still marked running.
These changes do not preserve a separate history of
each failed execution attempt; Retry clears the interrupted step's temporary
failure display as it resumes that same saved step.

If the final answer and its `workflow_completed` marker committed before worker
loss, Stop or failure detection recovers `completed`, without another model/tool
call or another chat publication. Cancel then skips Celery revocation. Both the
saved-workflow Cancel route and confirmation-cancel route return
`{workflow_run_id, status, cancelled}` using the actual saved outcome. In this
crash-recovery case the status is `completed` and `cancelled` is false.
The browser only displays cancellation text for actual cancellation; other saved
outcomes use existing progress handling and completion reloads the saved chat and
editor. A failed cancellation request retains tracking and confirmation controls.
Legacy non-workflow task cancellation keeps its existing behavior.

### Explicit editor-write prohibition

`publish()` validates a selected candidate's ownership/type, then calls
`check_editor_write()` before loading or changing editor state or finalizing
the response. **New behavior:** the first candidate-publication attempt for a
given user context makes one additional structured LLM call, using the assignment's
selected model. An answer-only completion (`candidate_id=None`) skips this check;
creating or revising a private candidate does not call it either.

The check receives only the saved original assignment and actual clarification
question/answer pairs. Agent questions provide context for the answers. Generated
tool instructions, working summaries, candidate contents, and source documents
are excluded. The three outcomes have explicit meanings:

| Decision | Publication behavior |
| --- | --- |
| `forbidden` | The user explicitly prohibited editor changes. Return a recoverable operation error; the controller can complete answer-only. |
| `not_forbidden` | No explicit prohibition applies. Continue the existing publication and ownership checks. This is not a new requirement for routine approval. |
| `unclear` | Conflicting or ambiguous instructions prevent a reliable decision. Keep the editor unchanged and return an operation error; the controller can use the existing clarification tool or answer without editing. |

The decision, short reason, supporting user wording, and a hash of the exact
assignment/clarification input are saved in
`payload_json.agent_context.editor_write_check`. Different candidates reuse this
cache. New clarification answers change the input hash and require a fresh check
on the next publication attempt. It is local to this assignment, not a permanent
preference. Ask mode (`draft_mode=False`) is not an editor-write prohibition.

SQL locks are released before the provider call. Worker ownership is revalidated
afterward, and only the cache field is merged into freshly loaded payload before
saving. A missing, malformed, incomplete, or failed provider result does not
assume permission or overwrite a valid cache; the existing workflow failure and
manual Retry path applies. Natural-language interpretation remains fallible: the saved
reason and supporting wording make it inspectable, not a proof of understanding.

This adds no verification requirement, mandatory draft/verify cycle, public API,
tool, UI, migration, or permanent preference. Existing candidate isolation and
final publication guards remain in force.

Validation passed 297 distinct backend tests, including 14 new editor-write
cases and all four isolated Celery integration cases using memory/temporary Redis
transports. Permission-provider doubles test the publication guard, cache, and
failure paths; this pass does not measure real-model interpretation accuracy.
The earlier verification-context fix and its regressions remain included.

## Context and memory: what is saved and what the LLM sees

Saving something in SQL does not automatically send it to the LLM. There are
three distinct lifetimes: workspace history across assignments, a snapshot for
one assignment, and the growing history of decisions/results within it.

| Saved record | Contents | Use |
| --- | --- | --- |
| `CaseAssistantWorkflow.data_json` | Accepted query/options, document snapshot; conversation basis, cached conversation context, and prior-conversation summary checkpoints. | Stable request/scope and reuse of the initial conversation context. |
| `CaseAssistantWorkflow.payload_json` | Version 1: `agent_context` holds initial system/user input, copied draft/citation/constraint state, conversation context, optional `working_context` checkpoint, `compaction_parts` batch cache, and the input-keyed `editor_write_check` decision/reason/supporting wording; `agent_sources` records supporting-document hints. | Initialize once; build active input from the original inputs, saved working summary, and newer steps. Cache the explicit editor-write interpretation for candidate publication. Supporting hints help later requests identify earlier documents, without granting access to them. |
| Other workflow fields | User/chat IDs, version, status/phase, expected task IDs, lease, draft hash, deadlines, error, final response or pending question. | Scheduling, ownership, publication checks, polling, Retry, and clarification. |
| `AgentStep` | Sequence, kind (`model` or `tool`), name, `call_id`, arguments, full result, small public summary, state, timestamps. | Complete ordered decision/result history; active input can use a working-context checkpoint while unfinished work can resume. |
| `AgentArtifact` | Source excerpts and ranges; generated analysis; candidate drafts with citation metadata and revision operations/rejections; verification reports with checked target, requested check scope, and execution-error metadata. | Explicit references for later operations and owner-authorized inspection; a verification report describes its recorded target only. |
| `TextEntry` through `DBMemory` | Published workspace `state`: draft, previous draft where applicable, chat history/metadata, conversation brief, constraints, citation state, and existing workspace fields. | Visible editor/chat state and context for a later assignment. |
| Completed workflows / archived `TextEntry` turns | Original completed exchanges beyond the workspace's bounded chat history. | Recover older conversation without treating a summary as the original transcript. |
| Prepared document records/source maps | Canonical source text, prepared metadata, section/range locations, extraction warnings and versions. | Original factual evidence; distinct from generated conversation summaries or analysis. See the preparation runbook for storage/retention. |

### Within one assignment

`initialize()` saves the accepted assignment, conversation context, source
catalogue, selected section/mode, and a reference to the initial editor draft.
`current_draft` always reads that saved initial draft. It does not move to the
latest candidate; subsequent operations must name the intended candidate ID.

`model_request()` asks `agent_context.prepare_input()` to build the next input.
Before compaction, it starts with the saved initial input and replays all
**ready** steps in order, as before. LLM output items include encrypted provider
reasoning needed for continuation; tool results become native
`function_call_output` items. `store=False` means our application supplies the
context rather than relying on a provider-stored conversation.

#### Working-context compaction

The full `AgentStep` history and `AgentArtifact` contents remain in SQL. The
working context is a separate view for the next controller decision:

```text
Original system instructions and assignment
+ User clarifications preserved verbatim
+ Generated summary of older completed work
+ Recent complete model decisions and corresponding tool results
```

The two new context defaults are explicit: compact at **80% of the usable input
budget**, and keep a rolling summary of at most **4,000 tokens**. The usable
budget is the existing model context limit minus its output reserve and safety
margin. Request estimation includes the initial input, history/summary, tool
schemas, and framing; it uses the same application estimate as the final provider
preflight, not an exact measurement of encrypted provider context.

When the estimated request crosses that threshold, the helper summarizes the
oldest completed work and keeps as much recent work verbatim as fits. A retained
decision includes all its model output items and all corresponding tool results;
a function call is not separated from its output. If the latest complete group
is itself too large, it can be compacted too. Summarization processes all readable
content in bounded **24,000-token batches**, following the existing conversation
compaction pattern. Encrypted reasoning is excluded from the summary request but
remains intact in SQL and in any retained raw groups.

The summary is generated working notes, not original evidence. It preserves
progress, qualifications, source/artifact IDs, the current candidate, unresolved
findings, and unfinished work. Exact source text remains available through the
existing range/artifact reads; their stored bodies and character offsets do not
change. Summary calls use the assignment's selected model and the existing
structured-summary API path, SDK, key, and provider retry handling.

`payload_json.agent_context.working_context` stores
`{summary, through_sequence}`. Only steps after that completed boundary are
replayed verbatim. Successful batch summaries are cached in
`agent_context.compaction_parts` so an interrupted compaction can reuse completed
work. Worker ownership/cancellation is revalidated before saving. Empty,
incomplete, or oversized summaries do not replace the previous valid checkpoint.
Completed batch caches are cleared when the new working checkpoint commits.
Retry uses that checkpoint and completed batches without rerunning completed
tools. Existing assignments without a checkpoint continue their original replay
until context needs compaction.

The existing final context check still applies. An oversized fixed assignment,
or an individual analysis/drafting operation that cannot fit, still fails
explicitly. This change adds no native compaction API, SDK upgrade, migration,
tool-call limit, tool, workflow stage, or across-assignment memory mechanism.

Sources read by `read_sources` are returned as text plus references. Analysis
and verification usually return an artifact reference first; the controller's
instructions tell the LLM to read the saved findings with `read_sources` before
using them. `material_ids` explicitly selects inputs for another operation;
every saved artifact is not automatically injected into every operation.

`Operations` starts each invocation with a fresh copy of the initial saved state.
Candidates accumulate as artifacts, not mutations of live editor state.
`complete_task` may choose a saved candidate from this assignment or publish
an answer without changing the editor. `publish()` writes the selected result,
one final chat turn, and the completion marker together using existing guards.

### Across assignments in the same chat

`conversation_context.for_request()` saves a stable basis and builds context
from the brief/constraints, the last two exchanges, relevant older passages,
and summaries of exchanges not covered by the brief. Existing builder targets
are about 3,900 tokens for the overview, 7,900 for recent exchanges, and 3,900
for older passages, with a final 16,000-token check. Large inputs can be
summarized; original exchanges remain in SQL. These are inherited policies.

Workspace chat state retains up to 100 turns; removed original exchanges are
archived or recoverable from completed workflows. After final publication,
`_enqueue_brief_update()` schedules the existing brief/constraint update. The
builder also accounts for exchanges the brief has not yet covered.

Clarification answers from `request_user_input` remain in the assignment's
`AgentStep` and are supplied verbatim during that assignment, including after
working-context compaction. For later assignments, `available_turns()` reads
answered questions from completed workflows in the same chat, then attaches
them after merging SQL and workspace turns. `conversation_turn()` includes each
agent question and user answer with its original prompt; both the next context
and future brief updates use this representation. The older
`confirmed_clarification` field remains supported. This does not write answers
into chat state or promote an assignment-specific answer into a permanent rule.
Existing context snapshots and previously generated briefs are not rebuilt;
newly built contexts retrieve the saved answers through the existing history
selection and token budgets. Prior agent artifacts and other steps are still
not automatically offered as memory to the next assignment.

`service.execution()` uses a temporary `ContextVar` to identify the active
request/worker inside nested Python calls. It is neither LLM memory nor durable
storage and does not travel through Redis; each new task establishes it again.

## Current rules and limitations to keep visible

These describe existing implementation, not new rules added by this memo:

- **Choice and completion:** the LLM chooses tools in any useful order.
  `tool_choice='required'` requires an application action; `complete_task`
  finishes. No mandatory retrieve/draft/verify cycle or separate verification
  gate exists. The controller follows the user's requested checks and discloses
  unresolved findings/source limitations; report metadata does not select its
  next action or impose another verification step.
- **Execution:** controller tools run sequentially (`parallel_tool_calls=False`).
  Existing internal drafting behavior remains. There is no agent call-count,
  cost, repetition, or additional confirmation cap. Clarification is for missing
  information, not routine approval of already requested work.
- **Authorization:** strict registered argument schemas, accepted document
  versions, workflow/artifact ownership, and one active assignment per chat are
  enforced by application code. LLM arguments do not grant arbitrary paths,
  SQL access, or another user's documents. New uploads do not expand a saved scope.
- **Time:** existing defaults are a six-hour assignment deadline, 20-minute
  soft / 21-minute hard task limits, a 22-minute worker lease, and 30-minute
  queue-wait deadline. Request and queue-wait settings are configurable. Waiting
  for clarification does not remove the assignment deadline.
- **Context:** working-context compaction runs at the explicit 80% input-budget
  threshold and retains a summary of at most 4,000 tokens. Raw history/artifacts
  remain unchanged. Existing provider preflight still checks the final request,
  including tool schemas and output reserves; fixed inputs or individual
  operations can still exceed their available context. Retry reuses valid
  compaction checkpoints/batches rather than replaying oversized history again.
- **Provider retries:** selected Fast/Deep mode and existing adapter retries
  remain. Provider retries are distinct from resuming a failed workflow and
  from any count of agent tool calls. No new LLM fallback is introduced.
- **Retrieval:** `search_sources` reuses up to three sections and twelve source
  blocks per document and explicitly reports partial coverage. `read_sources`
  can read other authorized original ranges or page saved artifacts. There is
  no new automatic exhaustive-coverage gate; the old planner's exhaustive
  schedule is bypassed. Search hits, summaries, and generated analysis do not
  establish complete coverage or replace original evidence.
- **Drafting/checks:** old formatting, normalization, citation lookup/annotation,
  and revision behavior remain inside reused code. `preserve_original` remains
  an uploaded-draft mode; unsupported targets fail explicitly. `verify_draft`
  saves a substantive report without editing; `check_citations=True` adds the
  existing CourtListener audit. That audit differs from ordinary citation
  lookup/annotation during drafting/publication. Verification target/scope metadata
  informs the controller; it does not add a publication gate or automatic recheck.
- **Publication:** candidates do not change the live editor. Completion accepts
  only a candidate from this assignment, or no candidate for an answer-only
  completion. A candidate publication first checks the original assignment and
  actual clarification answers for an explicit editing prohibition; the cached
  interpretation and its failure behavior are described above. Answer-only
  completion adds no permission-check call. Source versions, worker ownership, cancellation, and the current
  draft hash are checked before the final write. An intervening edit can stop
  publication rather than being overwritten.
- **Failure/Retry:** invalid arguments and expected operation errors become
  observations for the LLM. Claimed-task execution, scope, ownership, context,
  or timeout failures stop visibly and mark interrupted agent steps failed.
  Explicit Retry keeps ready results and reruns unfinished
  work, under the same workflow ID and version; the draft/sources must still
  match. Interrupted provider calls/operations can execute again. This is not
  an exactly-once guarantee for arbitrary future tool side effects.
- **Delivery/cancellation:** SQL commit and Redis publication are separate.
  Version 1 saves the next delivery with completed progress and Beat can resend
  it until a worker claims it. Publication errors leave it pending; existing
  queue-wait/assignment deadlines still end unrecovered requests. Version 0
  keeps its prior failure/manual-Retry policy. Polling never sends tasks.
  Cancel saves invalid ownership and attempts revocation;
  an already running provider call may continue, but guarded late saves fail.
  An already committed final answer is recovered as completed even when Cancel
  arrives before the worker's final bookkeeping update.
- **Retention:** full agent history/artifacts persist in SQL. There is no
  dedicated agent-history retention/cleanup job in this implementation. Source
  byte retention is a separate existing document-preparation policy.

## Immediate improvements: recommendations, not implemented

The priority here is reliable general analysis/drafting, not adding coding-agent
tools or replacing Celery merely to resemble another product.

**November 7 readiness checkpoint:** the current schema/operation/artifact
interface is ready for incremental read and saved-artifact tools without a
controller rewrite. [Live cases 6–7](tests/e2e/scenarios/agent-foundation-live-results-2026-11-06.md)
and [the multi-step case 8](tests/e2e/scenarios/multistep-order-review-live-results-2026-11-07.md)
record clarification, result inspection, self-correction, checked-candidate
publication, and reload behavior. Remaining issues include false saving prose
in analysis, table formatting, missing comparison inputs, and cumulative token
cost. They are focused follow-up work, not new mandatory stages or call limits.
The future file/external-write recovery boundary in priority 4 below still
applies; these live cases do not prove production concurrency or arbitrary
side-effect safety. This checkpoint is a design assessment, not runtime policy.

| Priority | Improvement | Why / concrete completion evidence |
| --- | --- | --- |
| 1 | Validate clarification memory in longer conversations. | Saved questions/answers now join their original conversation turn across assignments. Evaluate their selection and summary quality over longer histories and reloads; they remain subject to existing context budgets. |
| 2 | Evaluate working-summary quality on long real assignments. | Working-context compaction is implemented with the documented defaults above. Check that important qualifications, artifact IDs, and unresolved findings survive repeated compactions; summaries do not replace original evidence. |
| 3 | Strengthen evidence coverage for document-wide requests. | Make read/unchecked ranges and extraction gaps inspectable; evaluate exhaustive requests and conflicting passages. Decide explicit behavior before claiming completeness. This does not imply a mandatory fixed tool sequence. |
| 4 | Extend delivery checks under real workloads and define future tool effects. | Version-1 pending delivery now recovers without rerunning durably completed steps. Continue evaluating worker loss, stale results, cancellation, and Retry under load. Before file-writing/external tools, define how an interrupted operation recognizes already-applied effects; do not enable blanket execution retries. |
| 5 | Separate reusable drafting code from `_everything_phase2_body` gradually. | Keep current normalization, revision modes, citations, and publication isolation observable while reducing the adapter's dependence on the large legacy function. Existing code is still active until replaced deliberately. |
| 6 | Decide history/artifact retention and operational visibility. | Define which results must remain inspectable, cleanup timing, access expectations, and useful timing/usage records. There is currently no new retention or cost policy. |

Excel and other file capabilities should then use the same explicit argument /
operation / saved-result interface. First decide what their file references,
outputs, side effects, and retry behavior mean; this memo does not add those tools.
An asynchronous controller versus per-step Celery scheduling remains a separate
performance/design decision, best informed by measured queue delay and load.

## Operating the current implementation

### Update and restart the normal local server

Use this sequence to test the current checkout on this Mac at
`http://localhost:3000`, with Flask at `http://127.0.0.1:5000`.
These instructions use `ENVIRONMENT=dev-monica`, the normal local SQLite database
`instance/myapp.db`, and local Redis. They do not deploy to a hosted environment.
They also do not use the copied database and dedicated queue from an isolated
browser E2E run; see [tests/e2e/README.md](tests/e2e/README.md) for that setup.

**1. Stop the previous application processes.** Finish or cancel active requests
in the UI first, then press Ctrl+C in the terminals running Flask, the main
Celery worker, the document worker, Beat, and the frontend. Stop a legacy
extraction worker too if one is running. Restart workers after Python changes:
an already running worker keeps its imported code even when Flask reloads.
For the delivery-recovery change, restart the backend, main worker, and Beat
together after finishing/cancelling active assignments; do not mix the earlier
and current pending-delivery behavior across running processes.

If a process was started in the background, find it first:

```bash
ps -axo pid,ppid,command | rg 'flask|backend.e2e_worker|backend.document_preparation_worker|backend.case_assistant_worker|celery|react-scripts/scripts/start'
```

Match the process to this checkout or its known test services, then use
`kill -TERM PID`, replacing `PID` with that process ID. Old process IDs are not
reusable restart instructions; identify current processes each time. A previous
isolated test backend can also occupy port 5000 and must be stopped before
starting the normal backend. Redis can keep running.

**2. Run this setup in every backend/worker terminal.** A new terminal starts
with its own environment; activating Conda in one does not activate the others.

```bash
cd /Users/yuxiao/Documents/lexintel
source /Users/yuxiao/anaconda3/etc/profile.d/conda.sh
conda activate lexintel

export ENVIRONMENT=dev-monica
unset LEXINTEL_E2E E2E_DATABASE_URL E2E_REDIS_URL E2E_CELERY_QUEUE
```

The prompt should show `(lexintel)`, not `(base)`. Verify the Python executable
when needed:

```bash
python -c "import sys, openai; print(sys.executable); print('OpenAI SDK', openai.__version__)"
```

Expected: `/Users/yuxiao/anaconda3/envs/lexintel/bin/python` and OpenAI SDK
`3.26.0`, matching `requirements.txt`. Check this environment before restarting
the backend and workers; updating Flask alone does not update a worker's SDK.
The startup commands below use `python -m` so Flask and Celery use that Python.
Clearing the E2E variables selects normal local operation rather than a test
copy. If you previously set `FLASK_SQLALCHEMY_DATABASE_URI` or `FLASK_CELERY`
overrides manually, clear those test overrides too before using this sequence.
These shell commands do not edit `.env` or application configuration files.

The checked-out files are the code these processes run. Updating them is the
Git/editing step; restarting does not fetch or switch branches. For this agent
implementation, the working branch is `Monica-agent`. Check before starting:

```bash
git branch --show-current
git status --short
```

**3. Check whether the local database needs existing migrations.** Do this from
the repository root after the backend environment setup and after stopping the
old application writers:

```bash
python -m flask --app backend.app db heads
python -m flask --app backend.app db current
```

If the current revision already matches the head, no database upgrade is needed.
If it is behind, back up the normal local database, apply existing migrations,
and confirm the resulting revision:

```bash
local_db_backup="instance/myapp-before-update-$(date +%Y%m%d-%H%M%S).db"
sqlite3 instance/myapp.db ".backup '$local_db_backup'"

python -m flask --app backend.app db upgrade
python -m flask --app backend.app db current
```

For the separated chat/document identity schema, the revision should be
`c0d1e2f3a4b5` (or a later revision if future code adds migrations). Canonical
storage uses `chat_threads`, `draft_documents` and `conversation_turns` alongside
the existing agent tables. The earlier `workspace_draft_states` table remains
read-only migration evidence. Upgrading a separate
test copy does not upgrade `instance/myapp.db`. Applying existing migrations
uses `db upgrade`; generating a new migration or running `db init` is not part
of restarting. Existing prepared documents do not need to be uploaded again
just for this controller migration. Optional authority research and its
overview/audit discovery modes add no database migration.

**First cutover to canonical workspace storage:** keep Flask and every worker
stopped after the backup and schema upgrade, then reconcile legacy data before
restarting them. The report includes imported identities, counts, repaired
workflow answers and ambiguous legacy records; it does not contain document text.

```bash
python scripts/backfill_workspace_state.py --report artifacts/workspace-backfill-preview.json
# Inspect the report before committing the import.
python scripts/backfill_workspace_state.py --apply --report artifacts/workspace-backfill-applied.json
python scripts/backfill_workspace_state.py --report artifacts/workspace-backfill-repeat.json
```

Without `--apply`, the script rolls back. Repeated runs preserve existing turn
identities and ordering; initialized workspaces report `already_initialized`.
The additive migration and import preserve the original legacy rows, and each
canonical draft row retains the original state bytes for reconciliation. Keep
the database backup for rollback; do not run older JSON-writing web/worker code
alongside the new writers. No production migration is part of local testing.

Workspace JSON is now an assembled read view. Editor routes require
`expected_draft_version` and return `draft_version`; stale writes return HTTP
409 while retaining the local editor buffer. Agent publication commits its
optional draft, conversation turn and workflow completion together. Auxiliary
writers patch named metadata fields rather than replacing the read view.

Local cutover on 2026-11-10: backed up `instance/myapp.db`, upgraded to
`b9c0d1e2f3a4`, imported 262 workspace drafts and 212 turns (44 workflow-backed),
and verified an idempotent repeat with SQLite integrity `ok`. Sixteen legacy
history/metadata length differences and one workspace with multiple state rows
were reported without discarding the original data. Local operational evidence
is under the ignored `artifacts/workspace-persistence-cutover-20261110/` directory.

On this Mac the final local restart uses backend port **5001** because AirPlay
also listens on 5000 (`Server: AirTunes` can appear as misleading CORS failures).
The frontend stays at `http://localhost:3000`. Start Flask with `--port=5001`,
then, **after** `npm start` generates the local brand file, change only
`http://127.0.0.1:5000/api/` to `http://127.0.0.1:5001/api/` in the ignored
`frontend/lex-intel/src/brandConfig.generated.js`. The dev server reloads it.
This is a local runtime override: `npm start` regenerates that file, so repeat
the override after a future restart while the port conflict exists. No macOS
AirPlay setting or committed brand configuration was changed.

**Chat/document identity cutover (2026-11-11):** stop every web and worker writer
before applying `c0d1e2f3a4b5`. The migration preserves chat integer/public IDs and
categories, gives each owned document an independent ID/public UUID, transfers
conversation counters into chats, and preserves historical workflow snapshots
and revision IDs. Existing `draftId` routes still identify chats; new interfaces
carry `chat_thread_id` and `draft_document_id` separately. Each newly created
chat receives one empty owned document slot. Legacy guided, HG and Word flows
continue through bounded compatibility aliases, not separate writable stores.

The local cutover backed up `instance/myapp.db`, rehearsed on a copy, then
validated 263 chats/documents and 214 turns with exact content/version/history
and artifact preservation. Repeated reconciliation reported no unresolved
identity or ordering records. The copy comparison also recorded 816 pre-existing
orphan upload/chunk references and confirmed the migration introduced none;
their cleanup is outside this refactor. Operational evidence is in the ignored
`artifacts/chat-document-identity-cutover-20261111/` directory. No production
migration was executed.

**4. Check Redis.** With the existing local Redis server running:

```bash
redis-cli -u redis://127.0.0.1:6379/0 ping
```

Expected: `PONG`. If Redis is not running, start the installed server in another
terminal and leave it running:

```bash
redis-server --bind 127.0.0.1 --port 6379
```

**5. Start Flask and workers in separate terminals.** Run the setup from step 2
in each terminal, then one of the following commands. Each command stays running.

Flask backend (terminal A):

```bash
python -m flask --app backend.app --debug run --host=127.0.0.1 --port=5000
```

`--debug` belongs before `run`. Placing it after `run` caused the reported
`No such option: --debug` error.

Main Celery worker, which executes the agent (terminal B):

```bash
python -m celery -A backend.make_celery.celery_app worker --loglevel=INFO --concurrency=2 --prefetch-multiplier=1
```

Document preparation worker, needed for uploads (terminal C):

```bash
python -m backend.document_preparation_worker --loglevel=INFO
```

Celery Beat, which schedules failure checks and version-1 pending-delivery
recovery (terminal D):

```bash
python -m celery -A backend.make_celery.celery_app beat --loglevel=INFO
```

Run one Beat process for this local configuration. If saved version-0 requests
may still be retried, also start the legacy extraction worker in another
terminal with the same setup:

```bash
python -m backend.case_assistant_worker --loglevel=INFO
```

That extraction worker serves the old fixed workflow. It does not replace the
main worker for new agent requests. The document/extraction entry points select
their own queues; use those entry points without adding `-Q` overrides.
Beat and the main worker are both required for automatic delivery recovery;
browser status polling alone only detects failure and cannot resend tasks.

**6. Restart the frontend in another terminal.**

```bash
cd /Users/yuxiao/Documents/lexintel/frontend/lex-intel
unset REACT_APP_E2E
REACT_APP_BRAND=local PORT=3000 npm start
```

Set the `local` brand explicitly so the frontend uses the local API and the
startup script does not need to infer a brand from the backend source. This is
the development server; a production build is not required for local testing.

**7. Reload the browser and verify the new path.** Open
`http://localhost:3000` and submit a new Case Assistant request. Confirm:

- Flask is listening on port 5000 and the frontend is on port 3000.
- The main worker's startup task list includes `backend.agent_tasks.advance`.
- `db current` reports the agent migration or a later revision.
- The new request shows **Steps and saved results** as operations complete.
  New requests use `execution_version=1`; old saved requests retain version 0.
- When the agent chooses `lookup_authorities`, its saved arguments include
  `mode: overview` or `mode: audit`. Saved research shows the selected scope and
  distinguishes overview examples from individual audit occurrences. These are
  operations executed by the main worker, not separate Celery worker tasks.

Restarting Flask alone is insufficient to update Celery workers. Flask's debug
reloader can reload Python web code; it does not reload those workers. The
frontend normally reloads source edits during development, but restarting it
is needed when changing its startup environment/brand. Documentation-only
changes do not require an application restart.

Common startup problems:

| Symptom | Check / correction |
| --- | --- |
| `ModuleNotFoundError: flask_jwt_extended` or another installed backend dependency | Check for `(lexintel)` and the Python path from step 2. The reported error used `(base)`; installing into base is not the fix for that environment mismatch. |
| `No such option: --debug` | Use `python -m flask --app backend.app --debug run ...`; `--debug` is a global Flask option. |
| Missing `execution_version` or agent tables | Apply `db upgrade` to the normal local database using the correct environment, then restart the backend/workers. |
| `Address already in use` on 5000 or 3000 | Identify the previous local/test process and stop it before starting its replacement. |
| Unregistered `backend.agent_tasks.advance` | Restart the main Celery worker from this checkout and Conda environment. |
| Uploads stay queued / requests stay queued | Check Redis and the document/main worker respectively, and confirm all backend terminals use the same normal local configuration. Version-1 pending delivery also needs Beat and the main worker for automatic resend. |
| Different saved chats/results than expected | Check whether a temporary E2E backend or environment override is still selecting a database copy. |

**Last verified local restart: November 10, 2026.** On `Monica-agent` at
`e51c7685`, Conda `lexintel` used the Python path above and OpenAI SDK `3.26.0`.
The normal `instance/myapp.db` was already at head `a8b9c0d1e2f3`, so no upgrade
was executed. Redis returned `PONG`; Flask listened on `127.0.0.1:5000`, the
frontend returned HTTP 200 on `localhost:3000` after compilation, and the
authenticated browser restored the workspace and received API HTTP 200 responses.
Flask's root `/` returned 404 because it is not an application health endpoint.
The main, document-preparation and legacy extraction workers responded to
inspection, and Beat was running. Startup evidence and service logs are under
the ignored local directory
`artifacts/live-case-assistant/authority-discovery-modes-20261110/`.
The temporary synthetic-chat trace launcher used for that live test is not a
normal startup dependency; use the standard worker commands above. Tracing was
disabled after testing and the main worker was restored to that ordinary command.
[Live results and the actual overview-to-audit data flow](tests/e2e/scenarios/authority-discovery-modes-live-results-2026-11-10.md)
record six completed requests, source-span checks, recovery and observed issues.

### Deployment prerequisites (instructions only; not executed by this update)

The pending-delivery update adds no migration. The earlier agent schema below
remains a prerequisite for a database that has not yet received it.

1. Activate Conda `lexintel`, and apply migration `a8b9c0d1e2f3`
   through the existing deployment process against the intended database.
   It adds `execution_version`, `case_assistant_agent_steps`, and
   `case_assistant_agent_artifacts`; old rows default to version 0.
2. Finish or cancel active assignments, then restart the backend, default Celery
   worker, and Beat together so every process uses the current scheduling logic
   and `backend.agent_tasks` is registered. Existing worker entry:
   `celery -A backend.make_celery.celery_app worker --loglevel=INFO`.
3. Keep document preparation, shared source storage, and Beat reconciliation
   configured as described in [DOCUMENT_PREPARATION_RUNBOOK.md](DOCUMENT_PREPARATION_RUNBOOK.md).
   Keep the extraction worker while version-0 work may still be resumed.
4. Build/deploy the matching frontend through the existing environment process.
   Do not treat a local code update as a production migration or deployment.

### Follow a request / diagnose a stop

Use the workflow ID returned by submission, not an individual Celery task ID:

- `GET /api/everything/workflows/<workflow_id>` reads SQL status and
  `info.agent`: operation summaries, artifact references, current operation,
  stopping reason, and execution version. Existing `/result/wf_...` adapts this.
- `GET /api/everything/workflows/<workflow_id>/artifacts/<artifact_id>` reads
  the saved artifact for its owner after document-scope/version checks. Even a
  completed candidate may become unavailable after source scope changes.
- `POST /api/everything/workflows/<workflow_id>/confirm` supplies the answer to
  a saved agent question; suggested choices or free text resume the same call.
  Existing `cancel` and `retry` routes remain under the same workflow URL.

Check `execution_version`, workflow status/error, and the next unfinished
`AgentStep` before diagnosing a failure as an old extraction/chord problem.
For a waiting question, answer it; a running workflow needs the worker consuming
its queue. If version 1 has `dispatch_pending=True`, no lease, and a saved
`planning_task_id`, Beat can resend that same task while existing deadlines
permit. Check Beat/main-worker availability rather than clicking Retry on a
still-running request. For a failed request, inspect the reason before explicit Retry.
A source/draft change can require a new request rather than replaying old scope.

The **Steps and saved results** UI displays operation progress and lets the user
inspect candidates without applying them. Questions/failures survive reload.
Raw controller output/encrypted reasoning are internal replay data, not returned
by polling or the artifact route. An operation list shows executed work; it is
not a transcript of the LLM's private reasoning.

### Existing verification and its limits

Activate `lexintel` before Python tooling. Focused existing backend tests:

```bash
conda activate lexintel
pytest tests/test_agent_context.py tests/test_agent_controller.py tests/test_agent_uploaded_result_contract.py tests/test_conversation_context.py tests/ai/test_chat_completion.py
```

Broader relevant suites cover source context, history/failures/limits, ownership,
Responses adapters, and migration compatibility. Agent tests cover result-driven
continuation, candidate isolation, verification-driven revision, clarification
reload/resume, invalid calls, stale/cancelled workers, context checks, Retry,
and recovery of an already published answer without duplicating it.

`test_agent_context.py` adds 17 checks for bounded repeated reads, an oversized
latest result, intact call/result groups, original SQL/artifact contents,
verbatim clarifications, invalid summaries, cached-batch Retry, and stale-worker
guards. The large-read test confirms beginning, middle, and end content all reach
bounded summary requests. Failed question calls remain ordinary observations,
rather than being mistaken for successful user clarifications.

The November 5 implementation validation passed all 130 checks in the command
above. A separate real structured-summary API smoke check used synthetic
order/invoice facts and a disposable SQLite fixture. A deliberately lowered
test budget forced two summary calls: estimated controller input dropped from
29,169 to 3,809 tokens, with a 535-token summary. Original instructions, the
verbatim clarification, source/candidate IDs, and saved history were retained;
rebuilding input reused the checkpoint without another call. The local report is
`artifacts/agent-context-20261105/live-smoke.json`. This is one synthetic sample,
not a production context limit, quality benchmark, or live browser/worker test.

The execution-state regression pass also runs
`tests/test_case_assistant_workflows.py` and `tests/test_source_context.py` for
legacy-workflow and shared-service compatibility. The new controller cases cover
audit execution errors versus citation findings, interrupted steps, explicit
Retry, stale attempts, and Cancel after publication of an edited candidate.
These tests inject provider/worker failures at application boundaries; the two
existing opt-in Redis/Celery integration cases are skipped unless separately enabled.

Frontend coverage includes `AgentProgress.test.js`, existing restoration/upload
tests, and the agent candidate/question flow in
`tests/e2e/playwright/case-assistant.spec.js`. Tests using mocked LLM responses
check the controller mechanics, not general task quality.

Execution-state validation passed 254 distinct backend tests, 12 frontend
progress tests, and all 33 Case Assistant browser cases (including seven new
cancellation cases). Two opt-in Redis/Celery integration cases were skipped.
Browser cases used mocked APIs on an isolated local test server; they did not
restart the normal application workers or call live providers. Restart workers
and reload the frontend before validating these changes against the live app.

The subsequent delivery-recovery pass passed 280 distinct backend tests,
including 22 new checks in `tests/test_agent_delivery.py`. All four isolated
Celery integration cases passed: existing native chords and new agent duplicate
deliveries, each using memory and a temporary Redis transport. Provider calls
were mocked; temporary Redis instances were cleaned up without touching normal
application services. Frontend code did not change in this delivery pass.
Run the new unit checks with `pytest tests/test_agent_delivery.py`. Enable the
existing integration switch to exercise real Celery/Redis delivery separately:

```bash
LEXINTEL_RUN_CELERY_INTEGRATION=1 pytest tests/test_agent_delivery_integration.py tests/test_case_assistant_workflows.py -k 'duplicate_agent_deliveries or native_redis_chord'
```

`python scripts/check_agent_controller.py` is an opt-in, billed real-provider
smoke test using synthetic order/invoice text, temporary SQLite, and an
in-memory Celery queue. It reports selected operations and final publication,
then removes temporary sources/database. It does not use production SQL/Redis
and does not establish real multiple-worker delivery/recovery behavior.

## Keeping this memo useful during later AI-assisted changes

When changing execution behavior, update the responsible section here with the
actual code change: entry/routing, tool inputs/results, saved data, context sent
to the LLM, publication/side effects, failure/Retry behavior, and validation.
State any new rule, default, limit, or automatic action explicitly, including
whether it is enforced in Python or requested by an LLM instruction. Keep
recommendations separate from implemented behavior and identify which old paths
must remain usable. This is documentation guidance, not a new runtime policy.
