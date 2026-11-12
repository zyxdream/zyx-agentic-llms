"""Register old uploads explicitly, without doing ingestion in an HTTP request.

The CLI and Retry share this path. Opening a chat only reads the catalogue; it
must never register old files or start provider work as a side effect of polling.
"""
import hashlib
import json
import uuid
from pathlib import Path

from backend.database import db
from backend.models import (DocumentPreparation, DocumentPreparedArtifact,
                            ChatThread, Project, TextEntry, UploadedFiles, UploadGroup)
from backend.util.attachment_parser import unverified_manifest
from backend.util import document_preparation as prep


def existing_scope(file, owner_id=None):
    """Derive authorization from the saved file, never from a supplied scope."""
    draft = db.session.get(ChatThread, file.chat_thread_id) if file and file.chat_thread_id else None
    project = db.session.get(Project, draft.projectId if draft else file.project_id) if file else None
    if (not file or file.source == 'search' or (file.chat_thread_id and (not draft or draft.docCategory != 69))
            or not project or (owner_id is not None and project.userId != owner_id)):
        raise prep.PreparationError('not_found', 'Document not found.', 404)
    return project, draft


def register_existing_document(file_id, owner_id=None):
    """Return (document, changed), committing before any Celery dispatch.

    Serialize Retry and CLI registration on the same project lock as uploads.
    This works on SQLite as well as MySQL and keeps the original document ID.
    Existing active work and published current artifacts are reused.
    """
    file = db.session.get(UploadedFiles, file_id)
    project, _ = existing_scope(file, owner_id)
    project_id = project.id
    db.session.commit()
    prep._lock_scope(project_id)
    file = db.session.get(UploadedFiles, file_id, populate_existing=True)
    project, draft = existing_scope(file, owner_id)
    if project.id != project_id:
        raise prep.PreparationError('not_found', 'Document scope changed.', 404)
    previous = DocumentPreparation.query.filter_by(id=file.unique_id).populate_existing().first()
    if previous:
        prep.owned_group(previous.group_id, project.userId)
        artifact = db.session.get(DocumentPreparedArtifact, previous.artifact_id) if previous.artifact_id else None
        if previous.status in prep.ACTIVE or previous.status == 'cancelled' or (
                previous.status == 'ready' and artifact and artifact.preparation_version == prep.VERSION):
            db.session.commit()
            return previous, False
        manifest = json.loads(previous.parse_manifest_json or '{}')
        if previous.status == 'failed' and manifest.get('parser_version') == 'attachment-blocks-v2':
            # Normal failures resume saved parsing/summary checkpoints.
            prep.retry_document(previous)
            return previous, True

    text = file.content or ''
    has_source = bool(previous and previous.source_key and Path(previous.source_key).is_file())
    if previous is None:
        key = f'chat:{draft.id}' if draft else f'matter:{project.id}'
        group = UploadGroup(id=str(uuid.uuid4()), owner_id=project.userId, project_id=project.id,
                            chat_thread_id=draft.id if draft else None, scope_key=key,
                            request_key=f'backfill:{file.id}',
                            manifest_hash=hashlib.sha256(file.unique_id.encode()).hexdigest(), created_at=prep.now())
        db.session.add(group)
        try:
            from backend.util.workspace_store import load
            used = bool(load(draft.id).get('chat_history')) if draft else False
        except (ValueError, TypeError, AttributeError):
            used = False
        previous = DocumentPreparation(id=file.unique_id, group_id=group.id, scope_key=key,
            client_upload_id=f'backfill:{file.id}', filename=file.original_filename,
            relative_path=file.relative_path, folder_upload_id=file.folder_upload_id,
            file_size=file.file_size, uploaded_file_id=file.id, created_at=prep.now(),
            first_attached_at=prep.now() if used else None)
        db.session.add(previous)
    else:
        previous.generation += 1
    previous.attempts = 0
    previous.lease_token = previous.lease_until = previous.ready_at = None
    previous.artifact_id = previous.coverage_json = previous.coverage_summary_json = None
    previous.warning = None
    previous.parsed_at = prep.now() if text.strip() and not has_source else None
    # The worker builds the current exact-source index. Avoid tokenizing the
    # entire old document or loading all old chunks inside the HTTP/SQL lock.
    previous.indexed_at = None
    previous.checkpoint = 'parsed' if previous.parsed_at else 'queued'
    if has_source:
        previous.parse_manifest_json = json.dumps({'parser_version': 'attachment-blocks-v2', 'backfill_pending': True})
    else:
        previous.source_key = None
        previous.source_hash = hashlib.sha256(text.encode()).hexdigest()
        previous.parse_manifest_json = json.dumps({k: v for k, v in unverified_manifest(text).items() if k != 'text'})
    previous.status = 'queued' if has_source or text.strip() else 'failed'
    previous.dispatch_pending = previous.status == 'queued'
    previous.error_code = None if previous.dispatch_pending else 'source_missing'
    previous.error_message = None if previous.dispatch_pending else 'Original source is unavailable. Re-upload this document.'
    previous.updated_at = prep.now()
    db.session.commit()
    prep.dispatch_document(previous.id)
    return previous, True


def forget_chat_file(draft_id, document_id):
    """Remove legacy input/cache references in the caller's deletion transaction.

    Historical messages remain intact. Only reusable upload state is changed so
    a deleted attachment cannot reappear as available evidence after a reload.
    """
    if not draft_id:
        return
    TextEntry.query.filter_by(chat_thread_id=draft_id, textName=f'inputs/{document_id}').delete(synchronize_session=False)
    from backend.util.workspace_store import patch_auxiliary
    state = patch_auxiliary(draft_id, {})
    files = state.get('uploaded_files')
    if isinstance(files, dict) and document_id in files:
        files = {key: value for key, value in files.items() if key != document_id}
        patch_auxiliary(draft_id, {'uploaded_files': files,
            'total_page_count': sum(info.get('page_count', 0) or 0 for info in files.values() if isinstance(info, dict))})


def remove_existing_document(file_id, owner_id):
    file = db.session.get(UploadedFiles, file_id)
    project, _ = existing_scope(file, owner_id)
    project_id = project.id
    db.session.commit()
    prep._lock_scope(project_id)
    file = db.session.get(UploadedFiles, file_id, populate_existing=True)
    project, _ = existing_scope(file, owner_id)
    if project.id != project_id:
        raise prep.PreparationError('not_found', 'Document scope changed.', 404)
    # Registration may have won the lock while the Remove request was waiting.
    doc = DocumentPreparation.query.filter_by(id=file.unique_id).populate_existing().first()
    if doc:
        prep.owned_group(doc.group_id, owner_id)
        return prep.cancel_document(doc)
    document_id = file.unique_id
    forget_chat_file(file.chat_thread_id, document_id)
    db.session.delete(file)
    db.session.commit()
    return {'document_id': document_id, 'status': 'cancelled'}
