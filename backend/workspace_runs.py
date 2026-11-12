"""HTTP access to saved prompt progress, actions and paginated source evidence.

The browser polls one Workflow.id, not every Celery task ID. These routes read
SQL via workspace_service; they do not wait for model calls. Retry and
confirmation save their changes before dispatching an agent task.
"""
from functools import wraps
import json
from flask import Blueprint, request, send_file
from backend.auth import auth_required, current_user
from sqlalchemy.exc import OperationalError, IntegrityError
from backend.database import db
from backend.models import WorkspaceRun as Workflow, UploadedFiles, DocumentPreparedArtifact
from backend.util import workspace_service as service
from backend.util.document_preparation import PreparationError, validate_snapshot, owned_scope
bp = Blueprint('workspace_services', __name__, url_prefix='/api/workspace/runs')

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
            return ({'msg': 'Request not found'}, 404)
        except PreparationError as exc:
            db.session.rollback()
            return (exc.payload, exc.status)
        except service.WorkflowStopped:
            db.session.rollback()
            return ({'msg': 'The request cannot be resumed. Refresh its documents and retry.'}, 409)
        except (ValueError, TypeError) as exc:
            db.session.rollback()
            return ({'code':'invalid_request','msg':str(exc)}, 400)
        except IntegrityError:
            db.session.rollback()
            return ({'msg': 'Another request is active in this chat.'}, 409)
        except OperationalError:
            db.session.rollback()
            return ({'msg': 'Request progress is unavailable. Check database migrations and retry.'}, 503)
    return call

@bp.get('/active')
@auth_required
@guarded
def active():
    """Find the chat's active prompt after reload without knowing a task ID."""
    _, draft_id, _ = owned_scope(current_user.id, 'chat', request.args.get('conversation_id'))
    row = Workflow.query.filter_by(user_id=current_user.id, active_chat_thread_id=draft_id).first()
    return {'workflow': service.status(row) if row else None}

@bp.get('/<workflow_id>')
@auth_required
@guarded
def progress(workflow_id):
    """Return aggregate SQL progress/result for one authorized submitted prompt."""
    return service.status(service.owned(workflow_id, current_user.id))

@bp.get('/<workflow_id>/artifacts/<artifact_id>')
@auth_required
@guarded
def agent_artifact(workflow_id, artifact_id):
    from backend.models import AgentArtifact
    row = service.owned(workflow_id, current_user.id)
    validate_snapshot(row.user_id, row.chat_thread_id, json.loads(row.data_json)['document_snapshot'])
    artifact = AgentArtifact.query.filter_by(id=artifact_id, run_id=row.id).first()
    if artifact is None and db.session.get(AgentArtifact, artifact_id) is None:
        raise service.WorkflowNotFound('Artifact not found')
    if artifact is None or artifact.kind in {'authority_research', 'authority_source'}:
        from types import SimpleNamespace
        from backend.domains.legal.authority_research import resolve_artifact
        value = resolve_artifact(SimpleNamespace(row=row, snapshot=json.loads(row.data_json)['document_snapshot']), artifact_id)
        return {'id': artifact_id, 'kind': value['kind'], 'content': value}
    if artifact.kind in {'python_execution', 'derived_file'}:
        from backend.util.prepared_retrieval import authorized_artifacts
        authorized_artifacts(json.loads(row.data_json)['document_snapshot'])
    return {'id': artifact.id, 'kind': artifact.kind, 'content': json.loads(artifact.content_json)}

@bp.get('/<workflow_id>/artifacts/<artifact_id>/files/<file_id>')
@auth_required
@guarded
def agent_generated_file(workflow_id, artifact_id, file_id):
    from backend.util.agent_python import downloadable_file
    from backend.util.prepared_retrieval import authorized_artifacts
    row = service.owned(workflow_id, current_user.id)
    authorized_artifacts(json.loads(row.data_json)['document_snapshot'])
    path, filename = downloadable_file(row, artifact_id, file_id)
    return send_file(path, as_attachment=True, download_name=filename, conditional=True)

@bp.post('/<workflow_id>/cancel')
@auth_required
@guarded
def cancel(workflow_id):
    """Save cancellation even if Redis revocation cannot reach the workers."""
    row = service.owned(workflow_id, current_user.id)
    service.cancel(row)
    return {'run_id': row.id, 'status': row.status, 'cancelled': row.status == 'cancelled'}

@bp.post('/<workflow_id>/retry')
@auth_required
@guarded
def retry(workflow_id):
    from backend.agent_tasks import dispatch
    row=service.owned(workflow_id,current_user.id)
    service.retry(row);dispatch(row)
    return {'run_id':row.id}


@bp.post('/<workflow_id>/confirm')
@auth_required
@guarded
def confirm(workflow_id):
    from backend.agent_tasks import answer_question
    row=service.owned(workflow_id,current_user.id)
    decision=(request.get_json(silent=True) or {}).get('decision') or {}
    if decision.get('cancelled'):
        service.cancel(row);return {'run_id':row.id,'status':row.status,'cancelled':row.status=='cancelled'}
    answer_question(row,decision)
    return {'run_id':row.id}
