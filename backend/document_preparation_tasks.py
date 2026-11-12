"""Isolated, checkpointed ingestion. A killed worker can be safely redelivered."""
import json
import hashlib
from datetime import timedelta
from pathlib import Path
import uuid

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import defer, load_only

from backend.database import db
from backend.models import DocumentChunk, DocumentPreparation, DocumentPreparedArtifact, DocumentSummarySegment, FolderUpload, UploadedFiles, UploadGroup
from backend.util.account_access import AccountUnavailable, require_enabled_account
from backend.util.attachment_parser import (parse_attachment, search_chunks, unverified_manifest, WorkPending,
                                           fingerprint, normalized_extraction, extraction_warning)
from backend.util.upload_processing import _estimate_page_count
from backend.util.document_metadata import serialize_metadata, default_document_metadata
from backend.util.document_preparation import ACTIVE, enabled, now, reconcile, _lock_scope, VERSION, dispatch_document, compact_coverage
from backend.util.preparation_summary import summarize_document

LEASE_SECONDS = 1500


class StalePreparation(Exception):
    pass


class SourceCorrupt(ValueError):
    pass


def _claimed(document_id, generation, token):
    instant = now()
    updated = DocumentPreparation.query.filter(
        DocumentPreparation.id == document_id, DocumentPreparation.generation == generation,
        DocumentPreparation.lease_token == token, DocumentPreparation.status.in_(ACTIVE),
        DocumentPreparation.lease_until >= instant).update({
            'lease_until': instant + timedelta(seconds=LEASE_SECONDS), 'updated_at': instant}, synchronize_session=False)
    if not updated:
        db.session.rollback()
        raise StalePreparation()
    doc = DocumentPreparation.query.options(defer(DocumentPreparation.parse_manifest_json),
        defer(DocumentPreparation.coverage_json)).filter_by(id=document_id).populate_existing().one()
    # Recheck the upload owner before provider work and each saved checkpoint.
    owner_id = db.session.query(UploadGroup.owner_id).filter_by(id=doc.group_id).scalar()
    require_enabled_account(owner_id)
    return doc


def _failed(document_id, generation, token, stage, exc):
    """Release a failed attempt without discarding previously completed work."""
    db.session.rollback()
    reason = ('account_unavailable' if isinstance(exc, AccountUnavailable)
              else 'source_missing' if isinstance(exc, FileNotFoundError)
              else 'source_corrupt' if isinstance(exc, SourceCorrupt) else f'{stage}_failed')
    DocumentPreparation.query.filter_by(id=document_id, generation=generation, lease_token=token).update({
        'status': 'failed', 'lease_token': None, 'lease_until': None, 'dispatch_pending': False,
        'updated_at': now(), 'error_code': reason,
        'error_message': str(exc) if isinstance(exc, AccountUnavailable)
                         else f'Document {stage} failed. Retry this document.'}, synchronize_session=False)
    db.session.commit()
    from flask import current_app
    current_app.logger.error('Document %s failed at %s', document_id, stage,
                             exc_info=(type(exc), exc, exc.__traceback__))
    return {'ok': False, 'reason': 'account_unavailable' if isinstance(exc, AccountUnavailable) else f'{stage}_failed'}


@shared_task(bind=True, ignore_result=False, acks_late=True, reject_on_worker_lost=True,
             soft_time_limit=1200, time_limit=1260)
def prepare_document(self, document_id, generation):
    token, instant = str(uuid.uuid4()), now()
    claimed = DocumentPreparation.query.filter(
        DocumentPreparation.id == document_id, DocumentPreparation.generation == generation,
        DocumentPreparation.status.in_(ACTIVE),
        (DocumentPreparation.lease_token.is_(None)) | (DocumentPreparation.lease_until < instant),
    ).update({'lease_token': token, 'lease_until': instant + timedelta(seconds=LEASE_SECONDS),
              'attempts': DocumentPreparation.attempts + 1, 'dispatch_pending': False,
              'updated_at': instant}, synchronize_session=False)
    db.session.commit()
    if not claimed:
        return {'ok': False, 'reason': 'already_claimed_or_stale'}
    stage = 'parsing'
    try:
        ready_cache = {row.segment_key: json.loads(row.result_json) for row in DocumentSummarySegment.query.options(
            load_only(DocumentSummarySegment.segment_key, DocumentSummarySegment.result_json)).filter_by(
                document_id=document_id, state='ready').filter(DocumentSummarySegment.kind.in_(
                    ['parse', 'orientation', 'section', 'reduction', 'document', 'split', 'budget_retry'])).all()}
        db.session.commit()
        def checkpoint(key, result=None):
            if result is None:
                return ready_cache.get(key)
            doc = _claimed(document_id, generation, token)
            saved = DocumentSummarySegment.query.filter_by(document_id=document_id, segment_key=key).first()
            if result is not None:
                if saved is None:
                    saved = DocumentSummarySegment(document_id=document_id, segment_key=key, result_json='{}')
                    db.session.add(saved)
                saved.generation = generation
                if '_work' in result:
                    saved.kind, saved.state = result['_work'], 'running'
                    saved.lease_token, saved.lease_until = token, doc.lease_until
                    saved.attempts = (saved.attempts or 0) + 1
                else:
                    saved.kind, saved.state = result.get('_kind', 'section'), 'ready'
                    saved.result_json = json.dumps(result, ensure_ascii=False)
                    ready_cache[key] = result
                    saved.search_text = json.dumps(result, ensure_ascii=False) if saved.kind == 'section' else None
                    saved.lease_token = saved.lease_until = None
            previous = json.loads(saved.result_json) if saved and saved.state == 'ready' else None
            db.session.commit()  # Never keep a transaction open during parsing or provider work.
            return previous

        doc = _claimed(document_id, generation, token)
        project_id = db.session.get(UploadGroup, doc.group_id).project_id
        if not doc.parsed_at:
            doc.status = stage
            path, filename, size, group_id, expected_hash = doc.source_key, doc.filename, doc.file_size, doc.group_id, doc.source_hash
            db.session.commit()
            if not path or not Path(path).is_file():
                raise FileNotFoundError('Source bytes expired or are not accessible to this worker')
            digest = hashlib.sha256()
            with open(path, 'rb') as original:
                for block in iter(lambda: original.read(1024 * 1024), b''):
                    digest.update(block)
            if digest.hexdigest() != expected_hash:
                raise SourceCorrupt('Source fingerprint changed after upload')
            def parse_checkpoint(key, result=None):
                return checkpoint(hashlib.sha256((expected_hash + key).encode()).hexdigest(), result)
            manifest = parse_attachment(path, filename, parse_checkpoint, max_units=8)
            text = manifest['text']
            if not text or not text.strip():
                raise ValueError('No readable text was found in this document')
            pages = 0 if manifest.get('spreadsheet') else _estimate_page_count(filename, path, text)
            _lock_scope(project_id)
            doc = _claimed(document_id, generation, token)
            group = db.session.get(UploadGroup, group_id)
            file = db.session.get(UploadedFiles, doc.uploaded_file_id) if doc.uploaded_file_id else None
            if file is None:
                file = UploadedFiles(unique_id=document_id, original_filename=filename, file_size=size,
                    chat_thread_id=group.chat_thread_id, project_id=group.project_id if not group.chat_thread_id else None,
                    folder_upload_id=doc.folder_upload_id, relative_path=doc.relative_path, source='workspace')
            file.content, file.page_count = text, pages
            file.metadata_json = serialize_metadata(default_document_metadata('pending'))
            db.session.add(file)
            db.session.flush()
            doc.uploaded_file_id, doc.parsed_at, doc.checkpoint = file.id, now(), 'parsed'
            doc.parse_manifest_json = json.dumps({k: v for k, v in manifest.items() if k != 'text'}, ensure_ascii=False)
            doc.warning = extraction_warning(manifest['extraction'])
            db.session.commit()
        else:
            db.session.commit()

        stage = 'indexing'
        doc = _claimed(document_id, generation, token)
        if not doc.indexed_at:
            doc.status = stage
            file_id = doc.uploaded_file_id
            file = db.session.get(UploadedFiles, file_id)
            text = file.content
            db.session.commit()
            chunks = search_chunks(text)
            if not chunks:
                raise ValueError('No searchable content was found in this document')
            _lock_scope(project_id)
            doc = _claimed(document_id, generation, token)
            file = db.session.get(UploadedFiles, file_id)
            DocumentChunk.query.filter_by(uploaded_file_id=file_id).delete(synchronize_session=False)
            for chunk in chunks:
                db.session.add(DocumentChunk(uploaded_file_id=file_id, chat_thread_id=file.chat_thread_id,
                    project_id=file.project_id, source_filename=file.original_filename,
                    source_relative_path=file.relative_path, source_unique_id=document_id, **chunk))
            doc.indexed_at, doc.checkpoint = now(), 'indexed'
            if doc.folder_upload_id:
                folder = db.session.get(FolderUpload, doc.folder_upload_id)
                if folder:
                    folder.file_count = UploadedFiles.query.filter_by(folder_upload_id=folder.id).count()
                    folder.total_chunks = DocumentChunk.query.join(UploadedFiles).filter(
                        UploadedFiles.folder_upload_id == folder.id).count()
            db.session.commit()
        else:
            db.session.commit()

        stage = 'summarizing'
        doc = _claimed(document_id, generation, token)
        doc.status = stage
        file = db.session.get(UploadedFiles, doc.uploaded_file_id)
        text, filename, source_hash = file.content, file.original_filename, doc.source_hash
        manifest = json.loads(doc.parse_manifest_json) if doc.parse_manifest_json else unverified_manifest(text)
        manifest['extraction'] = normalized_extraction(manifest['extraction'])
        manifest['text'] = text
        db.session.commit()
        metadata, coverage = summarize_document(text, filename, checkpoint, source_hash=source_hash,
                                                manifest=manifest, max_calls=1)
        doc = _claimed(document_id, generation, token)
        file = db.session.get(UploadedFiles, doc.uploaded_file_id)
        artifact_id = fingerprint([document_id, source_hash, VERSION, metadata,
                                   {k: v for k, v in manifest.items() if k != 'text'}])
        metadata['artifact_id'] = artifact_id
        coverage['generated_at'] = now().isoformat()
        card = {k: metadata[k] for k in ('schema_version', 'status', 'headline', 'document_kind', 'preview', 'summary',
            'key_points', 'parties', 'dates', 'topics', 'preparation_version', 'summary_format_revision',
            'summary_basis', 'summary_exhaustive', 'spreadsheet', 'artifact_id') if k in metadata}
        if db.session.get(DocumentPreparedArtifact, artifact_id) is None:
            db.session.add(DocumentPreparedArtifact(id=artifact_id, document_id=document_id,
                preparation_version=VERSION, source_hash=source_hash, created_at=now(),
                card_json=json.dumps(card, ensure_ascii=False),
                result_json=json.dumps({k: v for k, v in metadata.items() if k != 'orientation'}, ensure_ascii=False),
                manifest_json=json.dumps({**{k: v for k, v in manifest.items() if k != 'text'},
                                          'orientation': metadata.get('orientation')}, ensure_ascii=False),
                coverage_json=json.dumps(coverage)))
        # Keep the compatibility column small; rich immutable records live outside chat state.
        file.metadata_json = serialize_metadata(card)
        doc.artifact_id, doc.coverage_json = artifact_id, json.dumps(coverage)
        doc.coverage_summary_json = json.dumps(compact_coverage(coverage))
        doc.parse_manifest_json = json.dumps({k: v for k, v in manifest.items() if k != 'text'}, ensure_ascii=False)
        doc.warning = extraction_warning(manifest['extraction'])
        doc.status, doc.checkpoint, doc.ready_at = 'ready', 'ready', now()
        doc.lease_token = doc.lease_until = None
        doc.error_code = doc.error_message = None
        db.session.commit()
        # Preserve completion fields used by clients of the legacy upload routes.
        chunk_count = DocumentChunk.query.filter_by(uploaded_file_id=file.id).count()
        group = db.session.get(UploadGroup, doc.group_id)
        from backend.util.document_preparation import _canonical_filter
        from sqlalchemy import func
        total_pages = db.session.query(func.coalesce(func.sum(UploadedFiles.page_count), 0)).filter(
            *_canonical_filter(group.project_id, group.chat_thread_id)).scalar()
        item = {'unique_id': document_id, 'filename': file.original_filename,
                'relative_path': file.relative_path, 'page_count': file.page_count, 'chunk_count': chunk_count}
        return {'ok': True, 'document_id': document_id, 'unique_id': document_id,
                'clientUploadId': doc.client_upload_id, 'filename': file.original_filename,
                'file_size': file.file_size, 'page_count': file.page_count, 'chunk_count': chunk_count,
                'total_page_count': int(total_pages), 'ocr_warning': doc.warning,
                'folder_id': doc.folder_upload_id, 'files': [item], 'file_count': 1,
                'batch_file_count': 1, 'total_chunks': chunk_count, 'batch_total_chunks': chunk_count,
                'failed_files': [], 'skipped_files': [], 'timed_out': False}
    except StalePreparation:
        return {'ok': False, 'reason': 'stale_attempt'}
    except WorkPending:
        try:
            doc = _claimed(document_id, generation, token)
        except AccountUnavailable as exc:
            return _failed(document_id, generation, token, stage, exc)
        doc.lease_token = doc.lease_until = None
        doc.dispatch_pending, doc.attempts, doc.updated_at = True, 0, now()
        db.session.commit()
        dispatch_document(document_id)
        return {'ok': True, 'document_id': document_id, 'continuing': True}
    except (Exception, SoftTimeLimitExceeded) as exc:
        return _failed(document_id, generation, token, stage, exc)


@shared_task(ignore_result=True)
def reconcile_document_preparations():
    if enabled():
        reconcile()
