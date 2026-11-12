"""HTTP access to saved prompt progress, actions and paginated source evidence.

The browser polls one Workflow.id, not every Celery task ID. These routes read
SQL via case_assistant_workflow; they do not wait for model calls. Retry and
confirmation save their changes before asking case_assistant_tasks to dispatch.
"""
from functools import wraps
import json

from flask import Blueprint, request, send_file
from flask_cognito import cognito_auth_required, current_user
from sqlalchemy.exc import OperationalError, IntegrityError

from backend.database import db
from backend.models import CaseAssistantWorkflow as Workflow, UploadedFiles, DocumentPreparedArtifact
from backend.util import case_assistant_workflow as service
from backend.util.document_preparation import PreparationError, validate_snapshot, owned_scope

bp = Blueprint('case_assistant_workflows', __name__, url_prefix='/api/everything/workflows')


def guarded(fn):
    """Translate expected SQL/domain errors into HTTP responses and roll back.

    This is a Python decorator: the decorated route calls ``call`` below, which
    invokes fn inside try/except. wraps(fn) preserves the route function's name
    and __wrapped__ reference. Authentication is applied separately above this
    wrapper in each route's decorator stack; service.owned also checks the chat.
    """
    @wraps(fn)
    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except service.WorkflowNotFound:
            db.session.rollback()
            return {'msg': 'Request not found'}, 404
        except PreparationError as exc:
            db.session.rollback()
            return exc.payload, exc.status
        except (ValueError, TypeError, service.WorkflowStopped):
            db.session.rollback()
            return {'msg': 'The request cannot be resumed. Refresh its documents and retry.'}, 409
        except IntegrityError:
            db.session.rollback()
            return {'msg': 'Another request is active in this chat.'}, 409
        except OperationalError:
            db.session.rollback()
            return {'msg': 'Request progress is unavailable. Check database migrations and retry.'}, 503
    return call


@bp.get('/active')
@cognito_auth_required
@guarded
def active():
    """Find the chat's active prompt after reload without knowing a task ID."""
    _, draft_id, _ = owned_scope(current_user.id, 'chat', request.args.get('draftId'))
    row = Workflow.query.filter_by(user_id=current_user.id, active_chat_thread_id=draft_id).first()
    return {'workflow': service.status(row) if row else None}


@bp.get('/<workflow_id>')
@cognito_auth_required
@guarded
def progress(workflow_id):
    """Return aggregate SQL progress/result for one authorized submitted prompt."""
    return service.status(service.owned(workflow_id, current_user.id))


@bp.get('/<workflow_id>/artifacts/<artifact_id>')
@cognito_auth_required
@guarded
def agent_artifact(workflow_id, artifact_id):
    from backend.models import AgentArtifact
    row = service.owned(workflow_id, current_user.id)
    # Scope/version checks still apply when reading a completed candidate.
    validate_snapshot(row.user_id, row.chat_thread_id, json.loads(row.data_json)['document_snapshot'])
    artifact = AgentArtifact.query.filter_by(id=artifact_id, workflow_run_id=row.id).first()
    if artifact is None and db.session.get(AgentArtifact, artifact_id) is None:
        raise service.WorkflowNotFound('Artifact not found')
    if artifact is None or artifact.kind in {'authority_research', 'authority_source'}:
        from types import SimpleNamespace
        from backend.util.authority_research import resolve_artifact
        value = resolve_artifact(SimpleNamespace(row=row,
            snapshot=json.loads(row.data_json)['document_snapshot']), artifact_id)
        return {'id': artifact_id, 'kind': value['kind'], 'content': value}
    if artifact.kind in {'python_execution', 'derived_file'}:
        from backend.util.prepared_retrieval import authorized_artifacts
        authorized_artifacts(json.loads(row.data_json)['document_snapshot'])
    return {'id': artifact.id, 'kind': artifact.kind, 'content': json.loads(artifact.content_json)}


@bp.get('/<workflow_id>/artifacts/<artifact_id>/files/<file_id>')
@cognito_auth_required
@guarded
def agent_generated_file(workflow_id, artifact_id, file_id):
    from backend.util.agent_python import downloadable_file
    from backend.util.prepared_retrieval import authorized_artifacts
    row = service.owned(workflow_id, current_user.id)
    authorized_artifacts(json.loads(row.data_json)['document_snapshot'])
    path, filename = downloadable_file(row, artifact_id, file_id)
    return send_file(path, as_attachment=True, download_name=filename, conditional=True)


@bp.post('/<workflow_id>/cancel')
@cognito_auth_required
@guarded
def cancel(workflow_id):
    """Save cancellation even if Redis revocation cannot reach the workers."""
    row = service.owned(workflow_id, current_user.id)
    service.cancel(row)
    # Cancellation may instead recover an already published final answer.
    # Return the saved outcome so the browser does not replace it with Cancelled.
    return {'workflow_run_id': row.id, 'status': row.status, 'cancelled': row.status == 'cancelled'}


@bp.post('/<workflow_id>/retry')
@cognito_auth_required
@guarded
def retry(workflow_id):
    """Keep the prompt ID and resume unfinished work in its saved execution version.

    Version 1 keeps ready AgentSteps/artifacts and dispatches advance again.
    Version 0 keeps ready extraction sections and its fixed workflow phase.
    """
    from backend import case_assistant_tasks as tasks
    row = service.owned(workflow_id, current_user.id)
    service.retry(row)
    tasks.dispatch_planning(row) if row.execution_version == 1 or row.phase == 'classify' else tasks.dispatch_group(row)
    return {'result_id': row.id, 'workflow_run_id': row.id}


@bp.post('/<workflow_id>/confirm')
@cognito_auth_required
@guarded
def confirm(workflow_id):
    """Resume the same prompt after confirming its draft action or clarifying it.

    Revalidate the saved documents and draft because they could have changed
    while waiting for the user; keep the active chat reservation.
    Version 1 saves the answer as the waiting request_user_input tool result
    and resumes advance. Only version 0 clears classification to replan phase 1.
    """
    from backend import case_assistant_tasks as tasks
    from backend.everything import _confirmation_clarification, _augment_query_with_clarification
    row = service.owned(workflow_id, current_user.id)
    row = Workflow.query.filter_by(id=row.id).populate_existing().with_for_update().one()
    decision = (request.get_json() or {}).get('decision') or {}
    if decision.get('cancelled') or decision.get('draft_action') == 'cancel':
        service.cancel(row)
        # Match Stop's response, including completion recovered after worker loss.
        return {'workflow_run_id': row.id, 'status': row.status, 'cancelled': row.status == 'cancelled'}
    if row.execution_version == 1:
        from backend.agent_tasks import answer_question
        answer_question(row, decision)
        return {'result_id': row.id, 'workflow_run_id': row.id}
    if row.status != 'awaiting_user_confirmation' or row.deadline <= service.now():
        raise ValueError('Confirmation is no longer active')
    data = json.loads(row.data_json)
    validate_snapshot(row.user_id, row.chat_thread_id, data['document_snapshot'])
    if service.fingerprint(service.draft_value(row.chat_thread_id)) != row.draft_hash:
        raise ValueError('The draft changed while awaiting confirmation')
    result = json.loads(row.result_json)
    clarification = _confirmation_clarification(decision)
    if clarification:
        data['query'] = _augment_query_with_clarification(data['query'], clarification, result['confirmation'].get('reason', 'how to proceed'))
        data['display_user_message'] = json.loads(row.data_json)['query']
        data['_clarified_after_confirmation'] = True
        data['confirmed_clarification'] = {'workflow_run_id': row.id, 'answer': clarification}
    else:
        from backend.util.db import DBMemory
        from backend.everything import load_state_from_memory
        state = load_state_from_memory(DBMemory(db=db, doc_id=row.chat_thread_id))
        data['_confirmed_decision'] = decision
        pending = (state.get('active_workflow') or {}).get('pending_confirmation') or {}
        if pending.get('workflow_run_id') != row.id:
            raise ValueError('Confirmation is no longer active')
        data['_confirmed_original_decision'] = pending['original_decision']
    row.data_json, row.intent_json, row.result_json = service.encode(data), None, None
    row.status, row.phase, row.dispatch_pending = 'running', 'classify', True
    # Confirmation is an explicit user action starting the next planning task.
    # Invalidate the old message before dispatch, just as explicit Retry does.
    row.planning_task_id = None
    row.updated_at = row.last_progress_at = service.now()
    db.session.commit()
    tasks.dispatch_planning(row)
    return {'result_id': row.id, 'workflow_run_id': row.id}


@bp.get('/<workflow_id>/evidence')
@cognito_auth_required
@guarded
def evidence(workflow_id):
    """Page through original extraction results, independently of answer summaries.

    Ready query rows contain original ranges directly, or link to compact
    findings/legacy validated quotes in DocumentSummarySegment. Source
    versions are reauthorized on every request. The cursor is a stable SQL row
    ID, not a filename or array position. At most 20 sections are returned, so
    the complete report can grow without bloating routine progress polling.
    """
    from backend.case_assistant_tasks import work_rows
    from backend.util.prepared_retrieval import authorized_artifacts
    row = service.owned(workflow_id, current_user.id)
    snapshot = json.loads(row.data_json)['document_snapshot']
    validate_snapshot(row.user_id, row.chat_thread_id, snapshot)
    after = request.args.get('after', '')
    limit = min(20, max(1, int(request.args.get('limit', 10))))
    query = work_rows(row, 'query').filter_by(state='ready')
    records = query.filter(service.Work.id > after).order_by(service.Work.id).limit(limit+1).all()
    documents = authorized_artifacts(snapshot, file_ids=list({w.document_id for w in records[:limit]}))
    from backend.util.attachment_parser import normalized_extraction
    coverages = {uid: normalized_extraction(json.loads(coverage)['extraction']) for uid, coverage in db.session.query(
        DocumentPreparedArtifact.document_id, DocumentPreparedArtifact.coverage_json).filter(
        DocumentPreparedArtifact.id.in_([info['entry']['artifact_id'] for info in documents.values()])).all()}
    results = []
    for item in records[:limit]:
        file = db.session.query(UploadedFiles.original_filename, UploadedFiles.relative_path).filter_by(unique_id=item.document_id).one()
        from backend.util import source_context
        value = source_context.read_work_value(row, item)
        excerpts = source_context.record_excerpts(row, item, value)
        findings = []
        if value.get('format') == source_context.FINDINGS:
            doc = source_context.document(snapshot, item.document_id, item.artifact_id)
            blocks = {b['id']: b for b in doc['manifest']['blocks']}
            for finding in value['findings']:
                if set(finding['block_ids']) - blocks.keys():
                    raise ValueError('A finding references an unavailable source')
                findings.append({'statement': finding['statement'], 'references': [
                    {'block_id': bid, 'source_location': {k: v for k, v in blocks[bid].items()
                        if k in ('page', 'paragraph', 'part', 'sheet', 'row', 'cell', 'range', 'origin', 'start', 'end')}}
                    for bid in finding['block_ids']]})
        results.append({'section_id': item.id, 'file_id': item.document_id, 'artifact_id': item.artifact_id,
            'filename': file.original_filename, 'relative_path': file.relative_path,
            'scope': documents[item.document_id]['entry']['scope'], 'range': json.loads(item.material_json),
            'extraction_coverage': coverages[item.document_id],
            'processing_method': value.get('format', 'validated_quotations'),
            'excerpts': excerpts, 'findings': findings})
    required = work_rows(row, 'query').filter(service.Work.state != 'split')
    from backend.util.retrieval_review import summary
    return {'workflow_run_id': row.id, 'status': row.status, 'phase': row.phase, 'sections': results,
            'next': records[limit-1].id if len(records) > limit else None,
            'total_sections': query.count(), 'complete': row.status == 'completed',
            'required_sections': required.count(), 'extraction_complete': required.filter(service.Work.state != 'ready').count() == 0,
            'coverage_mode': (json.loads(row.payload_json)['intent_decision'].get('coverage_mode') if row.payload_json else None),
            'retrieval': summary(json.loads(row.payload_json) if row.payload_json else {})}
