"""SQL run state, worker leases, cancellation and atomic publication for Workspace."""
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
from backend.models import WorkspaceRun as Workflow, ChatThread, TextEntry
from backend.util.account_access import AccountUnavailable, require_enabled_account
_execution = ContextVar('workspace_execution', default=None)
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
    supplied = [data[key] for key in ('chat_thread_id', 'conversation_id') if key in data and data[key] is not None]
    if not supplied:
        raise ValueError('No conversation_id or chat_thread_id in request')
    if any((isinstance(value, bool) or not isinstance(value, (str, int)) or (not str(value).strip()) for value in supplied)):
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
        target = json.loads(row.data_json).get('draft_document_id')
    try:
        document = workspace_store.document_for_chat(row.chat_thread_id, document_id=target, lock=lock)
    except ValueError as exc:
        raise WorkflowStopped('This request no longer targets a document owned by its chat.') from exc
    if check_version and row.base_draft_version is not None and (document.draft_version != row.base_draft_version):
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
    row.expected_task_id = str(uuid.uuid4())
    row.dispatch_pending = True
    row.lease_token = row.lease_until = None
    row.updated_at = now()
    return row.expected_task_id

def submit(user_id, data):
    """Save an authorized prompt before any Celery message is published.

    Reusing a run_id with the same request returns the saved row, so an
    HTTP retry need not create another prompt. The draft-row lock and unique
    active_chat_thread_id together prevent two active prompts from editing one chat.
    The caller dispatches planning after this SQL transaction has committed.
    """
    from backend.util.document_preparation import validate_snapshot
    draft_id = resolve_chat_identity(data)
    validate_snapshot(user_id, draft_id, data['document_snapshot'])
    data = {k: v for (k, v) in data.items() if not k.startswith('_')}
    data['user_id'] = user_id
    if not isinstance(data.get('query'), str) or not data['query'].strip():
        raise ValueError('Enter a prompt before sending')
    workflow_id = data.get('run_id') or 'wf_' + uuid.uuid4().hex
    if not isinstance(workflow_id, str) or not re.fullmatch('wf_[A-Za-z0-9_-]{1,77}', workflow_id):
        raise ValueError('Invalid run_id')
    existing = db.session.get(Workflow, workflow_id)
    if existing:
        from backend.util.prepared_retrieval import evidence_scope
        previous = json.loads(existing.data_json)
        if existing.user_id != user_id or existing.chat_thread_id != draft_id or any((previous.get(key) != data.get(key) for key in ('query', 'assistant_mode', 'model', 'selected_section', 'draft_mode'))) or (evidence_scope(previous['document_snapshot']) != evidence_scope(data['document_snapshot'])):
            raise ValueError('The submitted prompt identifier is already in use')
        if data.get('draft_document_id') is not None:
            from backend.util import workspace_store
            document = workspace_store.document_for_chat(draft_id, data['draft_document_id'])
            if existing.draft_document_id is not None and document.id != existing.draft_document_id:
                raise ValueError('The submitted prompt identifier targets another document')
        return existing
    db.session.query(ChatThread).filter_by(id=draft_id).with_for_update().one()
    active = Workflow.query.filter_by(active_chat_thread_id=draft_id).first()
    if active:
        check_failure(active)
        db.session.query(ChatThread).filter_by(id=draft_id).with_for_update().one()
    if Workflow.query.filter_by(active_chat_thread_id=draft_id).first():
        raise ValueError('Another request is still active in this chat. Cancel it before sending another.')
    from backend.util import workspace_store
    document = workspace_store.document_for_chat(draft_id, document_id=data.get('draft_document_id'), lock=True)
    chat = db.session.get(ChatThread, draft_id)
    data['conversation_id'] = data['chat_thread_id'] = chat.public_id
    data['draft_document_id'] = document.public_id
    data['_source_context_version'] = 1
    data['_conversation_context_version'] = 1
    row = Workflow(id=workflow_id, user_id=user_id, chat_thread_id=draft_id, active_chat_thread_id=draft_id, draft_document_id=document.id, base_draft_version=document.draft_version, status='running', phase='agent', data_json=encode(data), draft_hash=fingerprint(draft_value(draft_id)), evidence_budget=int(current_app.config.get('WORKSPACE_EVIDENCE_TOKENS', 64000)), created_at=now(), updated_at=now(), last_progress_at=now(), deadline=now() + timedelta(seconds=int(current_app.config.get('WORKSPACE_REQUEST_SECONDS', 21600))))
    db.session.add(row)
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
def execution(workflow_id, token=None):
    value = {'id': workflow_id, 'token': token}
    marker = _execution.set(value)
    try:
        yield
    finally:
        _execution.reset(marker)

def current_execution():
    """Return the current with-block's metadata, or None outside execution()."""
    return _execution.get()

def validate_execution(*, lock=False):
    context = _execution.get()
    if context is None:
        return
    query = Workflow.query.filter_by(id=context['id']).populate_existing()
    row = (query.with_for_update() if lock else query).one()
    validate(row)
    if context['token'] and (row.lease_token != context['token'] or not row.lease_until or row.lease_until <= now()):
        raise WorkflowStopped('This worker no longer owns the request.')
    return row

def before_call():
    if current_execution() is not None:
        validate_execution()
        db.session.commit()

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
    from backend.models import DocumentPreparation
    document_ids = [entry['file_id'] for entry in json.loads(row.data_json)['document_snapshot']['files']]
    db.session.query(DocumentPreparation.id).filter(DocumentPreparation.id.in_(document_ids)).order_by(DocumentPreparation.id).with_for_update().all()
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
            if meta.get('run_id') == row.id:
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
    row.result_json = encode({'response': response, 'text': draft, 'chat_thread_id': chat.public_id, 'draft_document_id': document.public_id})
    (row.status, row.phase, row.active_chat_thread_id) = ('completed', 'finalize', None)
    row.lease_token = row.lease_until = None
    (row.error_message, row.dispatch_pending, row.updated_at) = (None, False, now())

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
        draft = json.loads(candidate.content_json)['draft'] if candidate and candidate.kind == 'draft' else workspace_store.load(row.chat_thread_id)['draft']
    stage_publication(row, turn.assistant_text, draft)
    db.session.commit()
    return True

def fail_task(task_id, message=None):
    if not task_id:
        return False
    row = Workflow.query.filter_by(expected_task_id=task_id).populate_existing().with_for_update().one_or_none()
    if row is None or row.status != 'running' or (row.dispatch_pending and row.lease_token is None):
        db.session.rollback()
        return False
    stop(row, message or 'Request processing failed. Click Retry to resume saved work.')
    return row.status == 'failed'

def check_failure(row):
    row = Workflow.query.filter_by(id=row.id).populate_existing().with_for_update().one()
    if row.status not in ACTIVE:
        db.session.commit()
        return row
    (instant, message) = (now(), None)
    if row.deadline <= instant:
        message = 'The request reached its overall deadline. Click Retry to resume saved work.'
    elif row.status == 'running':
        if row.lease_until and row.lease_until <= instant:
            message = 'The request worker stopped or timed out. Click Retry to resume saved work.'
        elif not row.lease_until and row.last_progress_at < instant - timedelta(seconds=int(current_app.config.get('WORKSPACE_QUEUE_WAIT_SECONDS', 1800))):
            message = 'No worker made progress before the queue-wait deadline. Check workers, then click Retry.'
    if message:
        stop(row, message)
    else:
        db.session.commit()
    return row

def status(row):
    from backend.agent_tasks import progress
    from backend.util import workspace_store
    row = check_failure(row)
    chat = db.session.get(ChatThread, row.chat_thread_id)
    document = workspace_store.document_for_chat(row.chat_thread_id, row.draft_document_id)
    agent = progress(row)
    operation = agent['current_operation']
    label = 'Waiting for your answer' if row.status == 'awaiting_user_confirmation' else 'Finished' if row.status == 'completed' else 'Request cancelled' if row.status == 'cancelled' else 'Choosing next action' if operation == 'choose_next_action' else operation.replace('_', ' ').capitalize()
    return {'run_id': row.id, 'conversation_id': chat.public_id, 'document_id': document.public_id, 'status': row.status, 'phase': row.phase, 'ready': row.status != 'running', 'successful': row.status == 'completed', 'state': 'REVOKED' if row.status == 'cancelled' else row.status, 'info': {'status': row.error_message or label, 'agent': agent}, 'value': json.loads(row.result_json) if row.result_json else None}

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
    if complete_published_answer(row):
        return
    (row.status, row.phase) = ('cancelled' if cancelled else 'failed', 'finalize')
    row.error_message = message
    row.result_json = encode({'cancelled': True, 'response': 'Request cancelled.'} if cancelled else {'error': True, 'error_message': message, 'response': message})
    row.active_chat_thread_id = None
    row.lease_token = row.lease_until = None
    (row.updated_at, row.dispatch_pending) = (now(), False)
    from backend.models import AgentStep
    interrupted = ('queued', 'running', 'waiting') if cancelled else ('running', 'waiting')
    reason = message or ('Request cancelled.' if cancelled else 'Request processing failed. Click Retry to resume saved work.')
    AgentStep.query.filter_by(run_id=row.id).filter(AgentStep.state.in_(interrupted)).update({'state': 'cancelled' if cancelled else 'failed', 'summary_json': encode({'message': reason}), 'updated_at': now()}, synchronize_session='fetch')
    db.session.commit()

def cancel(row):
    row = Workflow.query.filter_by(id=row.id).populate_existing().with_for_update().one()
    if row.status not in ACTIVE:
        return
    task_id = row.expected_task_id
    stop(row, cancelled=True)
    if row.status == 'cancelled' and task_id:
        from celery import current_app as celery_app
        try:
            celery_app.control.revoke(task_id)
        except Exception:
            current_app.logger.exception('Could not revoke cancelled Workspace task')

def retry(row):
    from backend.util.document_preparation import validate_snapshot
    from backend.models import AgentStep
    db.session.query(ChatThread).filter_by(id=row.chat_thread_id).with_for_update().one()
    row = Workflow.query.filter_by(id=row.id).populate_existing().with_for_update().one()
    validate_snapshot(row.user_id, row.chat_thread_id, json.loads(row.data_json)['document_snapshot'])
    if row.status not in ('failed', 'cancelled'):
        raise ValueError('Only a failed or cancelled request can be retried')
    if Workflow.query.filter_by(active_chat_thread_id=row.chat_thread_id).first():
        raise ValueError('Another request is active in this chat. Cancel it before retrying.')
    if fingerprint(draft_value(row.chat_thread_id)) != row.draft_hash:
        raise ValueError('The draft changed. Send a new prompt against the current draft.')
    validate_document_target(row, check_version=True)
    (row.status, row.active_chat_thread_id, row.error_message) = ('running', row.chat_thread_id, None)
    (row.result_json, row.last_progress_at) = (None, now())
    row.phase = 'agent'
    row.deadline = now() + timedelta(seconds=int(current_app.config.get('WORKSPACE_REQUEST_SECONDS', 21600)))
    row.expected_task_id = row.lease_token = row.lease_until = None
    (row.dispatch_pending, row.updated_at, row.attempts) = (True, now(), 0)
    AgentStep.query.filter_by(run_id=row.id).filter(AgentStep.state.in_(('failed', 'running', 'cancelled'))).update({'state': 'queued', 'summary_json': None, 'result_json': None, 'updated_at': now()}, synchronize_session='fetch')
    prepare_agent_dispatch(row)
    db.session.commit()
