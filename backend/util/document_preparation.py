"""Transactional admission, durable progress and authorized document snapshots."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import uuid

from flask import current_app, has_app_context
from sqlalchemy import func, or_
from sqlalchemy.orm import load_only, defer

from backend.database import db
from backend.models import (DocumentPreparation, DocumentPreparedArtifact, DocumentSummarySegment, ChatThread, FolderUpload,
                            Project, UploadedFiles, UploadGroup)
from backend.util.document_metadata import parse_metadata
from backend.util.attachment_parser import normalized_extraction, extraction_warning

ACTIVE = {'queued', 'parsing', 'indexing', 'summarizing'}
TERMINAL = {'ready', 'failed', 'cancelled'}
VERSION = 'luna-source-records-v2'


class PreparationError(Exception):
    def __init__(self, code, message, status=409, **details):
        super().__init__(message)
        self.status = status
        self.payload = {'code': code, 'msg': message, **details}


def now():
    return datetime.utcnow()


def enabled():
    # Mandatory for attachment/matter ingestion, independent of environment flags.
    return has_app_context()


def policy():
    return {
        'max_files': int(current_app.config.get('DOCUMENT_UPLOAD_GROUP_FILES', 5)),
        'max_bytes': int(current_app.config.get('DOCUMENT_UPLOAD_GROUP_BYTES', 100 * 1024**2)),
        'concurrency': int(current_app.config.get('DOCUMENT_UPLOAD_CONCURRENCY', 2)),
        'chat_file_bytes': 50 * 1024**2,
        'matter_file_bytes': 100 * 1024**2,
        'chat_files': int(current_app.config.get('DOCUMENT_CHAT_FILE_LIMIT', 100)),
        'matter_files': int(current_app.config.get('DOCUMENT_MATTER_FILE_LIMIT', 30)),
        'total_bytes': int(current_app.config.get('DOCUMENT_TOTAL_BYTES', 1024**3)),
    }


def queue_name():
    default = current_app.config['CELERY']['task_default_queue']
    queue = current_app.config.get('DOCUMENT_PREPARATION_QUEUE', default + '.documents')
    isolated = current_app.config.get('E2E_CELERY_QUEUE')
    if isolated and not queue.startswith(isolated + '.'):
        raise ValueError('E2E document preparation must stay on a queue within its isolated E2E namespace.')
    return queue


def owned_scope(owner_id, scope, scope_id):
    if scope == 'chat':
        try:
            draft_id = ChatThread.resolve_id(scope_id)
        except (ValueError, TypeError):
            raise PreparationError('not_found', 'Chat not found.', 404)
        draft = db.session.get(ChatThread, draft_id)
        project = db.session.get(Project, draft.projectId) if draft else None
    elif scope == 'matter':
        try:
            project = db.session.get(Project, int(scope_id))
        except (TypeError, ValueError):
            project = None
        draft_id = None
    else:
        raise PreparationError('invalid_scope', 'Choose a chat or matter scope.', 400)
    if not project or project.userId != owner_id:
        raise PreparationError('not_found', 'Document scope not found.', 404)
    return project.id, draft_id, f'chat:{draft_id}' if draft_id else f'matter:{project.id}'


def _lock_scope(project_id):
    # A write before quota reads serializes SQLite too; SELECT FOR UPDATE alone
    # is ignored there. No parsing, source I/O or provider request holds this lock.
    from backend.workspace.project_access import lock_project, ProjectMissing
    try:
        lock_project(project_id)
    except ProjectMissing as exc:
        raise PreparationError('not_found', 'Project no longer exists.', 404) from exc


def _canonical_filter(project_id, draft_id):
    if draft_id:
        return (UploadedFiles.chat_thread_id == draft_id,)
    return (UploadedFiles.project_id == project_id, UploadedFiles.chat_thread_id.is_(None),
            or_(UploadedFiles.source != 'search', UploadedFiles.source.is_(None)))


def admit_group(owner_id, scope, scope_id, request_key, manifest, folder_id=None, *, folder_name=None):
    project_id, draft_id, scope_key = owned_scope(owner_id, scope, scope_id)
    limits = policy()
    if not isinstance(request_key, str) or not 1 <= len(request_key) <= 100:
        raise PreparationError('invalid_manifest', 'A stable selection ID is required.', 400)
    limit = limits['chat_files' if draft_id else 'matter_files']
    if folder_name and (draft_id or not isinstance(folder_name, str) or len(folder_name) > 500):
        raise PreparationError('invalid_manifest', 'Invalid folder name.', 400)
    if not isinstance(manifest, list) or not manifest or len(manifest) > limit:
        raise PreparationError('file_count_limit', f'Select between 1 and {limit} documents.', 400)
    if folder_id:
        folder = db.session.get(FolderUpload, int(folder_id))
        if not folder or folder.project_id != project_id or folder.chat_thread_id is not None or draft_id:
            raise PreparationError('not_found', 'Folder not found.', 404)
    clean, clients = [], set()
    for entry in manifest:
        if not isinstance(entry, dict):
            raise PreparationError('invalid_manifest', 'Invalid document manifest.', 400)
        client_id = entry.get('client_upload_id')
        filename = os.path.basename(str(entry.get('filename', '')).replace('\\', '/'))
        relative = entry.get('relative_path') or None
        size = entry.get('file_size')
        if (not isinstance(client_id, str) or not 1 <= len(client_id) <= 100
                or client_id in clients or not filename or len(filename) > 500
                or filename.startswith('~$') or filename.rsplit('.', 1)[-1].lower() not in {'pdf', 'doc', 'docx', 'txt', 'xlsx', 'csv'}
                or not isinstance(size, int) or isinstance(size, bool) or size <= 0
                or size > limits['chat_file_bytes' if draft_id else 'matter_file_bytes']):
            raise PreparationError('invalid_manifest', 'Invalid, duplicate, unsupported or oversized document.', 400)
        if relative:
            relative = str(relative).replace('\\', '/')
            if len(relative) > 1000 or relative.startswith('/') or '..' in relative.split('/'):
                raise PreparationError('invalid_manifest', 'Invalid document path.', 400)
        clients.add(client_id)
        clean.append(dict(client_upload_id=client_id, filename=filename, file_size=size, relative_path=relative))
    digest = hashlib.sha256(json.dumps([scope_key, folder_id, folder_name, clean], sort_keys=True).encode()).hexdigest()
    # End any ownership-read transaction before taking the admission write lock.
    db.session.commit()
    _lock_scope(project_id)
    owned_scope(owner_id, scope, scope_id)
    existing = UploadGroup.query.filter_by(owner_id=owner_id, request_key=request_key).first()
    if existing:
        if existing.manifest_hash != digest:
            raise PreparationError('idempotency_conflict', 'Selection ID was already used for different documents.')
        db.session.commit()
        return group_view(existing)
    if DocumentPreparation.query.filter(DocumentPreparation.scope_key == scope_key,
                                        DocumentPreparation.client_upload_id.in_(clients)).first():
        raise PreparationError('idempotency_conflict', 'Upload IDs already belong to another selection.')
    canonical = db.session.query(UploadedFiles.file_size.label('file_size')).filter(
        *_canonical_filter(project_id, draft_id))
    reservations = db.session.query(DocumentPreparation.file_size.label('file_size')).filter(
        DocumentPreparation.scope_key == scope_key, DocumentPreparation.uploaded_file_id.is_(None),
        DocumentPreparation.status != 'cancelled')
    # Read both sets in one statement so a worker publishing a canonical file
    # cannot move bytes between two separate quota reads, even at READ COMMITTED.
    corpus = canonical.union_all(reservations).subquery()
    used_count, used_bytes = db.session.query(
        func.count(), func.coalesce(func.sum(corpus.c.file_size), 0)).select_from(corpus).one()
    if used_count + len(clean) > limit:
        raise PreparationError('file_count_limit', f'Maximum {limit} documents allowed in this scope.', 400,
                               remaining=max(0, limit - used_count))
    used_bytes = int(used_bytes)
    selection_bytes = sum(entry['file_size'] for entry in clean)
    if used_bytes + selection_bytes > limits['total_bytes']:
        raise PreparationError('total_size_limit',
                               f'Maximum {limits["total_bytes"] / 1024**3:g} GB total per document collection, including pending and failed uploads. Remove documents before adding more.', 400,
                               limit_bytes=limits['total_bytes'], used_bytes=used_bytes,
                               remaining_bytes=max(0, limits['total_bytes'] - used_bytes),
                               selection_bytes=selection_bytes)
    group = UploadGroup(id=str(uuid.uuid4()), owner_id=owner_id, project_id=project_id,
                        chat_thread_id=draft_id, scope_key=scope_key, request_key=request_key,
                        manifest_hash=digest, created_at=now())
    db.session.add(group)
    if folder_name and not folder_id:
        folder = FolderUpload(folder_name=folder_name, project_id=project_id)
        db.session.add(folder)
        db.session.flush()
        folder_id = folder.id
    for entry in clean:
        db.session.add(DocumentPreparation(id=str(uuid.uuid4()), group_id=group.id, scope_key=scope_key,
                        folder_upload_id=folder_id, created_at=now(), updated_at=now(), **entry))
    db.session.commit()
    return group_view(group)


def owned_group(group_id, owner_id):
    group = db.session.get(UploadGroup, group_id)
    if not group or group.owner_id != owner_id:
        raise PreparationError('not_found', 'Upload selection not found.', 404)
    owned_scope(owner_id, 'chat' if group.chat_thread_id else 'matter', group.chat_thread_id or group.project_id)
    return group


def document_view(doc, file=None, artifact=None):
    artifact = artifact or (db.session.get(DocumentPreparedArtifact, doc.artifact_id, options=[load_only(
        DocumentPreparedArtifact.id, DocumentPreparedArtifact.preparation_version, DocumentPreparedArtifact.card_json)]) if doc.artifact_id else None)
    metadata = json.loads(artifact.card_json) if artifact else None
    compact = ({key: metadata[key] for key in ('schema_version', 'status', 'headline', 'document_kind',
                'preview', 'summary', 'key_points', 'parties', 'dates', 'topics', 'preparation_version',
                'summary_format_revision', 'summary_basis', 'summary_exhaustive', 'spreadsheet') if key in metadata}
               if metadata else None)
    needs_backfill = doc.status == 'ready' and (not artifact or artifact.preparation_version != VERSION)
    # Normalize the full coverage metadata before taking three example gaps:
    # historical vector warnings must not hide or inflate real unreadable pages.
    coverage = compact_coverage(json.loads(doc.coverage_json)) if doc.coverage_json else None
    return {
        'document_id': doc.id, 'unique_id': doc.id, 'group_id': doc.group_id,
        'clientUploadId': doc.client_upload_id, 'filename': doc.filename,
        'file_size': doc.file_size, 'relative_path': doc.relative_path,
        'folder_id': doc.folder_upload_id, 'scope': 'chat' if doc.scope_key.startswith('chat:') else 'matter',
        'status': 'failed' if needs_backfill else doc.status,
        'stage': 'backfill_required' if needs_backfill else 'awaiting_upload' if doc.status == 'queued' and not doc.source_key else doc.status,
        'generation': doc.generation, 'task_id': doc.task_id,
        'committed': bool(doc.first_attached_at),
        'error_code': 'backfill_required' if needs_backfill else doc.error_code,
        'error': 'This document needs the current preparation version. Retry preparation.' if needs_backfill else doc.error_message,
        'warning': extraction_warning(coverage['extraction']) if coverage else doc.warning,
        'artifact_id': artifact.id if artifact else None,
        'needs_reupload': not doc.parsed_at and doc.status != 'ready' and
                          (not doc.source_key or doc.error_code in {'source_missing', 'source_corrupt'}),
        'metadata': compact,
        'coverage': coverage,
    }


def compact_coverage(coverage):
    extraction = normalized_extraction(coverage.get('extraction'))
    outcomes = extraction.get('outcomes') or []
    fields = ('preparation_version', 'summary_format_revision', 'model', 'source_tokens', 'covered_tokens', 'total_segments',
              'completed_segments', 'source_characters', 'covered_characters', 'generated_at',
              'summary_basis', 'summary_exhaustive', 'profile_tokens')
    return {**{k: coverage[k] for k in fields if k in coverage}, 'extraction': {
        'status': extraction.get('status', 'unverified'), 'gap_fingerprint': extraction.get('gap_fingerprint'),
        'gap_count': len(extraction.get('gaps', [])), 'gaps': extraction.get('gaps', [])[:3],
        'total_pages': len(outcomes), 'readable_pages': sum(o.get('status') in {'native', 'ocr', 'verified_blank'} for o in outcomes)}}


def group_view(group):
    docs = DocumentPreparation.query.filter_by(group_id=group.id).order_by(DocumentPreparation.created_at, DocumentPreparation.id).all()
    files = {f.id: f for f in UploadedFiles.query.options(load_only(UploadedFiles.id, UploadedFiles.metadata_json)).filter(
        UploadedFiles.id.in_([d.uploaded_file_id for d in docs if d.uploaded_file_id])).all()}
    return {'enabled': True, 'group_id': group.id, 'policy': policy(),
            'documents': [document_view(d, files.get(d.uploaded_file_id)) for d in docs],
            'counts': dict(Counter(d.status for d in docs))}


def source_root():
    root = Path(current_app.config.get('DOCUMENT_PREPARATION_SOURCE_DIR',
                                      os.path.join(current_app.instance_path, 'document_sources'))).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def stage_transfer(group, client_id, upload):
    doc = DocumentPreparation.query.filter_by(group_id=group.id, client_upload_id=client_id).first()
    if not doc or doc.status == 'cancelled':
        raise PreparationError('not_found', 'Document was not admitted or has been removed.', 404)
    if doc.status == 'failed' and not doc.source_hash:
        raise PreparationError('invalid_transition', 'Retry this document before transferring its source again.')
    # File I/O is outside a database transaction. Publication below is fenced.
    document_id, generation, expected_size = doc.id, doc.generation, doc.file_size
    project_id = group.project_id
    filename = doc.filename
    if os.path.basename((upload.filename or '').replace('\\', '/')) != filename:
        raise PreparationError('idempotency_conflict', 'Filename does not match the admitted document.')
    db.session.commit()
    path = source_root() / f'{uuid.uuid4()}.{filename.rsplit(".", 1)[-1]}'
    digest, size = hashlib.sha256(), 0
    try:
        with open(path, 'xb') as target:
            os.chmod(path, 0o600)
            while True:
                block = upload.stream.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > expected_size:
                    raise PreparationError('size_mismatch', 'Received document exceeds its admitted size.', 400)
                target.write(block)
                digest.update(block)
        if size != expected_size:
            raise PreparationError('size_mismatch', 'Received document size does not match its manifest.', 400)
        _lock_scope(project_id)
        doc = DocumentPreparation.query.filter_by(id=document_id).with_for_update().populate_existing().one()
        if doc.generation != generation or doc.status == 'cancelled':
            raise PreparationError('stale_upload', 'The upload was cancelled or retried while transferring.')
        if doc.source_hash:
            if doc.source_hash != digest.hexdigest():
                raise PreparationError('idempotency_conflict', 'Upload ID was already used for different file content.')
            db.session.commit()
            return document_view(doc)
        doc.source_key, doc.source_hash = str(path), digest.hexdigest()
        doc.dispatch_pending, doc.updated_at = True, now()
        db.session.commit()
        path = None  # published source is retained for recovery
        dispatch_document(document_id)
        return document_view(db.session.get(DocumentPreparation, document_id))
    finally:
        if path:
            path.unlink(missing_ok=True)


def dispatch_document(document_id):
    """The intent remains pending on publish failure; beat retries it."""
    from backend.document_preparation_tasks import prepare_document
    doc = db.session.get(DocumentPreparation, document_id)
    if not doc or not doc.dispatch_pending or doc.status not in ACTIVE:
        return
    generation = doc.generation
    previous_task = doc.task_id
    task_id = str(uuid.uuid4())
    reserved = DocumentPreparation.query.filter_by(id=document_id, generation=generation, dispatch_pending=True,
        lease_token=None, task_id=previous_task).filter(DocumentPreparation.status.in_(ACTIVE)).update({
            'task_id': task_id, 'dispatched_at': now()}, synchronize_session=False)
    db.session.commit()
    if not reserved:
        return
    try:
        prepare_document.apply_async(args=[document_id, generation], task_id=task_id, queue=queue_name())
    except Exception:
        current_app.logger.exception('Document preparation dispatch failed: %s', document_id)
        return
    # A fast worker may already have scheduled another bounded unit. Only clear
    # this publication's intent; never replace the newer task/dispatch state.
    DocumentPreparation.query.filter_by(id=document_id, generation=generation, task_id=task_id).update(
        {'dispatch_pending': False})
    db.session.commit()


def catalog(owner_id, draft_id=None, project_id=None):
    project_id, internal_id, _ = owned_scope(owner_id, 'chat' if draft_id else 'matter', draft_id or project_id)
    groups = UploadGroup.query.filter_by(owner_id=owner_id, project_id=project_id).filter(
        or_(UploadGroup.chat_thread_id == internal_id, UploadGroup.chat_thread_id.is_(None))).all()
    docs = DocumentPreparation.query.options(defer(DocumentPreparation.parse_manifest_json)).filter(DocumentPreparation.group_id.in_([g.id for g in groups]),
                                             DocumentPreparation.status != 'cancelled').all()
    matter_condition = (UploadedFiles.project_id == project_id) & UploadedFiles.chat_thread_id.is_(None) & or_(
        UploadedFiles.source != 'search', UploadedFiles.source.is_(None))
    condition = or_(UploadedFiles.chat_thread_id == internal_id, matter_condition) if internal_id else matter_condition
    files = UploadedFiles.query.options(load_only(
        UploadedFiles.id, UploadedFiles.unique_id, UploadedFiles.original_filename,
        UploadedFiles.file_size, UploadedFiles.relative_path, UploadedFiles.folder_upload_id,
        UploadedFiles.chat_thread_id, UploadedFiles.metadata_json)).filter(condition).all()
    by_id = {f.id: f for f in files}
    artifacts = {a.id: a for a in DocumentPreparedArtifact.query.options(load_only(
        DocumentPreparedArtifact.id, DocumentPreparedArtifact.preparation_version, DocumentPreparedArtifact.card_json)).filter(
        DocumentPreparedArtifact.id.in_([d.artifact_id for d in docs if d.artifact_id])).all()}
    views = [document_view(d, by_id.get(d.uploaded_file_id), artifacts.get(d.artifact_id)) for d in docs]
    linked = {d.uploaded_file_id for d in docs}
    # Existing rows must be backfilled, never silently declared prepared.
    for file in files:
        if file.id not in linked:
            views.append({'document_id': file.unique_id, 'unique_id': file.unique_id,
                          'filename': file.original_filename, 'file_size': file.file_size,
                          'relative_path': file.relative_path, 'folder_id': file.folder_upload_id,
                          'scope': 'chat' if file.chat_thread_id else 'matter', 'status': 'failed',
                          'stage': 'failed', 'error_code': 'backfill_required',
                          'error': 'Re-upload this source to prepare it in Workspace.',
                          'metadata': parse_metadata(file.metadata_json)})
    folder_ids = {d['folder_id'] for d in views if d.get('folder_id')}
    folder_names = dict(db.session.query(FolderUpload.id, FolderUpload.folder_name).filter(
        FolderUpload.project_id == project_id, FolderUpload.id.in_(folder_ids)).all()) if folder_ids else {}
    for view in views:
        if view.get('folder_id'):
            view['folder_name'] = folder_names.get(view['folder_id'])
    views.sort(key=lambda d: d['document_id'])
    revision = hashlib.sha256(json.dumps(views, sort_keys=True).encode()).hexdigest()
    return {'enabled': True, 'owner_id': owner_id, 'documents': views, 'policy': policy(), 'revision': revision,
            'counts': dict(Counter(d['status'] for d in views))}


def build_snapshot(owner_id, draft_id, data):
    try:
        resolved_draft_id = ChatThread.resolve_id(draft_id)
    except (ValueError, TypeError) as exc:
        raise PreparationError('not_found', 'Conversation no longer exists.', 404) from exc
    view = catalog(owner_id, resolved_draft_id)
    raw_exclusions = data.get('excludedDocumentIds') or []
    if not isinstance(raw_exclusions, list) or not all(isinstance(x, str) for x in raw_exclusions):
        raise PreparationError('invalid_scope', 'Invalid document exclusions.', 400)
    exclusions = set(raw_exclusions)
    chat_files = data.get('chatFiles') or []
    attachments = data.get('promptAttachments') or []
    if (not isinstance(chat_files, list) or not isinstance(attachments, list)
            or any(not isinstance(f, dict) or not isinstance(f.get('name'), str) for f in chat_files)
            or any(not isinstance(f, dict) or not isinstance(f.get('file_id'), str) for f in attachments)):
        raise PreparationError('invalid_scope', 'Invalid attachment selection.', 400)
    chat_ids = {f['name'] for f in chat_files}
    current_ids = {f['file_id'] for f in attachments}
    chat_ids |= current_ids
    available = {d['document_id']: d for d in view['documents']}
    if (chat_ids | exclusions) - available.keys():
        raise PreparationError('stale_scope', 'Some documents are no longer available in this scope.')
    selected = [d for d in view['documents'] if d['document_id'] not in exclusions and
                (d['scope'] == 'matter' or d['document_id'] in chat_ids or d['status'] != 'ready')]
    blocked = [d for d in selected if d['status'] != 'ready']
    if blocked:
        raise PreparationError('documents_not_ready', 'Wait for document preparation, retry failures or explicitly exclude them.',
                               blocked_documents=blocked)
    # Legacy clients may still send acceptedCoverageGaps. Readiness depends on
    # preparation, not approval of best-effort extraction; ignore that field.
    rows = {d.id: d for d in DocumentPreparation.query.filter(DocumentPreparation.id.in_([d['document_id'] for d in selected])).all()}
    return {'version': 2, 'owner_id': owner_id, 'draft_id': str(resolved_draft_id), 'excluded_ids': sorted(exclusions),
            'files': [
        {'file_id': d['document_id'], 'generation': rows[d['document_id']].generation,
         'source_hash': rows[d['document_id']].source_hash, 'scope': d['scope'],
         'artifact_id': d['artifact_id'], 'preparation_version': VERSION,
         'current': d['scope'] == 'chat' and (d['document_id'] in current_ids or not d.get('committed'))} for d in selected]}


def mark_snapshot_committed(snapshot):
    ids = [entry['file_id'] for entry in snapshot['files'] if entry.get('current')]
    DocumentPreparation.query.filter(DocumentPreparation.id.in_(ids),
        DocumentPreparation.first_attached_at.is_(None)).update({'first_attached_at': now()}, synchronize_session=False)
    db.session.commit()


def validate_snapshot(owner_id, draft_id, snapshot):
    if (not isinstance(snapshot, dict) or snapshot.get('version') != 2
            or snapshot.get('owner_id') != owner_id or str(snapshot.get('draft_id')) != str(ChatThread.resolve_id(draft_id))):
        raise PreparationError('stale_scope', 'Refresh the document selection before sending.')
    view = {d['document_id']: d for d in catalog(owner_id, draft_id)['documents']}
    entries = snapshot.get('files')
    if not isinstance(entries, list) or any(not isinstance(f, dict) or not isinstance(f.get('file_id'), str) for f in entries):
        raise PreparationError('stale_scope', 'Invalid document snapshot.')
    ids = [f['file_id'] for f in entries]
    rows = {d.id: d for d in DocumentPreparation.query.filter(DocumentPreparation.id.in_(ids)).all()}
    for entry in snapshot.get('files', []):
        doc = rows.get(entry['file_id'])
        if (not doc or entry['file_id'] not in view or doc.status != 'ready' or not doc.uploaded_file_id
                or doc.generation != entry.get('generation') or doc.source_hash != entry.get('source_hash')
                or entry.get('scope') != view[doc.id]['scope']):
            raise PreparationError('stale_scope', 'A document changed or was removed. Refresh the document selection and try again.')
        if doc.artifact_id != entry.get('artifact_id') or entry.get('preparation_version') != VERSION:
            raise PreparationError('stale_scope', 'Document preparation changed. Refresh before sending.')
    return view


def lock_document(document_id, *, commit_reads=True):
    # End authorization reads, then acquire a write lock before reading mutable
    # state. This also fences retries/cancellation on SQLite, not only MySQL.
    if commit_reads:
        db.session.commit()
    count = DocumentPreparation.query.filter_by(id=document_id).update({DocumentPreparation.id: DocumentPreparation.id})
    if not count:
        raise PreparationError('not_found', 'Document no longer exists.', 404)
    return DocumentPreparation.query.filter_by(id=document_id).populate_existing().one()


def retry_document(doc, expected_generation=None):
    db.session.commit()
    group = db.session.get(UploadGroup, doc.group_id)
    if group is None:
        raise PreparationError('not_found', 'Document scope no longer exists.', 404)
    _lock_scope(group.project_id)
    doc = lock_document(doc.id, commit_reads=False)
    if expected_generation is not None:
        if not isinstance(expected_generation, int) or isinstance(expected_generation, bool):
            raise PreparationError('invalid_request', 'Invalid document generation.', 400)
        if doc.generation != expected_generation:
            db.session.commit()
            return document_view(doc)
    # A lost HTTP response can replay Retry after its first attempt queued work.
    # Reuse that attempt instead of restarting it or reporting a false failure.
    if doc.status in ACTIVE:
        db.session.commit()
        return document_view(doc)
    extraction = normalized_extraction(json.loads(doc.coverage_json).get('extraction')) if doc.coverage_json else {}
    retry_extraction = doc.status == 'ready' and bool(extraction.get('gap_fingerprint'))
    artifact = db.session.get(DocumentPreparedArtifact, doc.artifact_id) if doc.artifact_id else None
    outdated = doc.status == 'ready' and (artifact is None or artifact.preparation_version != VERSION)
    if outdated:
        # Start a new generation from the retained original; keep immutable old artifacts.
        DocumentSummarySegment.query.filter_by(document_id=doc.id).delete(synchronize_session=False)
        doc.parsed_at = doc.indexed_at = doc.summarized_at = None
        doc.coverage_json = None
    if doc.status != 'failed' and not retry_extraction and not outdated:
        raise PreparationError('invalid_transition', 'Only failed documents or extraction gaps can be retried.')
    if doc.status == 'failed':
        # A manual retry grants a fresh bounded reduction attempt sequence while
        # retaining every successfully completed parse/summary unit.
        DocumentSummarySegment.query.filter_by(document_id=doc.id, kind='budget_retry').delete(synchronize_session=False)
    if retry_extraction:
        for segment in DocumentSummarySegment.query.filter_by(document_id=doc.id, kind='parse').all():
            if json.loads(segment.result_json).get('gaps'):
                db.session.delete(segment)
        doc.parsed_at = doc.indexed_at = doc.ready_at = None
        doc.artifact_id = doc.parse_manifest_json = doc.coverage_json = doc.coverage_summary_json = None
        if doc.uploaded_file_id:
            file = db.session.get(UploadedFiles, doc.uploaded_file_id)
            file.metadata_json = json.dumps({'schema_version': 2, 'status': 'pending', 'preview': ''})
    doc.generation += 1
    doc.attempts = 0
    doc.status = 'queued'
    source_invalid = doc.error_code in {'source_missing', 'source_corrupt'}
    doc.error_code = doc.error_message = doc.lease_token = doc.lease_until = None
    if not doc.parsed_at and (source_invalid or not doc.source_key or not Path(doc.source_key).exists()):
        doc.source_key = doc.source_hash = None
    doc.dispatch_pending = bool(doc.source_key or doc.parsed_at)
    doc.updated_at = now()
    db.session.commit()
    dispatch_document(doc.id)
    return document_view(doc)


def cancel_document(doc):
    doc = lock_document(doc.id)
    doc.generation += 1
    doc.status, doc.dispatch_pending, doc.updated_at = 'cancelled', False, now()
    doc.lease_token = doc.lease_until = None
    # Canonical deletion is part of this transaction, after fencing the worker.
    if doc.uploaded_file_id:
        file = db.session.get(UploadedFiles, doc.uploaded_file_id)
        doc.uploaded_file_id = None
        if file:
            db.session.delete(file)
    db.session.commit()
    return document_view(doc)


def reconcile():
    instant = now()
    timeout = timedelta(seconds=int(current_app.config.get('DOCUMENT_DISPATCH_RETRY_SECONDS', 60)))
    expiry = timedelta(seconds=int(current_app.config.get('DOCUMENT_UPLOAD_EXPIRY_SECONDS', 86400)))
    retention = timedelta(seconds=int(current_app.config.get('DOCUMENT_SOURCE_RETENTION_SECONDS', 7 * 86400)))
    docs = DocumentPreparation.query.options(defer(DocumentPreparation.parse_manifest_json), defer(DocumentPreparation.coverage_json)).filter(or_(DocumentPreparation.status.in_(ACTIVE),
                                              DocumentPreparation.source_key.isnot(None))).all()
    dispatch, cleanup = set(), []
    for doc in docs:
        query = DocumentPreparation.query.filter_by(id=doc.id, generation=doc.generation)
        if doc.status == 'queued' and not doc.source_key and not doc.parsed_at and doc.created_at < instant - expiry:
            query.filter_by(status='queued', source_key=None, parsed_at=None).update({
                'status': 'cancelled', 'error_code': 'transfer_expired',
                'error_message': 'Upload expired before source bytes arrived. Select the file again.',
                'generation': doc.generation + 1, 'dispatch_pending': False,
                'updated_at': instant}, synchronize_session=False)
        elif doc.status in ACTIVE and doc.lease_until and doc.lease_until < instant:
            retryable = doc.attempts < 4
            changed = query.filter(DocumentPreparation.status.in_(ACTIVE),
                DocumentPreparation.lease_until < instant).update({
                    'generation': doc.generation + 1, 'lease_token': None, 'lease_until': None,
                    'status': 'queued' if retryable else 'failed', 'dispatch_pending': retryable,
                    'error_code': 'worker_timeout', 'updated_at': instant,
                    'error_message': 'Document preparation worker stopped. Retry if it does not recover.'},
                    synchronize_session=False)
            if changed and retryable:
                dispatch.add(doc.id)
        elif doc.status in ACTIVE and (doc.source_key or doc.parsed_at) and not doc.lease_token:
            if not doc.dispatched_at or doc.dispatched_at < instant - timeout or doc.dispatch_pending:
                changed = query.filter_by(status=doc.status, lease_token=None).update(
                    {'dispatch_pending': True}, synchronize_session=False)
                if changed:
                    dispatch.add(doc.id)
        retained_spreadsheet = (doc.status != 'cancelled' and doc.uploaded_file_id is not None
                                and doc.filename.rsplit('.', 1)[-1].lower() in {'xlsx', 'csv'})
        if doc.source_key and doc.status in TERMINAL and not retained_spreadsheet and doc.updated_at < instant - retention:
            # Claim cleanup before touching the filesystem; a simultaneous retry
            # changes generation/status and prevents deletion of its source.
            changed = query.filter_by(status=doc.status, source_key=doc.source_key).filter(
                DocumentPreparation.updated_at < instant - retention).update({'source_key': None}, synchronize_session=False)
            if changed:
                cleanup.append(doc.source_key)
    db.session.commit()
    root = source_root()
    for source_key in cleanup:
        path = Path(source_key)
        if root in path.resolve().parents:
            path.unlink(missing_ok=True)
    for document_id in dispatch:
        dispatch_document(document_id)
    # Remove unpublished files left behind if a web process died during staging.
    referenced = {p[0] for p in db.session.query(DocumentPreparation.source_key).filter(DocumentPreparation.source_key.isnot(None))}
    db.session.commit()
    for path in root.iterdir():
        if path.is_file() and str(path) not in referenced and path.stat().st_mtime < (instant - expiry).timestamp():
            path.unlink(missing_ok=True)
    return len(dispatch)
