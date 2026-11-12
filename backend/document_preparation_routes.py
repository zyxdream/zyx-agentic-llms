"""HTTP transport for preparation; no parsing or LLM work runs in Gunicorn."""
from functools import wraps
import json
import uuid
from flask import Blueprint, request
from backend.auth import auth_required, current_user
from sqlalchemy.exc import IntegrityError, OperationalError
from backend.database import db
from backend.models import DocumentPreparation, DocumentPreparedArtifact, UploadGroup, UploadedFiles
from backend.util import document_preparation as prep
from backend.util.attachment_parser import normalized_coverage
bp = Blueprint('document_preparation', __name__, url_prefix='/api/workspace')

def guarded(fn):

    @wraps(fn)
    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except prep.PreparationError as exc:
            db.session.rollback()
            return (exc.payload, exc.status)
        except (TypeError, ValueError, KeyError):
            db.session.rollback()
            return ({'code': 'invalid_request', 'msg': 'Invalid document preparation request.'}, 400)
        except IntegrityError:
            db.session.rollback()
            return ({'code': 'idempotency_conflict', 'msg': 'This upload was already admitted. Refresh its status and retry.'}, 409)
        except OperationalError:
            db.session.rollback()
            return ({'code': 'preparation_busy', 'msg': 'Document preparation is busy. Please retry.'}, 503)
    return call

@bp.post('/upload-groups')
@auth_required
@guarded
def create_group():
    data = request.get_json() or {}
    return (prep.admit_group(current_user.id, data.get('scope'), data.get('scopeId'), data.get('selectionId'), data.get('documents'), data.get('folderId'), folder_name=data.get('folderName')), 201)

def transfer(group):
    files = request.files.getlist('files')
    ids = json.loads(request.form.get('clientUploadIds', '[]'))
    return transfer_files(group, files, ids)

def transfer_files(group, files, ids):
    limits = prep.policy()
    if not files or not isinstance(ids, list) or (not all((isinstance(i, str) for i in ids))) or (len(files) > limits['max_files']) or (len(files) != len(ids)) or (len(set(ids)) != len(ids)):
        raise prep.PreparationError('group_limit', 'Invalid upload group or too many files.', 400)
    total = 0
    for file in files:
        file.stream.seek(0, 2)
        total += file.stream.tell()
        file.stream.seek(0)
    if total > limits['max_bytes']:
        raise prep.PreparationError('group_limit', 'Upload group exceeds its byte limit.', 413)
    accepted, failed = ([], [])
    for file, client_id in zip(files, ids):
        try:
            accepted.append(prep.stage_transfer(group, client_id, file))
        except prep.PreparationError as exc:
            db.session.rollback()
            record_transfer_failure(group.id, client_id, exc.payload['code'], exc.payload['msg'])
            failed.append({'clientUploadId': client_id, **exc.payload})
        except OSError:
            db.session.rollback()
            record_transfer_failure(group.id, client_id, 'storage_failed', 'Document source could not be stored. Retry the upload.')
            failed.append({'clientUploadId': client_id, 'code': 'storage_failed', 'msg': 'Document source could not be stored. Retry the upload.'})
    folder_id = next((d['folder_id'] for d in accepted if d['folder_id']), None)
    return ({'group_id': group.id, 'folder_id': folder_id, 'documents': accepted, 'failed': failed, 'uploads': [{'clientUploadId': d['clientUploadId'], 'unique_id': d['document_id'], 'filename': d['filename'], 'task_id': d['task_id']} for d in accepted]}, 202)

def record_transfer_failure(group_id, client_id, code, message):
    DocumentPreparation.query.filter_by(group_id=group_id, client_upload_id=client_id, source_hash=None, status='queued').update({'status': 'failed', 'error_code': code, 'error_message': message, 'generation': DocumentPreparation.generation + 1, 'updated_at': prep.now(), 'dispatch_pending': False}, synchronize_session=False)
    db.session.commit()

@bp.post('/upload-groups/<group_id>/files')
@auth_required
@guarded
def transfer_group(group_id):
    return transfer(prep.owned_group(group_id, current_user.id))

@bp.get('/upload-groups/<group_id>')
@auth_required
@guarded
def group_status(group_id):
    return prep.group_view(prep.owned_group(group_id, current_user.id))

@bp.get('/document-preparation')
@auth_required
def preparation_status():
    return _preparation_status()

@guarded
def _preparation_status():
    return prep.catalog(current_user.id, request.args.get('conversation_id'), request.args.get('projectId'))

def owned_document(document_id, *, allow_existing=False):
    doc = db.session.get(DocumentPreparation, document_id)
    if doc is None: raise prep.PreparationError('not_found', 'Document not found.', 404)
    prep.owned_group(doc.group_id, current_user.id)
    return doc


@bp.get('/sources/<document_id>/summary')
@auth_required
@guarded
def document_summary(document_id):
    doc = owned_document(document_id)
    artifact = db.session.get(DocumentPreparedArtifact, request.args.get('artifactId') or doc.artifact_id) if request.args.get('artifactId') or doc.artifact_id else None
    if doc.status == 'cancelled' or not artifact or artifact.document_id != doc.id:
        raise prep.PreparationError('not_found', 'Prepared summary is unavailable.', 404)
    result = json.loads(artifact.result_json)
    blocks = {b['id']: b for b in json.loads(artifact.manifest_json).get('blocks', [])}
    for point in result.get('key_points', []):
        if 'block_ids' in point:
            point['source_locations'] = [{k: blocks[uid][k] for k in ('page', 'paragraph', 'part', 'sheet', 'row', 'cell', 'range') if k in blocks[uid]} for uid in point['block_ids'] if uid in blocks]
    result.pop('parse_manifest', None)
    return {'document_id': doc.id, 'artifact_id': artifact.id, 'filename': doc.filename, **result, 'coverage': normalized_coverage(json.loads(artifact.coverage_json))}

@bp.post('/sources/<document_id>/retry')
@auth_required
@guarded
def retry_document(document_id):
    doc = owned_document(document_id)
    data=request.get_json(silent=True) or {}
    prep.retry_document(doc, expected_generation=data.get('generation'))
    return prep.document_view(doc), 202


@bp.delete('/sources/<document_id>')
@auth_required
@guarded
def remove_document(document_id):
    doc = owned_document(document_id)
    prep.cancel_document(doc)
    return {'deleted': True, 'source_id': document_id}


def register(app):

    class PreparationRequest(app.request_class):

        @property
        def max_content_length(self):
            return self.environ.get('zyx.document_body_limit', super().max_content_length)
    app.request_class = PreparationRequest
    app.register_blueprint(bp)

    @app.before_request
    def bound_preparation_request():
        if prep.enabled() and request.method == 'POST' and (request.path.startswith('/api/workspace/upload-groups') or request.path in {'/api/workspace/upload-files-async', '/api/upload-file-matter', '/api/upload-folder-matter'}):
            request.environ['zyx.document_body_limit'] = prep.policy()['max_bytes'] + 2 * 1024 ** 2
