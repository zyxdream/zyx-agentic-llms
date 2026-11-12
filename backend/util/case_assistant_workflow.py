"""SQL progress and worker ownership for one submitted Case Assistant prompt.

This module is active for BOTH execution versions; it is not legacy-only.
Read it alongside ``backend.agent_tasks`` and ``backend.case_assistant_tasks``:

* ``views.everything_api`` calls ``submit`` to save a CaseAssistantWorkflow.
* Version 1 routes to agent_tasks.advance for LLM decisions/tool operations.
  Version 0 uses the fixed planning/extraction/chord/answer tasks.
* Each Celery task claims the Workflow row (or a version-0 CaseAssistantWork
  section row), then enters ``execution`` while calling its helpers.
* Those helpers use ``validate_execution`` before provider calls and result saves.
  ``everything.save_state_to_memory`` uses ``guard_chat_write`` before publishing.
* ``case_assistant_routes`` reads these same SQL records for progress and Retry.

There are two different lifetimes here. SQL records survive worker crashes and
browser reloads. The ContextVar below only tells nested Python calls which saved
request and worker attempt they are currently serving; it disappears on exit.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
import hashlib
import json
import re
import uuid

from flask import current_app
from sqlalchemy import case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only

from backend.database import db
from backend.models import CaseAssistantWorkflow as Workflow, CaseAssistantWork as Work, ChatThread, TextEntry
from backend.util.account_access import AccountUnavailable, require_enabled_account
from backend.util.attachment_parser import WorkPending

# A regular module-global dict could mix concurrent callers' request metadata.
# ContextVar keeps the binding local to the current execution context. It does
# not send this value to another Celery process; each task enters execution().
_execution = ContextVar('case_assistant_execution', default=None)
ACTIVE = ('running', 'awaiting_user_confirmation')


class WorkflowStopped(Exception):
    """Stop an obsolete/cancelled attempt without treating it as an LLM error."""
    pass


class WorkflowNotFound(LookupError):
    """The requested workflow is absent or belongs to another user."""
    pass


def now():
    return datetime.utcnow()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def draft_value(draft_id):
    from backend.util import workspace_store
    return workspace_store.load(draft_id)['draft']


def resolve_chat_identity(data):
    """Resolve explicit chat identity and the legacy chat-only ``draftId`` alias.

    Document UUIDs are never searched as chat IDs. Conflicting aliases cannot
    silently retarget an editor mutation or an accepted agent request.
    """
    supplied = [data[key] for key in ('chat_thread_id', 'draftId')
                if key in data and data[key] is not None]
    if not supplied:
        raise ValueError('No chat_thread_id or draftId in request')
    if any(isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip()
           for value in supplied):
        raise ValueError('Invalid chat identity')
    resolved = [ChatThread.resolve_id(value) for value in supplied]
    if len(set(resolved)) != 1:
        raise ValueError('chat_thread_id and draftId refer to different chats')
    return resolved[0]


def validate_document_target(row, *, lock=False, check_version=False):
    """Keep this execution attached to its admitted chat-owned document.

    Older requests have no recorded base version and retain their existing
    hash-based freshness checks. New document publications also use version CAS.
    """
    from backend.util import workspace_store
    target = row.draft_document_id
    if target is None:
        # Historical requests have neither field. New accepted requests also
        # retain the public target in their immutable input snapshot.
        target = json.loads(row.data_json).get('draft_document_id')
    try:
        document = workspace_store.document_for_chat(row.chat_thread_id,
            document_id=target, lock=lock)
    except ValueError as exc:
        raise WorkflowStopped('This request no longer targets a document owned by its chat.') from exc
    if check_version and row.base_draft_version is not None and document.draft_version != row.base_draft_version:
        raise WorkflowStopped('The draft changed while this request was running. Retry against the current draft.')
    return document


def lock_publication_chat(chat_thread_id):
    """Use chat → workflow → document ordering for writes that append a turn.

    Conversation position allocation now updates the chat row. Taking it first
    keeps publication compatible with admission/Retry's existing lock order.
    """
    return ChatThread.query.filter_by(id=chat_thread_id).populate_existing().with_for_update().one()


def prepare_agent_dispatch(row):
    """Prepare the atomic handoff to the next version-1 task.

    Atomic handoff means completed results/artifacts, the next task ID, pending
    delivery, and lease release save together in one SQL commit.

    This helper prepares the scheduling fields below; it does not save the
    completed result, call commit(), or contact Redis. In advance(), the caller
    has already prepared the result/artifacts, then calls this helper followed
    by db.session.commit() to save all of those changes together. Submission,
    clarification answers, and explicit Retry use the same transaction pattern.

    After that commit, dispatch() sends the saved task ID. If the process stops
    before sending, Beat can resend it. Only a worker claim clears pending;
    interrupted claimed tasks still require manual Retry.
    """
    # Prepare a fresh expected ID. After the commit, old deliveries become stale;
    # delivery recovery resends this saved ID rather than allocating another one.
    row.planning_task_id = str(uuid.uuid4())
    # Pending means waiting for a worker claim, even if Redis received the task.
    row.dispatch_pending = True
    # Prepare release of this worker's ownership in the same commit as the result
    # and next ID, leaving no separate gap where an old task could claim again.
    row.lease_token = row.lease_until = None
    row.updated_at = now()
    return row.planning_task_id


def submit(user_id, data):
    """Save an authorized prompt before any Celery message is published.

    Reusing a workflow_run_id with the same request returns the saved row, so an
    HTTP retry need not create another prompt. The draft-row lock and unique
    active_chat_thread_id together prevent two active prompts from editing one chat.
    The caller dispatches planning after this SQL transaction has committed.
    """
    from backend.util.document_preparation import validate_snapshot
    draft_id = resolve_chat_identity(data)
    validate_snapshot(user_id, draft_id, data['document_snapshot'])
    # Private worker controls must never be supplied by the browser.
    data = {k: v for k, v in data.items() if not k.startswith('_')}
    data['user_id'] = user_id
    if not isinstance(data.get('query'), str) or not data['query'].strip():
        raise ValueError('Enter a prompt before sending')
    workflow_id = data.get('workflow_run_id') or 'wf_' + uuid.uuid4().hex
    if not isinstance(workflow_id, str) or not re.fullmatch(r'wf_[A-Za-z0-9_-]{1,77}', workflow_id):
        raise ValueError('Invalid workflow_run_id')
    existing = db.session.get(Workflow, workflow_id)
    if existing:
        from backend.util.prepared_retrieval import evidence_scope
        previous = json.loads(existing.data_json)
        if (existing.user_id != user_id or existing.chat_thread_id != draft_id
                or any(previous.get(key) != data.get(key) for key in ('query', 'assistant_mode', 'model', 'selected_section', 'draft_mode'))
                or evidence_scope(previous['document_snapshot']) != evidence_scope(data['document_snapshot'])):
            raise ValueError('The submitted prompt identifier is already in use')
        if data.get('draft_document_id') is not None:
            from backend.util import workspace_store
            document = workspace_store.document_for_chat(draft_id, data['draft_document_id'])
            if existing.draft_document_id is not None and document.id != existing.draft_document_id:
                raise ValueError('The submitted prompt identifier targets another document')
        return existing
    # Lock the draft while checking and creating its single active prompt.
    db.session.query(ChatThread).filter_by(id=draft_id).with_for_update().one()
    active = Workflow.query.filter_by(active_chat_thread_id=draft_id).first()
    if active:
        check_failure(active)
        # The status check commits its bookkeeping; reacquire admission's lock.
        db.session.query(ChatThread).filter_by(id=draft_id).with_for_update().one()
    if Workflow.query.filter_by(active_chat_thread_id=draft_id).first():
        raise ValueError('Another request is still active in this chat. Cancel it before sending another.')
    from backend.util import workspace_store
    document = workspace_store.document_for_chat(draft_id,
        document_id=data.get('draft_document_id'), lock=True)
    chat = db.session.get(ChatThread, draft_id)
    data['draftId'] = data['chat_thread_id'] = chat.public_id
    data['draft_document_id'] = document.public_id
    # Persist only at new admission. Old requests, HTTP retries and confirmation
    # resumes keep their policy even when the running backend is upgraded.
    data['_retrieval_policy_version'] = 1 if current_app.config.get('CASE_ASSISTANT_RETRIEVAL_REVIEW_ENABLED', True) else 0
    data['_source_context_version'] = 1
    data['_conversation_context_version'] = 1
    row = Workflow(id=workflow_id, user_id=user_id, chat_thread_id=draft_id, active_chat_thread_id=draft_id,
        draft_document_id=document.id, base_draft_version=document.draft_version,
        status='running', phase='agent', execution_version=1, data_json=encode(data), draft_hash=fingerprint(draft_value(draft_id)),
        evidence_budget=int(current_app.config.get('CASE_ASSISTANT_EVIDENCE_TOKENS', 64000)),
        created_at=now(), updated_at=now(), last_progress_at=now(),
        deadline=now() + timedelta(seconds=int(current_app.config.get('CASE_ASSISTANT_REQUEST_SECONDS', 21600))))
    db.session.add(row)
    # Save the first expected task ID with admission, even if the web process
    # stops before it can send the task. HTTP retries reuse this saved intent.
    prepare_agent_dispatch(row)
    db.session.commit()
    return row


def validate(row):
    """Recheck account access, request status and authorized document versions."""
    from backend.util.document_preparation import validate_snapshot
    if row.status != 'running' or row.deadline <= now():
        raise WorkflowStopped('This request is cancelled, finished, or has reached its deadline.')
    try:
        require_enabled_account(row.user_id)
    except AccountUnavailable as exc:
        raise WorkflowStopped(str(exc)) from exc
    validate_snapshot(row.user_id, row.chat_thread_id, json.loads(row.data_json)['document_snapshot'])
    validate_document_target(row)


@contextmanager
def execution(workflow_id, token=None, max_calls=None, work_id=None):
    """Expose one worker attempt to nested helpers for the duration of ``with``.

    Typical caller in case_assistant_tasks.plan_prompt::

        with service.execution(row.id, token, max_calls=1):
            payload = everything._everything_phase1_body(...)

    ``@contextmanager`` turns this generator function into a context manager.
    Before ``yield`` we install the metadata; at ``yield`` Python runs the
    caller's with-block; on leaving that block it runs ``finally``, including
    when an exception or return exits the block. Nothing is yielded as a value
    because callers only need the temporary context, not ``with ... as value``.
    Exceptions still propagate to the task's handlers; this scope does not
    catch them, retry work, or turn a failed operation into a successful one.

    Arguments connect the temporary context to durable SQL records:
    - workflow_id: CaseAssistantWorkflow.id, stable for the submitted prompt.
    - token: the current worker's SQL lease_token, returned by claim_workflow or
      assigned when process_section claims a CaseAssistantWork row. A lease is
      permission to work until lease_until; replacing its token invalidates an
      older worker even if that worker's provider call later returns.
    - work_id: CaseAssistantWork.id for extraction/reduction; omitted for a
      planning, callback, or answer attempt owned through the workflow row.
    - max_calls: optional continuation limit for prepared_retrieval.checked_call.
      before_call counts those calls; save_classification separately counts the
      classifier. It is not a token budget or a count of every provider retry.

    Connectivity: prepared_retrieval.checked_call calls before_call, and
    ai.base.get_openai_client checks the SQL attempt before provider I/O.
    save_segment validates before saving section results; guard_chat_write
    validates before DBMemory publishes chat state.
    These helpers find the IDs here without adding parameters to every existing
    drafting function. The helper checks, not this decorator, enforce ownership.

    This function neither schedules Celery tasks nor opens/closes a database
    transaction. Context does not travel through Redis: task arguments carry
    IDs, SQL stores progress, and each receiving task establishes its own scope.
    """
    value = {'id': workflow_id, 'token': token, 'work_id': work_id,
             'calls': 0, 'max_calls': max_calls}
    # marker is ContextVar's restoration token, NOT the SQL lease token above.
    marker = _execution.set(value)
    try:
        # Nested synchronous helpers now see value through current_execution().
        yield
    finally:
        # Restore the previous binding, even on failure. reset supports nested
        # scopes and prevents metadata leaking into the worker's next task.
        _execution.reset(marker)


def current_execution():
    """Return the current with-block's metadata, or None outside execution()."""
    return _execution.get()


def validate_execution(*, lock=False):
    """Read SQL to check the context's request, source versions and worker lease.

    populate_existing refreshes objects already cached by SQLAlchemy: another
    process may have cancelled or replaced this attempt. lock=True adds a SQL
    FOR UPDATE lock on the workflow row until the caller commits/rolls back;
    it is not a Python lock and is not released by leaving execution().
    Outside execution(), legacy/shared callers keep their existing behavior.
    """
    context = _execution.get()
    if context is None:
        return
    query = Workflow.query.filter_by(id=context['id']).populate_existing()
    row = (query.with_for_update() if lock else query).one()
    validate(row)
    if context.get('work_id'):
        attempt = Work.query.filter_by(id=context['work_id'], workflow_run_id=row.id).populate_existing().one()
    else:
        attempt = row
    if context['token'] and (attempt.lease_token != context['token'] or not attempt.lease_until or attempt.lease_until <= now()):
        raise WorkflowStopped('This worker no longer owns the request.')
    return row


def before_call():
    """Authorize a retrieval LLM call and yield planning when its allowance is used.

    checked_call invokes this before provider I/O. WorkPending is caught by
    plan_prompt, which schedules a continuation using saved reviews. Ending the
    authorization transaction here avoids holding its reads/locks over the call.
    """
    context = _execution.get()
    if context is None:
        return
    validate_execution()
    db.session.commit()
    if context['max_calls'] is not None and context['calls'] >= context['max_calls']:
        raise WorkPending('Continue planning from saved document reviews')
    context['calls'] += 1


def saved_classification():
    """Reuse the intent decision when plan_prompt continues after WorkPending."""
    context = _execution.get()
    if context:
        row = db.session.get(Workflow, context['id'])
        return json.loads(row.intent_json) if row.intent_json else None


def save_classification(decision):
    """Checkpoint classification before planning continues to relevance reviews."""
    context = _execution.get()
    if context:
        row = validate_execution(lock=True)
        row.intent_json, row.updated_at, row.last_progress_at = encode(decision), now(), now()
        db.session.commit()
        # The classifier does not use prepared_retrieval.checked_call, so count
        # it here rather than charging the same operation in before_call too.
        context['calls'] += 1


def guard_chat_write(state, memory):
    """Authorize a prospective publication against the locked canonical draft.

    Both execution versions keep these ownership, source and draft-freshness
    checks. The caller stages the draft, turn and workflow result, then commits
    them together; this guard neither publishes state nor commits independently.
    """
    context = _execution.get()
    if not context:
        return
    lock_publication_chat(memory.doc_id)
    row = Workflow.query.filter_by(id=context['id']).populate_existing().with_for_update().one()
    if row.status != 'running' or row.active_chat_thread_id != memory.doc_id or row.deadline <= now():
        db.session.rollback()
        raise WorkflowStopped('This request is no longer active.')
    if row.lease_token != context['token'] or not row.lease_until or row.lease_until <= now():
        db.session.rollback()
        raise WorkflowStopped('This worker no longer owns the request.')
    # The source or its accepted extraction gaps may have changed during the call.
    # Keep those versions stable until DBMemory commits the chat publication.
    from backend.models import DocumentPreparation
    document_ids = [entry['file_id'] for entry in json.loads(row.data_json)['document_snapshot']['files']]
    db.session.query(DocumentPreparation.id).filter(DocumentPreparation.id.in_(document_ids)).order_by(
        DocumentPreparation.id).with_for_update().all()
    validate(row)
    from backend.util import workspace_store
    document = validate_document_target(row, lock=True)
    current_draft = json.loads(document.draft_json)
    if fingerprint(current_draft) != row.draft_hash:
        db.session.rollback()
        raise WorkflowStopped('The draft changed while this request was running. Retry against the current draft.')
    row.draft_hash = fingerprint(state.get('draft', {}))
    row.updated_at = now()
    if (state.get('active_workflow') or {}).get('status') == 'completed':
        for meta in reversed(state.get('chat_history_meta', [])):
            if meta.get('workflow_run_id') == row.id:
                meta['workflow_completed'] = True
                break


def owned(workflow_id, user_id):
    """Authorize both the saved prompt and its chat before exposing progress."""
    row = Workflow.query.filter_by(id=workflow_id, user_id=user_id).first()
    if not row:
        raise WorkflowNotFound('Request not found')
    from backend.util.document_preparation import owned_scope
    owned_scope(user_id, 'chat', row.chat_thread_id)
    return row


def stage_publication(row, response, draft):
    """Stage workflow completion with its draft and conversation writes.

    The publication caller owns the single commit; helpers must not publish a
    draft, an answer or a completed workflow independently.
    """
    from backend.util import workspace_store
    document = workspace_store.document_for_chat(row.chat_thread_id, row.draft_document_id)
    chat = db.session.get(ChatThread, row.chat_thread_id)
    row.result_json = encode({'response': response, 'text': draft,
        'chat_thread_id': chat.public_id, 'draft_document_id': document.public_id})
    row.status, row.phase, row.active_chat_thread_id = 'completed', 'finalize', None
    row.lease_token = row.lease_until = None
    row.error_message, row.dispatch_pending, row.updated_at = None, False, now()


def complete_published_answer(row):
    """Recover completion from a durable conversation turn without rerunning work.

    Canonical turns have a unique workflow association. Mutable workspace JSON
    is not a publication marker and cannot hide a previously committed answer.
    """
    from backend.util import workspace_store
    workspace_store.initialize(row.chat_thread_id)
    turn = workspace_store.turn_for_workflow(row.id)
    if turn is None:
        return False
    result = json.loads(row.result_json or '{}')
    draft = result.get('text')
    if not isinstance(draft, dict):
        metadata = json.loads(turn.metadata_json or '{}')
        from backend.models import AgentArtifact
        candidate = db.session.get(AgentArtifact, metadata.get('candidate_id')) if metadata.get('candidate_id') else None
        draft = (json.loads(candidate.content_json)['draft'] if candidate and candidate.kind == 'draft'
                 else workspace_store.load(row.chat_thread_id)['draft'])
    stage_publication(row, turn.assistant_text, draft)
    db.session.commit()
    return True


def fail_task(task_id, message=None):
    """Fail only the current saved task; late failures cannot affect Retry.

    Celery errbacks can also run in the parent worker after a hard timeout or
    child-process loss. Match the saved message ID rather than relying on a
    child process's temporary execution context or lease. SQL is authoritative
    even if the chord cannot deliver its normal success callback.
    An unclaimed version-1 delivery stays pending for automatic resend; only
    claimed execution failures require explicit Retry.
    """
    if not task_id:
        return False
    from sqlalchemy import or_
    item = Work.query.options(load_only(Work.id, Work.workflow_run_id, Work.task_id,
                                       Work.state)).filter_by(task_id=task_id).first()
    workflow_id = item.workflow_run_id if item else db.session.query(Workflow.id).filter(
        or_(Workflow.planning_task_id == task_id, Workflow.answer_task_id == task_id)).scalar()
    if not workflow_id:
        db.session.rollback()
        return False
    row = Workflow.query.filter_by(id=workflow_id).populate_existing().with_for_update().one()
    if item:
        item = Work.query.filter_by(id=item.id).populate_existing().with_for_update().one()
        # Celery may report the chord callback failure before the failed header
        # task's errback. The callback cancels unfinished rows; still record the
        # actual failed section afterwards. Retry replaces its task ID, so an
        # older notification cannot change a new attempt.
        if (row.status == 'failed' and item.task_id == task_id and item.state == 'cancelled'):
            item.state, item.error_message = 'failed', message or 'Document task failed. Click Retry to resume saved work.'
            db.session.commit()
            return True
        if row.status != 'running' or item.task_id != task_id or item.state not in ('queued', 'running'):
            db.session.rollback()
            return False
        item.state, item.error_message = 'failed', message or 'Document task failed. Click Retry to resume saved work.'
        item.lease_token = item.lease_until = None
    elif row.status != 'running' or task_id not in (row.planning_task_id, row.answer_task_id):
        db.session.rollback()
        return False
    if row.execution_version == 1 and row.dispatch_pending and row.lease_token is None:
        # No worker claimed this delivery. Keep it pending for Beat to resend;
        # after a claim, pending is false and normal execution failure applies.
        db.session.rollback()
        return False
    stop(row, message or 'Request processing failed. Click Retry to resume saved work.')
    return row.status == 'failed'


def check_failure(row):
    """Expire stalled requests, never restart them or publish Celery messages.

    Beat calls this every 30 seconds on the main queue. Progress reads call it
    too, so failure becomes visible even when all workers have stopped. Keep
    the existing execution, queue-wait and whole-request deadlines. Healthy
    tasks and confirmations continue normally; only explicit Retry can start
    unfinished work after this function marks a request failed.
    """
    row = Workflow.query.filter_by(id=row.id).populate_existing().with_for_update().one()
    if row.status not in ACTIVE:
        db.session.commit()
        return row
    instant = now()
    message = None
    if row.deadline <= instant:
        message = 'The request reached its overall deadline. Click Retry to resume saved work.'
    elif row.status == 'running':
        work = Work.query.options(load_only(Work.id, Work.state, Work.error_message,
                                            Work.lease_token, Work.lease_until)).filter_by(workflow_run_id=row.id)
        failed = work.filter_by(state='failed').first()
        expired = work.filter_by(state='running').filter(
            (Work.lease_until.is_(None)) | (Work.lease_until <= instant)).first()
        if failed:
            message = failed.error_message or 'A document task failed. Click Retry to resume saved work.'
        elif expired:
            expired.state, expired.error_message = 'failed', 'The document worker stopped or timed out.'
            expired.lease_token = expired.lease_until = None
            message = 'The document worker stopped or timed out. Click Retry to resume saved work.'
        elif row.lease_until and row.lease_until <= instant:
            message = 'The request worker stopped or timed out. Click Retry to resume saved work.'
        elif not row.lease_until and not work.filter_by(state='running').count():
            wait = int(current_app.config.get('CASE_ASSISTANT_QUEUE_WAIT_SECONDS', 1800))
            if row.last_progress_at < instant - timedelta(seconds=wait):
                message = 'No worker made progress before the queue-wait deadline. Check workers, then click Retry.'
    if message:
        # stop() first checks an answer's publication marker. Read chat content
        # only when failure is suspected, not on every healthy progress poll.
        stop(row, message)
    else:
        db.session.commit()
    return row


def status(row):
    """Build the frontend's small polling response from SQL, not Celery results.

    Split parents are replaced by their children. Character coverage uses the
    union of source ranges so overlapping sections do not inflate progress.
    load_only and metadata queries keep passage bodies out of frequent polls;
    the evidence route retrieves those separately on demand.
    """
    row = check_failure(row)
    counts, files, ranges = {}, {}, {}
    # Counts and coverage must use the same records while workers add work.
    # Only query work needs text positions; other kinds can contain evidence.
    work = db.session.query(Work.id, Work.document_id, Work.kind, Work.state, Work.error_message,
        case((Work.kind == 'query', Work.material_json)).label('material_json')).filter(
            Work.workflow_run_id == row.id).all()
    for item in work:
        if item.state == 'split':
            continue
        label = item.kind + ':' + item.state
        counts[label] = counts.get(label, 0) + 1
        if item.document_id and item.kind == 'query':
            document = files.setdefault(item.document_id, {'file_id': item.document_id, 'total': 0, 'completed': 0, 'failed': 0})
            document['total'] += 1
            document['completed'] += item.state == 'ready'
            document['failed'] += item.state == 'failed'
            if item.error_message and len(document.get('errors', [])) < 3:
                document.setdefault('errors', []).append({'section_id': item.id, 'message': item.error_message})
            span = json.loads(item.material_json)
            required, completed = ranges.setdefault(item.document_id, ([], []))
            required.append((span['start'], span['end']))
            if item.state == 'ready':
                completed.append((span['start'], span['end']))
    def covered_length(spans):
        total, end = 0, 0
        for start, stop in sorted(spans):
            total += max(0, stop - max(start, end))
            end = max(end, stop)
        return total
    for uid, (required, completed) in ranges.items():
        files[uid]['required_characters'] = covered_length(required)
        files[uid]['completed_characters'] = covered_length(completed)
    from backend.models import UploadedFiles
    for file in db.session.query(UploadedFiles.unique_id, UploadedFiles.original_filename, UploadedFiles.relative_path).filter(
            UploadedFiles.unique_id.in_(list(files))).all():
        files[file.unique_id]['filename'] = file.original_filename
        files[file.unique_id]['relative_path'] = file.relative_path
    completed = counts.get('query:ready', 0)
    total = sum(v for k, v in counts.items() if k.startswith('query:'))
    label = {'classify': 'Selecting documents', 'review': 'Checking extracted evidence',
             'search': 'Searching additional source passages',
             'extract': f'Extracting passages: {completed} of {total} sections completed',
             'draft': 'Preparing the answer', 'finalize': 'Finished', 'confirm': 'Waiting for your confirmation'}.get(row.phase, row.phase)
    from backend.util.retrieval_review import summary
    from backend.util import workspace_store
    document = workspace_store.document_for_chat(row.chat_thread_id, row.draft_document_id)
    chat = db.session.get(ChatThread, row.chat_thread_id)
    result = {'workflow_run_id': row.id, 'draft_id': row.chat_thread_id,
            'chat_thread_id': chat.public_id, 'draft_document_id': document.public_id,
            'status': row.status, 'phase': row.phase,
            'ready': row.status not in ('running',), 'successful': row.status == 'completed',
            'state': 'REVOKED' if row.status == 'cancelled' else row.status,
            'info': {'status': row.error_message or label, 'counts': counts, 'files': list(files.values()),
                     'retrieval': summary(json.loads(row.payload_json) if row.payload_json else {}),
                     'coverage_mode': (json.loads(row.payload_json).get('intent_decision', {}).get('coverage_mode')
                                       if row.payload_json else None)},
            'value': json.loads(row.result_json) if row.result_json else ({'error_message': row.error_message, 'error': True} if row.error_message else None),
            'report_available': bool(completed), 'report_url': f'everything/workflows/{row.id}/evidence'}
    if row.execution_version == 1:
        from backend.agent_tasks import progress
        result['info']['agent'] = progress(row)
        operation = result['info']['agent']['current_operation']
        result['info']['status'] = row.error_message or (
            'Waiting for your answer' if row.status == 'awaiting_user_confirmation' else
            'Finished' if row.status == 'completed' else
            'Request cancelled' if row.status == 'cancelled' else
            'Choosing next action' if operation == 'choose_next_action' else operation.replace('_', ' ').capitalize())
    return result


def stop(row, message=None, cancelled=False):
    """Save a terminal status, release the active chat, and invalidate attempts.

    Completed sections/results remain available for the evidence report and a
    failed request's explicit Retry. Clearing ownership prevents an already
    running worker from publishing a late result after this commit.
    """
    row = Workflow.query.filter_by(id=row.id).populate_existing().with_for_update().one()
    if row.status not in ACTIVE:
        db.session.rollback()
        return
    # A committed final answer wins even if the user clicked Cancel before the
    # worker saved its final workflow status. Recover bookkeeping without reruns.
    if complete_published_answer(row):
        return
    row.status, row.phase = ('cancelled' if cancelled else 'failed'), 'finalize'
    row.error_message = message
    row.result_json = encode({'cancelled': True, 'response': 'Request cancelled.'} if cancelled else
                            {'error': True, 'error_message': message, 'response': message})
    row.active_chat_thread_id = None
    row.lease_token = row.lease_until = None
    row.updated_at, row.dispatch_pending = now(), False
    Work.query.filter_by(workflow_run_id=row.id).filter(Work.state.in_(['queued', 'running'])).update({
        'state': 'cancelled', 'lease_token': None, 'lease_until': None}, synchronize_session='fetch')
    if row.execution_version == 1:
        from backend.models import AgentStep
        # Commit the workflow and its interrupted steps together. On failure,
        # queued calls remain unexecuted; Cancel also cancels those queued calls.
        # Ready results/artifacts, including saved operation errors, stay intact.
        interrupted = ('queued', 'running', 'waiting') if cancelled else ('running', 'waiting')
        reason = message or ('Request cancelled.' if cancelled else 'Request processing failed. Click Retry to resume saved work.')
        # A message-only summary explains failed/cancelled execution in progress.
        # Do not invent a tool result for an execution that never finished.
        AgentStep.query.filter_by(workflow_run_id=row.id).filter(AgentStep.state.in_(interrupted)).update({
            'state': 'cancelled' if cancelled else 'failed', 'summary_json': encode({'message': reason}),
            'updated_at': now()}, synchronize_session='fetch')
    db.session.commit()


def cancel(row):
    """Commit cancellation first, then ask Celery to skip outstanding task IDs.

    revoke is best effort and does not terminate an in-flight provider request.
    SQL checks before result/chat saves are what prevent its late publication.
    """
    row = Workflow.query.filter_by(id=row.id).populate_existing().with_for_update().one()
    if row.status not in ACTIVE:
        return
    task_ids = [row.planning_task_id, row.answer_task_id] + [task_id for task_id, in db.session.query(Work.task_id).filter(
        Work.workflow_run_id == row.id, Work.state.notin_(['ready', 'split'])).all()]
    stop(row, cancelled=True)
    # stop() can recover a final answer committed just before worker loss.
    # There is then no cancellation to send to Celery, and the API must say so.
    if row.status != 'cancelled':
        return
    # Saved cancellation is authoritative even if Redis is temporarily unavailable.
    from celery import current_app as celery_app
    for task_id in filter(None, task_ids):
        try:
            celery_app.control.revoke(task_id)
        except Exception:
            current_app.logger.exception('Could not revoke cancelled Case Assistant task')


def retry(row):
    """Resume a failed prompt or cancelled agent prompt under the same ID.

    Source versions and the draft must still match the original request. New
    Celery task IDs are prepared before sending; the browser keeps
    polling this Workflow.id. Version 0 retains finished sections/split parents;
    version 1 retains ready AgentSteps/artifacts and requeues interrupted steps.
    """
    from backend.util.document_preparation import validate_snapshot
    db.session.query(ChatThread).filter_by(id=row.chat_thread_id).with_for_update().one()
    row = Workflow.query.filter_by(id=row.id).populate_existing().with_for_update().one()
    validate_snapshot(row.user_id, row.chat_thread_id, json.loads(row.data_json)['document_snapshot'])
    if row.status != 'failed' and not (row.status == 'cancelled' and row.execution_version == 1):
        raise ValueError('Only a failed request or a cancelled agent request can be retried')
    if Workflow.query.filter_by(active_chat_thread_id=row.chat_thread_id).first():
        raise ValueError('Another request is active in this chat. Cancel it before retrying.')
    if fingerprint(draft_value(row.chat_thread_id)) != row.draft_hash:
        raise ValueError('The draft changed. Send a new prompt against the current draft.')
    validate_document_target(row, check_version=True)
    row.status, row.active_chat_thread_id, row.error_message = 'running', row.chat_thread_id, None
    row.result_json, row.last_progress_at = None, now()
    row.phase = 'extract' if row.payload_json and json.loads(row.payload_json).get('_sections_planned') else 'classify'
    row.deadline = now() + timedelta(seconds=int(current_app.config.get('CASE_ASSISTANT_REQUEST_SECONDS', 21600)))
    row.group_id = row.answer_task_id = row.planning_task_id = None
    row.lease_token = row.lease_until = None
    row.dispatch_pending, row.updated_at, row.attempts = True, now(), 0
    for item in row.work:
        if item.state not in ('ready', 'split'):
            item.state, item.attempts, item.error_message = 'queued', 0, None
            item.lease_token = item.lease_until = None
            item.task_id = None
    if row.execution_version == 1:
        from backend.models import AgentStep
        row.phase = 'agent'
        # Resume interrupted or cancelled steps, clearing unfinished results and
        # stopping summaries. Older failed workflows may still have a running step.
        # Ready error observations remain ready for the LLM, not automatic reruns.
        AgentStep.query.filter_by(workflow_run_id=row.id).filter(AgentStep.state.in_(('failed', 'running', 'cancelled'))).update(
            {'state': 'queued', 'summary_json': None, 'result_json': None, 'updated_at': now()}, synchronize_session='fetch')
        # Requeued work and its next delivery become durable together. If the
        # Retry HTTP request stops before sending, Beat can deliver the same ID.
        prepare_agent_dispatch(row)
    db.session.commit()


def save_segment(document_id, key, kind, value):
    """Insert/reuse a validated result; let process_section commit completion.

    flush sends the INSERT and obtains its ID without committing. The caller
    then links Work.result_id and marks that section ready in the same commit.
    The unique document/key constraint resolves duplicate cache insertions; if
    it forces a rollback, the caller must reacquire/check ownership before its
    final commit, as process_section does.
    """
    from backend.models import DocumentSummarySegment
    validate_execution(lock=True)
    saved = DocumentSummarySegment.query.filter_by(document_id=document_id, segment_key=key).first()
    if not saved:
        saved = DocumentSummarySegment(document_id=document_id, segment_key=key, kind=kind, state='ready', result_json=encode(value))
        db.session.add(saved)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            saved = DocumentSummarySegment.query.filter_by(document_id=document_id, segment_key=key).one()
    return saved
