"""Authorize immutable document revisions and save private deterministic exports."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from flask import current_app

from backend.database import db
from backend.models import AgentArtifact
from backend.util import case_assistant_workflow as workflows
from backend.util.document_drafts import is_markdown_draft
from backend.util.document_preparation import validate_snapshot


# Bump when rendering changes; older files remain saved but are not reused.
EXPORT_RENDERER_VERSION = 1


class DocumentDownloadError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _source_hash(draft):
    return hashlib.sha256(json.dumps(draft, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _root():
    root = Path(current_app.config.get('DOCUMENT_EXPORT_STORAGE_DIR',
        os.environ.get('DOCUMENT_EXPORT_STORAGE_DIR', Path(current_app.instance_path) / 'document_exports'))).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _saved_path(root, metadata):
    key = metadata.get('storage_key')
    if not isinstance(key, str):
        return None
    candidate = root / key
    resolved = candidate.resolve()
    if candidate.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file():
        return None
    return resolved if _hash(resolved) == metadata.get('sha256') else None


def download_revision(*, user_id, draft_id, revision_id, format, preset='general', current_draft=None):
    """Return a file for the requested saved revision, even after later publication.

    The source artifact and its workflow provide identity and authorization.
    Export records share that workflow; they never update its steps or draft.
    """
    from backend.util.document_exports import export_document
    if format not in {'markdown', 'html', 'docx', 'pdf'}:
        raise DocumentDownloadError('Choose Markdown, HTML, DOCX, or PDF.')
    if preset not in {'general', 'court'}:
        raise DocumentDownloadError('Choose the general or court layout.')
    if not isinstance(revision_id, str) or not revision_id:
        raise DocumentDownloadError('A saved document revision is required.', 409)
    artifact = db.session.get(AgentArtifact, revision_id)
    if artifact is None or artifact.kind != 'draft':
        raise DocumentDownloadError('Document revision not found.', 404)
    try:
        workflow = workflows.owned(artifact.workflow_run_id, user_id)
    except workflows.WorkflowNotFound:
        raise DocumentDownloadError('Document revision not found.', 404) from None
    if workflow.draft_id != draft_id:
        raise DocumentDownloadError('Document revision not found.', 404)
    validate_snapshot(user_id, draft_id, json.loads(workflow.data_json)['document_snapshot'])
    source = json.loads(artifact.content_json).get('draft')
    if not is_markdown_draft(source) or source.get('revision_id') != revision_id:
        raise DocumentDownloadError('The saved document revision is invalid.', 409)
    source_hash = _source_hash(source)
    if (is_markdown_draft(current_draft) and current_draft.get('revision_id') == revision_id
            and _source_hash(current_draft) != source_hash):
        raise DocumentDownloadError('The current document differs from its saved revision. Refresh the document.', 409)
    preset = preset if format in {'docx', 'pdf'} else 'general'
    # End the authorization transaction before filesystem or Docker work.
    workflow_id, step_id = artifact.workflow_run_id, artifact.step_id
    saved = AgentArtifact.query.filter_by(workflow_run_id=workflow_id, kind='document_export').all()
    db.session.commit()
    root = _root()
    for record in saved:
        value = json.loads(record.content_json)
        if all(value.get(key) == expected for key, expected in {
                'source_revision_id': revision_id, 'source_sha256': source_hash,
                'format': format, 'preset': preset,
                'renderer_version': EXPORT_RENDERER_VERSION}.items()):
            path = _saved_path(root, value)
            if path:
                return path, value, record.id
    export_id = str(uuid.uuid4())
    directory = root / workflow_id / export_id
    directory.mkdir(parents=True, mode=0o700)
    extension = 'md' if format == 'markdown' else format
    filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(source.get('title') or 'Document')).strip(' .')[:100] or 'Document'
    path = directory / (filename + '.' + extension)
    try:
        try:
            metadata = export_document(source, format, path, preset=preset)
        except ValueError as exc:
            raise DocumentDownloadError(str(exc)) from exc
        value = {**metadata, 'kind': 'document_export', 'source_revision_id': revision_id,
            'source_sha256': source_hash, 'preset': preset, 'renderer_version': EXPORT_RENDERER_VERSION,
            'storage_key': str(path.relative_to(root)), 'sha256': _hash(path),
            'text': f'{format.upper()} export of {source.get("title") or "document"} ({preset} layout).'}
        db.session.add(AgentArtifact(id=export_id, workflow_run_id=workflow_id, step_id=step_id,
            kind='document_export', content_json=json.dumps(value, ensure_ascii=False), created_at=workflows.now()))
        db.session.commit()
    except Exception:
        db.session.rollback()
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return path, value, export_id
