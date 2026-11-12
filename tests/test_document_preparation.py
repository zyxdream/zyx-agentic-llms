"""Durable ingestion tests use real SQLite transactions and mocked provider I/O."""
import io
import json
from datetime import timedelta
from types import SimpleNamespace

from flask import Flask
import pytest
from werkzeug.datastructures import FileStorage

from backend.database import db
from backend.models import (DocumentChunk, DocumentPreparation, DocumentSummarySegment,
                            Drafts, Project, TextEntry, UploadedFiles, UploadGroup, User)
from backend.util import document_preparation as prep
from backend import document_preparation_tasks as tasks
from backend.util import preparation_summary as summaries
from backend import document_preparation_routes as routes
from backend.util.attachment_parser import manifest_from_blocks


def prepared_provider(prompt, schema=summaries.PreparedSummary, **kwargs):
    if schema is summaries.Orientation:
        return {'title': 'Contract', 'headings': [], 'definitions': []}
    if schema is summaries.SectionRecord:
        return {'heading': 'Contract', 'summary': 'A contract concerning the middle event.', 'facts': [],
                'definitions': [], 'parties': [], 'dates': [], 'topics': ['contract'], 'uncertainties': []}
    return {'headline': 'Contract concerning the middle event', 'document_kind': 'contract',
            'preview': 'A contract concerning the middle event.',
            'detailed_digest': 'The contract concerns the middle event. It identifies the parties. It records obligations. It contains exceptions.',
            'key_points': [], 'parties': [], 'dates': [], 'topics': ['contract']}


@pytest.fixture
def app(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI=f'sqlite:///{tmp_path / "documents.db"}',
                      DOCUMENT_PREPARATION_ENABLED=True, DOCUMENT_PREPARATION_SOURCE_DIR=str(tmp_path / 'sources'),
                      CELERY={'task_default_queue': 'zyx-agentic-llms.e2e-documents'})
    db.init_app(app)
    routes.register(app)
    for endpoint, fn in list(app.view_functions.items()):
        if endpoint.startswith('document_preparation.'):
            app.view_functions[endpoint] = fn.__wrapped__
    monkeypatch.setattr(routes, 'current_user', SimpleNamespace(id=1))
    monkeypatch.setattr(tasks.prepare_document, 'apply_async', lambda **kw: None)
    monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw: manifest_from_blocks([{'text': 'A contract about the middle event.'}]))
    monkeypatch.setattr(summaries, '_call', prepared_provider)
    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email='ingestion@example.test'))
        db.session.add(User(id=2, email='other@example.test'))
        db.session.add(Project(id=1, name='Matter', userId=1))
        db.session.add(Project(id=2, name='Other', userId=2))
        db.session.add(Drafts(id=1, public_id='draft-1', projectId=1,  display_name='Chat'))
        db.session.add(Drafts(id=2, public_id='draft-2', projectId=1,  display_name='Sibling'))
        db.session.commit()
        yield app
        db.session.remove()


def manifest(*ids, size=4):
    return [{'client_upload_id': str(i), 'filename': f'{i}.txt', 'file_size': size} for i in ids]


def admit(*ids, scope='chat', key='selection', size=4):
    return prep.admit_group(1, scope, 'draft-1' if scope == 'chat' else 1, key, manifest(*ids, size=size))


def receive(group, index=0, data=b'text'):
    entry = group['documents'][index]
    return prep.stage_transfer(prep.owned_group(group['group_id'], 1), entry['clientUploadId'],
                               FileStorage(stream=io.BytesIO(data), filename=entry['filename']))


def run(entry):
    for _ in range(100):
        result = tasks.prepare_document.run(entry['document_id'], entry['generation'])
        if not result.get('continuing'):
            return result
    pytest.fail('Preparation did not converge')


def test_manifest_replay_quota_pending_and_identity_conflict(app):
    app.config['DOCUMENT_CHAT_FILE_LIMIT'] = 2
    first = admit('A', 'B')
    assert admit('A', 'B')['documents'] == first['documents']
    assert DocumentPreparation.query.count() == 2
    with pytest.raises(prep.PreparationError, match='Maximum 2'):
        admit('C', key='second')
    db.session.rollback()
    with pytest.raises(prep.PreparationError, match='different documents'):
        admit('A', key='selection', size=3)
    db.session.rollback()
    prep.cancel_document(db.session.get(DocumentPreparation, first['documents'][0]['document_id']))
    assert admit('C', key='second')['documents'][0]['stage'] == 'awaiting_upload'


def test_transfer_replay_hash_conflict_and_complete_lifecycle_without_chat_writes(app, monkeypatch):
    monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw: manifest_from_blocks([{'text': 'A contract about the middle event.'}], gaps=[{'code': 'ocr_unreadable', 'page': 2, 'message': 'Unreadable page'}]))
    group = admit('A')
    entry = receive(group)
    assert receive(group)['document_id'] == entry['document_id']
    with pytest.raises(prep.PreparationError, match='different file content'):
        receive(group, data=b'evil')
    db.session.rollback()
    assert run(entry)['ok']
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    assert doc.status == 'ready'
    assert 'could not be read' in doc.warning
    assert json.loads(doc.coverage_json)['completed_segments'] == 1
    assert json.loads(doc.coverage_json)['covered_tokens'] > 0
    assert UploadedFiles.query.count() == 1
    assert DocumentChunk.query.count() == 1
    assert TextEntry.query.count() == 0
    assert run(entry)['reason'] == 'already_claimed_or_stale'


def test_failed_summary_reuses_parse_index_and_segment_checkpoints(app, monkeypatch):
    monkeypatch.setattr(summaries, 'SOURCE_SEGMENT_TOKENS', 4)
    calls = []
    def fail_second(prompt, schema, **kwargs):
        calls.append(prompt)
        if len(calls) == 2:
            raise ValueError('simulated provider outage')
        return prepared_provider(prompt, schema)
    monkeypatch.setattr(summaries, '_call', fail_second)
    entry = receive(admit('A'))
    assert not run(entry)['ok']
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    assert doc.status == 'failed'
    assert doc.indexed_at and doc.parsed_at
    assert DocumentSummarySegment.query.filter_by(state='ready').count() == 1
    monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw: pytest.fail('parse checkpoint was repeated'))
    retried = prep.retry_document(doc)
    assert run(entry)['reason'] == 'already_claimed_or_stale'
    assert run(retried)['ok']
    assert calls.count(calls[0]) == 1
    assert DocumentChunk.query.count() == 1
    assert UploadedFiles.query.count() == 1


def test_cancel_during_summary_fences_result_and_releases_quota(app, monkeypatch):
    entry = receive(admit('A'))
    def cancel(prompt, schema, **kwargs):
        prep.cancel_document(db.session.get(DocumentPreparation, entry['document_id']))
        return prepared_provider(prompt, schema)
    monkeypatch.setattr(summaries, '_call', cancel)
    assert run(entry)['reason'] == 'stale_attempt'
    assert db.session.get(DocumentPreparation, entry['document_id']).status == 'cancelled'
    assert UploadedFiles.query.count() == 0
    assert DocumentChunk.query.count() == 0


def test_provider_does_not_hold_database_transaction(app, monkeypatch):
    def provider(prompt, schema, **kwargs):
        assert not db.session().in_transaction()
        return prepared_provider(prompt, schema)
    monkeypatch.setattr(summaries, '_call', provider)
    assert run(receive(admit('A')))['ok']


def test_catalog_readiness_scope_and_frozen_snapshot(app):
    chat = receive(admit('A'))
    background = receive(admit('B', scope='matter', key='background'))
    data = {'chatFiles': [{'name': chat['document_id']}],
            'promptAttachments': [{'file_id': chat['document_id']}]}
    with pytest.raises(prep.PreparationError) as error:
        prep.build_snapshot(1, 'draft-1', data)
    assert error.value.payload['code'] == 'documents_not_ready'
    run(chat)
    # Excluding a blocked background document is explicit and submission-local.
    data['excludedDocumentIds'] = [background['document_id']]
    snapshot = prep.build_snapshot(1, 'draft-1', data)
    assert [f['file_id'] for f in snapshot['files']] == [chat['document_id']]
    assert snapshot['files'][0]['current']
    sibling = prep.catalog(1, 'draft-2')['documents']
    assert {d['document_id'] for d in sibling} == {background['document_id']}
    run(background)
    # Documents ready after admission do not silently expand the frozen snapshot.
    assert len(prep.validate_snapshot(1, 'draft-1', snapshot)) == 2
    assert len(snapshot['files']) == 1
    prep.cancel_document(db.session.get(DocumentPreparation, chat['document_id']))
    with pytest.raises(prep.PreparationError) as error:
        prep.validate_snapshot(1, 'draft-1', snapshot)
    assert error.value.payload['code'] == 'stale_scope'


def test_owner_and_sibling_scope_cannot_be_forged(app):
    group = admit('A')
    with pytest.raises(prep.PreparationError):
        prep.owned_group(group['group_id'], 2)
    with pytest.raises(prep.PreparationError):
        prep.build_snapshot(1, 'draft-2', {'chatFiles': [{'name': group['documents'][0]['document_id']}]})


def test_dispatch_failure_is_recovered_and_expired_lease_is_fenced(app, monkeypatch):
    monkeypatch.setattr(tasks.prepare_document, 'apply_async', lambda **kw: (_ for _ in ()).throw(ConnectionError('offline broker')))
    entry = receive(admit('A'))
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    assert doc.dispatch_pending
    dispatched = []
    monkeypatch.setattr(tasks.prepare_document, 'apply_async', lambda **kw: dispatched.append(kw))
    prep.reconcile()
    assert len(dispatched) == 1
    doc.status, doc.lease_token, doc.lease_until = 'indexing', 'dead-worker', prep.now() - timedelta(seconds=1)
    doc.attempts = 1
    db.session.commit()
    prep.reconcile()
    assert doc.generation == 2
    assert run(entry)['reason'] == 'already_claimed_or_stale'
    assert len(dispatched) == 2


def test_expired_untransferred_selection_releases_capacity(app):
    group = admit('A')
    doc = db.session.get(DocumentPreparation, group['documents'][0]['document_id'])
    doc.created_at = prep.now() - timedelta(days=2)
    db.session.commit()
    prep.reconcile()
    assert doc.status == 'cancelled'
    assert not prep.catalog(1, 'draft-1')['documents']


def test_http_groups_enforce_count_bytes_and_aggregate_status(app):
    client = app.test_client()
    app.config.update(DOCUMENT_UPLOAD_GROUP_FILES=2, DOCUMENT_UPLOAD_GROUP_BYTES=7)
    response = client.post('/api/workspace/upload-groups', json={
        'scope': 'chat', 'scopeId': 'draft-1', 'selectionId': 'selection', 'documents': manifest('A', 'B')})
    assert response.status_code == 201
    group = response.json
    response = client.post(f'/api/workspace/upload-groups/{group["group_id"]}/files', data={
        'clientUploadIds': json.dumps(['A', 'B']),
        'files': [(io.BytesIO(b'text'), 'A.txt'), (io.BytesIO(b'text'), 'B.txt')]})
    assert response.status_code == 413
    assert UploadedFiles.query.count() == 0
    response = client.get('/api/workspace/document-preparation?conversation_id=draft-1')
    assert response.json['counts'] == {'queued': 2}
    assert len(response.json['documents']) == 2
    assert all('source_key' not in d for d in response.json['documents'])


def test_luna_route_budget_and_full_middle_coverage(app, monkeypatch):
    monkeypatch.setattr(summaries, 'SOURCE_SEGMENT_TOKENS', 10)
    calls = []
    def provider(prompt, schema, **kwargs):
        calls.append((prompt, kwargs))
        assert summaries.input_tokens(prompt, schema) <= (summaries.REDUCTION_INPUT_LIMIT if schema is summaries.PreparedSummary else summaries.INPUT_LIMIT)
        return schema(**prepared_provider(prompt, schema))
    monkeypatch.setattr(summaries, '_call', _real_summary_call)
    monkeypatch.setattr(summaries, 'chat_completion_parse', provider)
    saved = {}
    def checkpoint(key, value=None):
        if value is not None and '_work' not in value:
            saved[key] = value
        return saved.get(key)
    text = 'beginning ' * 12 + 'MIDDLE_ONLY_FACT ' + 'ending ' * 12
    _, coverage = summaries.summarize_document(text, 'memo.txt', checkpoint, source_hash='hash')
    assert any('MIDDLE_ONLY_FACT' in prompt for prompt, _ in calls)
    assert coverage['covered_tokens'] == coverage['source_tokens']
    assert coverage['completed_segments'] == coverage['total_segments'] > 1
    assert all(k['model'] == 'gpt-6-luna' and k['reasoning_effort'] == 'medium' and
               k['max_output_tokens'] == 32768 for _, k in calls)


_real_summary_call = summaries._call








@pytest.mark.parametrize('kind', ['other-user', 'search', 'other-pipeline', 'missing'])
@pytest.mark.parametrize('action', ['retry', 'remove'])
def test_existing_document_actions_reject_inaccessible_or_unrelated_uploads(app, kind, action):
    if kind == 'other-pipeline':
        db.session.add(Drafts(id=3, projectId=1,  display_name='Other pipeline'))
    if kind != 'missing':
        db.session.add(UploadedFiles(unique_id='forbidden', original_filename='old.txt', file_size=4,
            content='private', project_id=2 if kind == 'other-user' else 1,
            draft_id=3 if kind == 'other-pipeline' else None, source='search' if kind == 'search' else None))
    db.session.commit()
    client = app.test_client()
    result = client.post('/api/workspace/sources/forbidden/retry') if action == 'retry' else client.delete('/api/workspace/sources/forbidden')
    assert result.status_code == 404
    assert DocumentPreparation.query.count() == UploadGroup.query.count() == 0
    assert UploadedFiles.query.count() == (0 if kind == 'missing' else 1)










def test_default_100_document_manifest_status_and_101st_rejection(app):
    group = admit(*range(100))
    assert len(group['documents']) == 100
    assert prep.catalog(1, 'draft-1')['counts'] == {'queued': 100}
    assert prep.policy()['chat_files'] == 100
    assert prep.policy()['matter_files'] == 30
    with pytest.raises(prep.PreparationError) as error:
        admit('overflow', key='overflow')
    assert error.value.payload['code'] == 'file_count_limit'
    assert error.value.payload['remaining'] == 0
    db.session.rollback()
    with pytest.raises(prep.PreparationError) as error:
        admit(*range(101), key='oversized-manifest')
    assert error.value.payload['code'] == 'file_count_limit'
    db.session.rollback()
    assert DocumentPreparation.query.count() == 100




@pytest.mark.parametrize('quota,expected_error', [('count', 'file_count_limit'), ('bytes', 'total_size_limit')])
def test_concurrent_admission_cannot_overbook_sqlite(app, monkeypatch, quota, expected_error):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    if quota == 'count':
        app.config['DOCUMENT_CHAT_FILE_LIMIT'] = 1
    else:
        app.config['DOCUMENT_TOTAL_BYTES'] = 4
    barrier = Barrier(2)
    lock = prep._lock_scope
    def synchronized(project_id):
        barrier.wait(timeout=10)
        lock(project_id)
    monkeypatch.setattr(prep, '_lock_scope', synchronized)
    db.session.remove()
    def submit(key):
        with app.app_context():
            try:
                return admit(key, key=key)['group_id']
            except prep.PreparationError as error:
                db.session.rollback()
                return error.payload['code']
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(submit, ['one', 'two']))
    assert result.count(expected_error) == 1
    assert DocumentPreparation.query.count() == 1


@pytest.mark.parametrize('scope,file_count,file_bytes', [
    ('chat', 20, 50 * 1024**2), ('matter', 10, 100 * 1024**2),
])
def test_default_total_size_boundary_keeps_matter_count_limit(app, scope, file_count, file_bytes):
    assert prep.policy()['chat_files'] == 100
    assert prep.policy()['matter_files'] == 30
    assert prep.policy()['total_bytes'] == 1024**3
    first = admit(*range(file_count), scope=scope, size=file_bytes)
    admit('boundary', scope=scope, key='boundary', size=24 * 1024**2)
    # The whole manifest is accepted or rejected before any bytes are transferred.
    with pytest.raises(prep.PreparationError) as error:
        admit('overflow', scope=scope, key='overflow', size=1)
    assert error.value.payload['code'] == 'total_size_limit'
    assert error.value.payload['remaining_bytes'] == 0
    db.session.rollback()
    assert UploadGroup.query.count() == 2
    assert DocumentPreparation.query.count() == file_count + 1
    assert admit(*range(file_count), scope=scope, size=file_bytes)['group_id'] == first['group_id']


def test_total_size_counts_failed_pending_and_canonical_once_and_releases_on_remove(app):
    app.config['DOCUMENT_TOTAL_BYTES'] = 12
    entry = receive(admit('A'))
    assert run(entry)['ok']
    group = admit('B', key='pending')
    pending = db.session.get(DocumentPreparation, group['documents'][0]['document_id'])
    pending.status = 'failed'
    db.session.commit()
    # A ready canonical row and its preparation reserve four bytes only once.
    admit('C', key='at-limit')
    with pytest.raises(prep.PreparationError) as error:
        admit('D', key='overflow')
    assert error.value.payload['used_bytes'] == 12
    db.session.rollback()
    prep.cancel_document(pending)
    admit('D', key='replacement')


def test_total_size_includes_legacy_files_and_keeps_chat_matter_search_scopes_separate(app):
    app.config['DOCUMENT_TOTAL_BYTES'] = 8
    db.session.add_all([
        UploadedFiles(unique_id='legacy', original_filename='legacy.txt', file_size=4, content='', draft_id=1),
        UploadedFiles(unique_id='search', original_filename='search.txt', file_size=100, content='',
                      project_id=1, source='search'),
    ])
    db.session.commit()
    admit('A')
    with pytest.raises(prep.PreparationError) as error:
        admit('B', key='chat-overflow')
    assert error.value.payload['code'] == 'total_size_limit'
    db.session.rollback()
    # Each chat and the shared background corpus have separate total quotas.
    prep.admit_group(1, 'chat', 'draft-2', 'sibling', manifest('sibling', size=8))
    admit('background', scope='matter', key='background', size=8)




def test_100_documents_prepare_and_admit_one_frozen_prompt(app):
    group = admit(*range(100))
    ids = []
    for index in range(100):
        entry = receive(group, index)
        assert run(entry)['ok']
        ids.append(entry['document_id'])
    assert prep.catalog(1, 'draft-1')['counts'] == {'ready': 100}
    snapshot = prep.build_snapshot(1, 'draft-1', {'chatFiles': [{'name': i} for i in ids]})
    assert len(snapshot['files']) == 100
    assert len(prep.validate_snapshot(1, 'draft-1', snapshot)) == 100
    assert UploadedFiles.query.count() == DocumentChunk.query.count() == 100
    assert TextEntry.query.count() == 0


def test_long_unicode_summary_preserves_exact_text_coverage(app, monkeypatch):
    monkeypatch.setattr(summaries, 'SOURCE_SEGMENT_TOKENS', 20)
    source = '漢字節合約😺 clause ' * 12
    seen = []
    def collect(prompt, schema, **kwargs):
        if schema is summaries.SectionRecord:
            payload = json.loads(prompt.rsplit('\n', 1)[-1])
            seen.append(payload['range'])
        return prepared_provider(prompt, schema)
    monkeypatch.setattr(summaries, '_call', collect)
    summaries.summarize_document(source, 'agreement.txt', lambda *a: None, source_hash='hash')
    assert ''.join(source[r['core_start']:r['core_end']] for r in seen) == source




def test_transfer_failure_is_durable_and_retry_keeps_document_id(app):
    group = admit('A')
    client = app.test_client()
    response = client.post(f'/api/workspace/upload-groups/{group["group_id"]}/files', data={
        'clientUploadIds': json.dumps(['A']), 'files': (io.BytesIO(b'wrong-size'), 'A.txt')})
    assert response.status_code == 202
    assert response.json['failed'][0]['code'] == 'size_mismatch'
    doc = db.session.get(DocumentPreparation, group['documents'][0]['document_id'])
    assert doc.status == 'failed'
    assert prep.catalog(1, 'draft-1')['documents'][0]['error_code'] == 'size_mismatch'
    retry = prep.retry_document(doc)
    assert retry['document_id'] == doc.id and retry['needs_reupload']
    entry = receive(group)
    assert run(entry)['ok']




def test_prompt_scope_metadata_survives_normalization(app):
    from backend.workspace import state as everything
    scope = {'version': 1, 'files': [], 'excluded_ids': ['failed-document']}
    meta = everything._normalize_prompt_turn_meta({'prompt_attachments': [], 'document_scope': scope})
    assert meta['document_scope'] == scope




def test_deleting_draft_cascades_preparation_on_sqlite(app):
    entry = receive(admit('A'))
    run(entry)
    db.session.delete(db.session.get(Drafts, 1))
    db.session.commit()
    assert DocumentPreparation.query.count() == 0
    assert DocumentSummarySegment.query.count() == 0
    assert UploadGroup.query.count() == 0


def test_e2e_queue_cannot_be_overridden_with_a_shared_queue(app):
    app.config.update(E2E_CELERY_QUEUE='zyx-agentic-llms.e2e-documents', DOCUMENT_PREPARATION_QUEUE='celery.documents')
    with pytest.raises(ValueError, match='isolated E2E namespace'):
        prep.queue_name()


def test_status_queries_do_not_load_document_bodies(app):
    from sqlalchemy import event
    entry = receive(admit('A'))
    run(entry)
    db.session.remove()
    statements = []
    def capture(connection, cursor, statement, *args):
        statements.append(statement)
    event.listen(db.engine, 'before_cursor_execute', capture)
    try:
        assert prep.catalog(1, 'draft-1')['counts'] == {'ready': 1}
    finally:
        event.remove(db.engine, 'before_cursor_execute', capture)
    assert all('uploaded_files.content' not in statement.partition('FROM')[0] for statement in statements)


@pytest.mark.parametrize('missing', [True, False])
def test_missing_or_corrupted_source_can_be_retransferred_without_new_identity(app, missing):
    from pathlib import Path
    group = admit('A')
    entry = receive(group)
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    path = Path(doc.source_key)
    path.unlink() if missing else path.write_bytes(b'evil')
    assert not run(entry)['ok']
    assert doc.error_code == ('source_missing' if missing else 'source_corrupt')
    assert prep.document_view(doc)['needs_reupload']
    retry = prep.retry_document(doc)
    assert retry['document_id'] == entry['document_id'] and retry['needs_reupload']
    assert run(receive(group))['ok']
    assert UploadedFiles.query.count() == 1


def test_outdated_prepared_artifact_can_be_reprepared_without_legacy_backfill(app):
    from backend.models import DocumentPreparedArtifact
    entry=receive(admit('versioned'))
    assert run(entry)['ok']
    doc=db.session.get(DocumentPreparation,entry['document_id'])
    artifact=db.session.get(DocumentPreparedArtifact,doc.artifact_id)
    prior_artifact='obsolete-test-artifact'
    artifact.id=prior_artifact
    artifact.preparation_version='obsolete-test-version'
    doc.artifact_id=prior_artifact
    db.session.commit()
    generation=doc.generation
    response=app.test_client().post(f'/api/workspace/sources/{doc.id}/retry',json={'generation':generation})
    assert response.status_code==202
    assert response.json['generation']==generation+1
    assert run(response.json)['ok']
    db.session.refresh(doc)
    assert doc.status=='ready' and doc.artifact_id!=prior_artifact
    assert db.session.get(DocumentPreparedArtifact,prior_artifact) is not None
