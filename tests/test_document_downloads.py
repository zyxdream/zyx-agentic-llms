"""Pinned revisions, private export records, authorization and export recovery."""
import copy
import json
import uuid
from types import SimpleNamespace

import pytest

from backend import views
from backend.database import db
from backend.models import AgentArtifact, AgentStep, Drafts
from backend.util import case_assistant_workflow as workflow
from backend.util import document_downloads as downloads, document_exports as exports
from backend.util import workspace_store
from tests.test_agent_controller import agent
from tests.test_document_preparation import app


def revision(row, markdown, parent=None):
    step = AgentStep.query.filter_by(workflow_run_id=row.id).first()
    if step is None:
        step = AgentStep(id=str(uuid.uuid4()), workflow_run_id=row.id, sequence=1, kind='tool',
            name='create_draft', arguments_json='{}', state='ready', created_at=workflow.now(), updated_at=workflow.now())
        db.session.add(step)
    uid = str(uuid.uuid4())
    source = {'format_version': 2, 'source_format': 'markdown', 'title': 'Analysis', 'markdown': markdown,
        'revision_id': uid, 'parent_revision_id': parent, 'export_settings': {'court': {}}}
    db.session.add(AgentArtifact(id=uid, workflow_run_id=row.id, step_id=step.id, kind='draft',
        content_json=json.dumps({'kind': 'draft', 'draft': source}), created_at=workflow.now()))
    db.session.commit()
    return source


def download(row, source, format='markdown', **kwargs):
    return downloads.download_revision(user_id=1, draft_id=row.draft_id,
        revision_id=source['revision_id'], format=format, **kwargs)


def test_exact_revision_survives_new_publication_and_reuses_private_export(agent, app, tmp_path, monkeypatch):
    app.config['DOCUMENT_EXPORT_STORAGE_DIR'] = str(tmp_path / 'private')
    first = revision(agent, '# Analysis\n\n| ID | Amount |\n|---|---:|\n| 000101 | 12.50 |\n')
    second = revision(agent, first['markdown'].replace('12.50', '20.00'), first['revision_id'])
    state = workspace_store.load(agent.draft_id)
    workspace_store.update_draft(agent.draft_id, second, expected_version=state['draft_version'])
    db.session.commit()
    before = {a.id: a.content_json for a in AgentArtifact.query.filter_by(kind='draft')}
    monkeypatch.setattr('backend.ai.base.chat_completion_parse', lambda *a, **kw: pytest.fail('Export called an LLM'))
    old_path, old_metadata, old_export = download(agent, first, current_draft=second)
    assert old_path.read_bytes() == first['markdown'].encode('utf-8')
    assert old_path.is_relative_to(tmp_path / 'private')
    assert old_metadata['source_revision_id'] == first['revision_id']
    assert download(agent, first, current_draft=second)[2] == old_export
    new_path, _, _ = download(agent, second, current_draft=second)
    assert new_path.read_text() == second['markdown']
    assert {a.id: a.content_json for a in AgentArtifact.query.filter_by(kind='draft')} == before
    assert workspace_store.load(agent.draft_id)['draft'] == second
    assert AgentArtifact.query.filter_by(kind='document_export').count() == 2


def test_revision_authorization_and_snapshot_integrity(agent, app, tmp_path):
    app.config['DOCUMENT_EXPORT_STORAGE_DIR'] = str(tmp_path / 'private')
    source = revision(agent, 'Saved content')
    for user_id, draft_id in [(2, 1), (1, 2)]:
        with pytest.raises(downloads.DocumentDownloadError) as error:
            downloads.download_revision(user_id=user_id, draft_id=draft_id,
                revision_id=source['revision_id'], format='markdown')
        assert error.value.status == 404
    changed = copy.deepcopy(source); changed['markdown'] = 'Tampered current state'
    with pytest.raises(downloads.DocumentDownloadError, match='differs'):
        download(agent, source, current_draft=changed)
    assert AgentArtifact.query.filter_by(kind='document_export').count() == 0


@pytest.mark.parametrize('old_version', [None, 0])
def test_updated_renderer_replaces_cached_download_without_rewriting_saved_work(agent, app, tmp_path, monkeypatch, old_version):
    app.config['DOCUMENT_EXPORT_STORAGE_DIR'] = str(tmp_path / 'private')
    source = revision(agent, '# Saved memo\n\n[Case](https://example.com/opinion)')
    with monkeypatch.context() as previous:
        previous.setattr(downloads, 'EXPORT_RENDERER_VERSION', old_version)
        old_path, _, old_id = download(agent, source)
    old_record = db.session.get(AgentArtifact, old_id).content_json
    new_path, metadata, new_id = download(agent, source)
    assert new_id != old_id
    assert metadata['renderer_version'] == downloads.EXPORT_RENDERER_VERSION
    assert new_path.read_bytes() == old_path.read_bytes() == source['markdown'].encode('utf-8')
    assert db.session.get(AgentArtifact, old_id).content_json == old_record
    assert download(agent, source)[2] == new_id


def test_failed_pdf_keeps_source_and_can_retry(agent, app, tmp_path, monkeypatch):
    app.config['DOCUMENT_EXPORT_STORAGE_DIR'] = str(tmp_path / 'private')
    source = revision(agent, '# Result\n\nA saved result.')
    artifact = db.session.get(AgentArtifact, source['revision_id'])
    before = artifact.content_json
    def unavailable(*args, **kwargs):
        raise exports.DocumentExportError('PDF conversion is unavailable.')
    monkeypatch.setattr(exports, 'convert_docx_to_pdf', unavailable)
    with pytest.raises(exports.DocumentExportError, match='unavailable'):
        download(agent, source, 'pdf')
    assert db.session.get(AgentArtifact, source['revision_id']).content_json == before
    assert AgentArtifact.query.filter_by(kind='document_export').count() == 0
    def converter(source_path, target):
        target.write_bytes(b'%PDF-test-conversion')
    monkeypatch.setattr(exports, 'convert_docx_to_pdf', converter)
    path, metadata, _ = download(agent, source, 'pdf')
    assert path.read_bytes().startswith(b'%PDF-')
    assert metadata['format'] == 'pdf'


def route_setup(agent, monkeypatch, source):
    state = workspace_store.load(agent.draft_id)
    workspace_store.update_draft(agent.draft_id, source, expected_version=state['draft_version'])
    db.session.commit()
    def owned(*args, **kwargs):
        return None
    owned._last_draft = db.session.get(Drafts, 1)
    monkeypatch.setattr(views, 'ownership_check', owned)
    monkeypatch.setattr(views, 'current_user', SimpleNamespace(id=1))
    monkeypatch.setattr(views, 'analytics_record', lambda *a, **kw: None)
    monkeypatch.setattr(views, 'increment_task_analytics', lambda *a, **kw: None)
    monkeypatch.setattr(views, 'draft_full_everything_word', lambda *a, **kw: pytest.fail('Used legacy generator'))


def test_endpoint_defaults_to_word_and_pins_requested_revision(agent, app, tmp_path, monkeypatch):
    app.config['DOCUMENT_EXPORT_STORAGE_DIR'] = str(tmp_path / 'private')
    source = revision(agent, '# Result\n\n| A | B |\n|---|---|\n| 1 | 2 |\n')
    route_setup(agent, monkeypatch, source)
    with app.test_request_context(json={}):
        response = views.download_word_everything.__wrapped__('draft-1')
        assert response.status_code == 200
        assert response.headers['X-Document-Revision'] == source['revision_id']
        assert response.mimetype == exports.CONTENT_TYPES['docx']
        response.close()
    with app.test_request_context(json={'format': 'markdown', 'revision_id': source['revision_id']}):
        response = views.download_word_everything.__wrapped__('draft-1')
        response.direct_passthrough = False
        assert response.get_data() == source['markdown'].encode('utf-8')
        response.close()
    with app.test_request_context(json={'format': 'executable'}):
        _, status = views.download_word_everything.__wrapped__('draft-1')
        assert status == 400


def test_endpoint_reports_pdf_failure_without_changing_revision(agent, app, tmp_path, monkeypatch):
    app.config['DOCUMENT_EXPORT_STORAGE_DIR'] = str(tmp_path / 'private')
    source = revision(agent, 'Saved content')
    route_setup(agent, monkeypatch, source)
    def fail(*a, **kw):
        raise exports.DocumentExportError('PDF conversion is unavailable.')
    monkeypatch.setattr(exports, 'convert_docx_to_pdf', fail)
    with app.test_request_context(json={'format': 'pdf'}):
        body, status = views.download_word_everything.__wrapped__('draft-1')
        assert status == 503 and 'unavailable' in body['msg']
    assert workspace_store.load(agent.draft_id)['draft'] == source


def test_missing_court_settings_is_a_client_error_and_leaves_source_available(agent, app, tmp_path, monkeypatch):
    app.config['DOCUMENT_EXPORT_STORAGE_DIR'] = str(tmp_path / 'private')
    source = revision(agent, 'Saved content')
    route_setup(agent, monkeypatch, source)
    with app.test_request_context(json={'format': 'docx', 'preset': 'court'}):
        body, status = views.download_word_everything.__wrapped__('draft-1')
        assert status == 400 and 'Court formatting is unavailable' in body['msg']
    assert download(agent, source)[0].read_text() == 'Saved content'
