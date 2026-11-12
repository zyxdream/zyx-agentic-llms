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
                      CELERY={'task_default_queue': 'lexintel-e2e-documents'})
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
        db.session.add(Drafts(id=1, public_id='draft-1', projectId=1, docCategory=69, display_name='Chat'))
        db.session.add(Drafts(id=2, public_id='draft-2', projectId=1, docCategory=69, display_name='Sibling'))
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
    response = client.post('/api/everything/upload-groups', json={
        'scope': 'chat', 'scopeId': 'draft-1', 'selectionId': 'selection', 'documents': manifest('A', 'B')})
    assert response.status_code == 201
    group = response.json
    response = client.post(f'/api/everything/upload-groups/{group["group_id"]}/files', data={
        'clientUploadIds': json.dumps(['A', 'B']),
        'files': [(io.BytesIO(b'text'), 'A.txt'), (io.BytesIO(b'text'), 'B.txt')]})
    assert response.status_code == 413
    assert UploadedFiles.query.count() == 0
    response = client.get('/api/everything/document-preparation?draftId=draft-1')
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


def test_backfill_reuses_canonical_text_but_rebuilds_incomplete_chunks(app):
    from backend.document_preparation_cli import backfill_documents
    file = UploadedFiles(unique_id='legacy', original_filename='legacy.txt', file_size=4,
                         content='A contract about the middle event.', draft_id=1)
    db.session.add(file)
    db.session.commit()
    assert prep.catalog(1, 'draft-1')['documents'][0]['error_code'] == 'backfill_required'
    assert backfill_documents() == ['legacy']
    assert backfill_documents() == []
    assert tasks.prepare_document.run('legacy', 1)['ok']
    assert UploadedFiles.query.count() == 1
    assert DocumentChunk.query.count() == 1


@pytest.mark.parametrize('scope', ['chat', 'matter'])
def test_existing_file_retry_registers_once_and_backfill_reuses_work(app, scope):
    from backend.document_preparation_cli import backfill_documents
    file = UploadedFiles(unique_id='old-file', original_filename='old.txt', file_size=4,
        content='A contract about the middle event.', draft_id=1 if scope == 'chat' else None,
        project_id=1 if scope == 'matter' else None)
    db.session.add(file)
    db.session.commit()
    client = app.test_client()
    assert client.get('/api/everything/document-preparation?draftId=draft-1').status_code == 200
    assert DocumentPreparation.query.count() == 0  # Polling never starts work.
    first = client.post('/api/everything/documents/old-file/retry')
    assert first.status_code == 202
    assert first.json['document_id'] == 'old-file'
    doc = db.session.get(DocumentPreparation, 'old-file')
    generation, group_id = doc.generation, doc.group_id
    assert doc.parsed_at and json.loads(doc.parse_manifest_json)['extraction']['gaps']
    assert client.post('/api/everything/documents/old-file/retry').status_code == 202
    assert backfill_documents() == []
    assert DocumentPreparation.query.count() == UploadGroup.query.count() == 1
    assert (doc.generation, doc.group_id) == (generation, group_id)
    assert run(first.json)['ok']


@pytest.mark.parametrize('prepared', [False, True])
def test_existing_file_remove_cleans_chunks_and_chat_cache_without_deleting_history(app, prepared):
    file = UploadedFiles(unique_id='old-file', original_filename='old.txt', file_size=4,
        content='A contract about the middle event.', draft_id=1)
    db.session.add(file)
    db.session.flush()
    db.session.add(DocumentChunk(uploaded_file_id=file.id, draft_id=1, chunk_index=0,
        content='old text', token_count=2, char_count=8, source_filename='old.txt', source_unique_id=file.unique_id))
    db.session.add(TextEntry(draftId=1, textName='inputs/old-file', content='cached text'))
    db.session.add(TextEntry(draftId=1, textName='state', content=json.dumps({
        'uploaded_files': {'old-file': {'page_count': 2}, 'other-file': {'page_count': 3}},
        'total_page_count': 5, 'chat_history': [['question', 'answer']]})))
    db.session.commit()
    if prepared:
        from backend.util.document_backfill import register_existing_document
        register_existing_document(file.id, 1)
    result = app.test_client().delete('/api/everything/documents/old-file')
    assert result.status_code == 200
    assert result.json['status'] == 'cancelled'
    assert UploadedFiles.query.count() == DocumentChunk.query.count() == 0
    assert TextEntry.query.filter_by(textName='inputs/old-file').first() is None
    from backend.util.workspace_store import load
    state = load(1)
    assert state['uploaded_files'] == {'other-file': {'page_count': 3}}
    assert state['total_page_count'] == 3
    assert state['chat_history'] == [['question', 'answer']]
    assert prep.catalog(1, 'draft-1')['documents'] == []


@pytest.mark.parametrize('kind', ['other-user', 'search', 'other-pipeline', 'missing'])
@pytest.mark.parametrize('action', ['retry', 'remove'])
def test_existing_document_actions_reject_inaccessible_or_unrelated_uploads(app, kind, action):
    if kind == 'other-pipeline':
        db.session.add(Drafts(id=3, projectId=1, docCategory=1, display_name='Other pipeline'))
    if kind != 'missing':
        db.session.add(UploadedFiles(unique_id='forbidden', original_filename='old.txt', file_size=4,
            content='private', project_id=2 if kind == 'other-user' else 1,
            draft_id=3 if kind == 'other-pipeline' else None, source='search' if kind == 'search' else None))
    db.session.commit()
    client = app.test_client()
    result = client.post('/api/everything/documents/forbidden/retry') if action == 'retry' else client.delete('/api/everything/documents/forbidden')
    assert result.status_code == 404
    assert DocumentPreparation.query.count() == UploadGroup.query.count() == 0
    assert UploadedFiles.query.count() == (0 if kind == 'missing' else 1)


def test_existing_ready_file_with_outdated_artifact_queues_current_preparation(app):
    from backend.models import DocumentPreparedArtifact
    entry = receive(admit('A'))
    assert run(entry)['ok']
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    artifact = db.session.get(DocumentPreparedArtifact, doc.artifact_id)
    artifact.preparation_version = 'previous-preparation-version'
    generation = doc.generation
    db.session.commit()
    result = app.test_client().post(f'/api/everything/documents/{doc.id}/retry')
    assert result.status_code == 202
    assert result.json['status'] == 'queued'
    assert result.json['generation'] == generation + 1
    assert result.json['artifact_id'] is None


def test_existing_file_without_readable_content_requires_reupload(app):
    db.session.add(UploadedFiles(unique_id='empty-file', original_filename='old.txt', file_size=4,
                                content='', project_id=1))
    db.session.commit()
    response = app.test_client().post('/api/everything/documents/empty-file/retry')
    assert response.status_code == 202
    assert response.json['status'] == 'failed'
    assert response.json['needs_reupload']
    assert response.json['error_code'] == 'source_missing'


def test_concurrent_existing_file_registration_reuses_one_document_and_dispatch(app, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from backend.util.document_backfill import register_existing_document
    file = UploadedFiles(unique_id='old-file', original_filename='old.txt', file_size=4,
                         content='Stored readable text.', project_id=1)
    db.session.add(file)
    db.session.commit()
    file_id = file.id
    db.session.commit()
    barrier = Barrier(2)
    def register(_):
        with app.app_context():
            barrier.wait(timeout=10)
            doc, changed = register_existing_document(file_id, 1)
            return doc.id, changed
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, range(2)))
    assert sorted(changed for _, changed in results) == [False, True]
    assert {document_id for document_id, _ in results} == {'old-file'}
    assert DocumentPreparation.query.count() == UploadGroup.query.count() == 1


def test_backfill_retry_replay_after_completion_does_not_repeat_preparation(app):
    db.session.add(UploadedFiles(unique_id='old-file', original_filename='old.txt', file_size=4,
                                content='Stored readable text.', project_id=1))
    db.session.commit()
    client = app.test_client()
    initial = client.post('/api/everything/documents/old-file/retry', json={'backfill': True})
    assert run(initial.json)['ok']
    doc = db.session.get(DocumentPreparation, 'old-file')
    generation, artifact_id = doc.generation, doc.artifact_id
    repeated = client.post('/api/everything/documents/old-file/retry', json={'backfill': True})
    assert repeated.status_code == 202
    assert repeated.json['status'] == 'ready'
    assert (repeated.json['generation'], repeated.json['artifact_id']) == (generation, artifact_id)
    # A stale retry of an older attempt also cannot reset successful work.
    stale = client.post('/api/everything/documents/old-file/retry', json={'generation': generation - 1})
    assert stale.json['artifact_id'] == artifact_id


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
def test_active_upload_workers_cannot_overbook_final_slot_or_byte_capacity(app, tmp_path, monkeypatch, quota, expected_error):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from backend import upload_tasks
    barrier = Barrier(2)
    sizes = [4] * 99 if quota == 'count' else [50 * 1024**2] * 20 + [24 * 1024**2 - 4]
    db.session.add_all([UploadedFiles(unique_id=f'existing-{i}', original_filename=f'existing-{i}.txt',
        file_size=size, content='', draft_id=1) for i, size in enumerate(sizes)])
    db.session.commit()
    def parse(_path):
        barrier.wait(timeout=10)
        return 'Document contents.', '', False
    monkeypatch.setattr(upload_tasks, 'get_complaint_text_from_file', parse)
    monkeypatch.setattr(upload_tasks, 'get_last_ocr_warning', lambda: None)
    monkeypatch.setattr(upload_tasks, 'DBMemory', lambda **kw: {})
    monkeypatch.setattr(upload_tasks, 'load_state_from_memory', lambda memory: {})
    monkeypatch.setattr(upload_tasks, 'save_state_to_memory', lambda *args: None)
    monkeypatch.setattr(upload_tasks.document_metadata_task, 'delay', lambda *args: None)
    paths = [tmp_path / f'worker-{i}.txt' for i in range(2)]
    for path in paths:
        path.write_bytes(b'text')
    db.session.remove()
    def process(path):
        with app.app_context():
            return upload_tasks.case_assistant_upload_file_task.run({
                'draft_id': 'draft-1', 'temp_path': str(path), 'filename': path.name,
                'unique_id': f'accepted-{path.stem}',
                'client_upload_id': path.name})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(process, paths))
    assert sum(result['ok'] for result in results) == 1
    assert [result['reason'] for result in results if not result['ok']] == [expected_error]
    assert UploadedFiles.query.count() == len(sizes) + 1
    assert db.session.query(db.func.sum(UploadedFiles.file_size)).scalar() <= 1024**3
    completed = next(result for result in results if result['ok'])
    assert completed['unique_id'].startswith('accepted-worker-')
    assert UploadedFiles.query.filter_by(unique_id=completed['unique_id']).one()
    assert completed['metadata']['status'] == 'pending'


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


@pytest.mark.parametrize('partial', [False, True])
def test_direct_prompt_api_rejects_preparing_files_before_enqueue(app, monkeypatch, partial):
    from backend import views
    from backend import agent_tasks
    from backend.models import CaseAssistantWorkflow
    queued = []
    monkeypatch.setattr(views, 'current_user', SimpleNamespace(id=1))
    monkeypatch.setattr(agent_tasks.advance, 'apply_async', lambda **kw: queued.append(kw) or SimpleNamespace(id='turn'))
    monkeypatch.setattr(views, 'increment_task_analytics', lambda *a, **kw: None)
    monkeypatch.setattr(views, '_cancel_redis', SimpleNamespace(set=lambda *a, **kw: None))
    if partial:
        monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw: manifest_from_blocks(
            [{'text': 'Readable contract text.'}],
            gaps=[{'code': 'ocr_unreadable', 'page': 2, 'message': 'Unreadable page'}]))
    entry = receive(admit('A'))
    payload = {'draftId': 'draft-1', 'query': 'Review the attached contract.',
               'chatFiles': [{'name': entry['document_id']}]}
    with app.test_request_context(json=payload):
        body, status = views.everything_api.__wrapped__()
    assert status == 409 and body['code'] == 'documents_not_ready' and not queued
    assert db.session.get(DocumentPreparation, entry['document_id']).first_attached_at is None
    run(entry)
    payload['document_snapshot'] = {'files': ['forged-client-snapshot']}
    with app.test_request_context(json=payload):
        _, status = views.everything_api.__wrapped__()
    assert status == 200
    accepted = db.session.get(CaseAssistantWorkflow, queued[0]['args'][0])
    assert json.loads(accepted.data_json)['document_snapshot']['files'][0]['file_id'] == entry['document_id']
    assert 'accepted_coverage_gaps' not in json.loads(accepted.data_json)['document_snapshot']
    assert db.session.get(DocumentPreparation, entry['document_id']).first_attached_at is not None


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


def test_migration_matches_models_and_can_be_reversed(app):
    import importlib
    from sqlalchemy import inspect
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    migration = importlib.import_module('migrations.versions.d5e6f7a8b9c0_document_preparation')
    latest = importlib.import_module('migrations.versions.e6f7a8b9c0d1_prepared_document_artifacts')
    names = ['document_prepared_artifacts', 'document_summary_segments', 'document_preparations', 'document_upload_groups']
    with db.engine.begin() as connection:
        for name in names:
            db.metadata.tables[name].drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            latest.upgrade()
        inspector = inspect(connection)
        for name in names:
            expected = set(db.metadata.tables[name].columns.keys())
            # These historical migrations precede the chat-identity rename.
            if name == 'document_upload_groups':
                expected = (expected - {'chat_thread_id'}) | {'draft_id'}
            assert {c['name'] for c in inspector.get_columns(name)} == expected
        with Operations.context(MigrationContext.configure(connection)):
            latest.downgrade()
            migration.downgrade()
        assert not set(names) & set(inspect(connection).get_table_names())


def test_transfer_failure_is_durable_and_retry_keeps_document_id(app):
    group = admit('A')
    client = app.test_client()
    response = client.post(f'/api/everything/upload-groups/{group["group_id"]}/files', data={
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


def test_confirmation_revalidates_snapshot_and_keeps_cancellation_available(app, monkeypatch):
    from backend import everything, views
    from backend.util.db import DBMemory
    monkeypatch.setattr(views, 'current_user', SimpleNamespace(id=1))
    queued = []
    monkeypatch.setattr(views.everything_confirmed_workflow_task, 'apply_async', lambda **kw: queued.append(kw))
    entry = receive(admit('A'))
    run(entry)
    snapshot = prep.build_snapshot(1, 'draft-1', {'chatFiles': [{'name': entry['document_id']}]})
    state = everything.create_empty_state()
    state['active_workflow'] = {'workflow_run_id': 'workflow', 'status': 'awaiting_user_confirmation',
        'pending_confirmation': {'workflow_run_id': 'workflow', 'original_turn_input': {'document_snapshot': snapshot}}}
    memory = DBMemory(db, 1)
    from backend.util.workspace_store import initialize, patch_auxiliary
    initialize(1, state=state)
    patch_auxiliary(1, {'active_workflow': state['active_workflow']})
    db.session.commit()
    prep.cancel_document(db.session.get(DocumentPreparation, entry['document_id']))
    with app.test_request_context(json={'draftId': 'draft-1', 'workflow_run_id': 'workflow', 'decision': {}}):
        body, status = views.everything_confirm_workflow_api.__wrapped__()
    assert status == 409 and body['code'] == 'stale_scope' and not queued
    with app.test_request_context(json={'draftId': 'draft-1', 'workflow_run_id': 'workflow', 'decision': {'cancelled': True}}):
        body, status = views.everything_confirm_workflow_api.__wrapped__()
    assert status == 200 and body['cancelled']


def test_prompt_scope_metadata_survives_normalization(app):
    from backend import everything
    scope = {'version': 1, 'files': [], 'excluded_ids': ['failed-document']}
    meta = everything._normalize_prompt_turn_meta({'prompt_attachments': [], 'document_scope': scope})
    assert meta['document_scope'] == scope


def test_new_matter_documents_do_not_expand_phase_one_snapshot(app, monkeypatch):
    from backend import everything
    first = receive(admit('A', scope='matter'))
    run(first)
    snapshot = prep.build_snapshot(1, 'draft-1', {})
    second = receive(admit('B', scope='matter', key='later-selection'))
    run(second)
    captured = []
    def classify(*args, **kwargs):
        captured.extend(args[4])
        return {'draft_action': 'answer_only', 'revision_mode': 'general_revision', 'selected_files': [],
                'context_policy': 'none', 'needs_confirmation': False,
                'file_selection': {'selected_files': [], 'primary_draft_file_id': None}}
    monkeypatch.setattr(everything, 'classify_intent_decision', classify)
    from backend.util import prepared_retrieval
    monkeypatch.setattr(prepared_retrieval, 'refine_selection', lambda snapshot, decision, *a, **kw: decision)
    monkeypatch.setattr(everything, 'acquire_draft_lock', lambda *a: True)
    monkeypatch.setattr(everything, 'release_draft_lock', lambda *a: None)
    monkeypatch.setattr(everything, 'analytics_record', lambda *a: None)
    task = SimpleNamespace(request=SimpleNamespace(id='turn'), update_state=lambda **kw: None)
    result = everything._everything_phase1_body(task, {'draftId': 'draft-1', 'user_id': 1,
        'query': 'Discuss the matter.', 'assistant_mode': 'fast', 'document_snapshot': snapshot})
    assert not result.get('error')
    assert [d['unique_id'] for d in captured] == [first['document_id']]


def test_deleting_draft_cascades_preparation_on_sqlite(app):
    entry = receive(admit('A'))
    run(entry)
    db.session.delete(db.session.get(Drafts, 1))
    db.session.commit()
    assert DocumentPreparation.query.count() == 0
    assert DocumentSummarySegment.query.count() == 0
    assert UploadGroup.query.count() == 0


def test_e2e_queue_cannot_be_overridden_with_a_shared_queue(app):
    app.config.update(E2E_CELERY_QUEUE='lexintel-e2e-documents', DOCUMENT_PREPARATION_QUEUE='celery.documents')
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
