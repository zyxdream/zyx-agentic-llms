"""Legacy upload/metadata workers recheck their owner's local account status."""
import pytest

from backend import document_metadata_tasks as metadata_tasks
from backend import upload_tasks
from backend.database import db
from backend.models import UploadedFiles, User
from backend.util import upload_processing
from backend.util import workspace_store as workspace
from backend.util.account_access import AccountUnavailable
from tests.test_document_preparation import app


def disable_owner():
    db.session.query(User).filter_by(id=1).update({'is_disabled': True}, synchronize_session=False)
    db.session.commit()


@pytest.mark.parametrize('kind', ['chat', 'search', 'background', 'batch'])
def test_disabled_queued_upload_denied_and_temp_removed(app, tmp_path, monkeypatch, kind):
    path = tmp_path / 'input.txt'
    path.write_text('original upload')
    disable_owner()
    monkeypatch.setattr(upload_tasks, 'get_complaint_text_from_file', lambda *a: pytest.fail('parser called'))
    monkeypatch.setattr(upload_tasks, 'parse_project_uploaded_file', lambda **kw: pytest.fail('parser called'))
    monkeypatch.setattr(upload_tasks, 'process_project_uploaded_file', lambda **kw: pytest.fail('parser called'))
    for task in (upload_tasks.upload_batch_task, upload_tasks.smart_search_upload_file_task,
                 upload_tasks.case_background_upload_file_task):
        monkeypatch.setattr(task, 'update_state', lambda **kw: None)
    file_meta = {'filename': 'input.txt', 'temp_path': str(path)}
    if kind == 'chat':
        result = upload_tasks.case_assistant_upload_file_task.run(
            {'draft_id': 'draft-1', **file_meta})
        assert result['reason'] == 'account_unavailable'
    else:
        task = {'search': upload_tasks.smart_search_upload_file_task,
                'background': upload_tasks.case_background_upload_file_task,
                'batch': upload_tasks.upload_batch_task}[kind]
        result = task.run({'project_id': 1, 'file': file_meta, 'files': [file_meta]})
        assert result['failed_files'][0]['reason'] == 'account_unavailable'
    assert not path.exists()
    assert UploadedFiles.query.count() == 0


def test_chat_upload_disabled_during_parse_does_not_publish(app, tmp_path, monkeypatch):
    path = tmp_path / 'input.txt'
    path.write_text('original upload')
    def parse(*args):
        disable_owner()
        return 'parsed text', '', False
    monkeypatch.setattr(upload_tasks, 'get_complaint_text_from_file', parse)
    result = upload_tasks.case_assistant_upload_file_task.run(
        {'draft_id': 'draft-1', 'filename': 'input.txt', 'temp_path': str(path)})
    assert result['reason'] == 'account_unavailable'
    assert UploadedFiles.query.count() == 0
    assert not path.exists()


def test_project_upload_disabled_during_parse_does_not_publish(app, tmp_path, monkeypatch):
    path = tmp_path / 'input.txt'
    path.write_text('original upload')
    def parse(**kwargs):
        disable_owner()
        return 'parsed text', 1
    monkeypatch.setattr(upload_processing, 'parse_project_uploaded_file', parse)
    with pytest.raises(AccountUnavailable):
        upload_processing.process_project_uploaded_file(
            temp_path=str(path), safe_filename='input.txt', project_id=1, source='search')
    assert UploadedFiles.query.count() == 0


@pytest.mark.parametrize('disable_during_provider', [False, True])
def test_metadata_denial_preserves_existing_data(app, monkeypatch, disable_during_provider):
    app.config['DOCUMENT_PREPARATION_ENABLED'] = False
    previous = '{"status": "ready", "summary": "Earlier saved metadata"}'
    uploaded = UploadedFiles(unique_id='prior', original_filename='input.txt', file_size=4,
                             content='text', project_id=1, source='search', metadata_json=previous)
    db.session.add(uploaded)
    db.session.commit()
    calls = []
    def extract(**kwargs):
        calls.append(kwargs)
        disable_owner()
        return {'status': 'ready', 'summary': 'New result must not publish'}
    monkeypatch.setattr(metadata_tasks, 'extract_document_metadata', extract)
    if not disable_during_provider:
        disable_owner()
    result = metadata_tasks.document_metadata_task.run(uploaded.id)
    assert result['reason'] == 'account_unavailable'
    assert len(calls) == int(disable_during_provider)
    db.session.expire(uploaded)
    assert uploaded.metadata_json == previous


@pytest.mark.parametrize('stage', ['queued', 'provider', 'publication'])
def test_background_brief_denial_preserves_saved_workspace(app, monkeypatch, stage):
    from backend import everything
    workspace.append_turn(1, 'question', 'answer')
    workspace.patch_auxiliary(1, {'conversation_brief': 'Earlier brief', 'constraints': {'tone': 'formal'}})
    db.session.commit()
    before = workspace.load(1)
    provider_calls = []

    def update(*args, **kwargs):
        provider_calls.append(args)
        if stage == 'provider':
            disable_owner()
        return 'Late brief', {'tone': 'new'}

    load = everything.load_state_from_memory
    loads = []
    def reload_state(memory):
        value = load(memory)
        loads.append(value)
        if stage == 'publication' and len(loads) == 2:
            disable_owner()
        return value

    monkeypatch.setattr(everything, 'update_conversation_brief', update)
    monkeypatch.setattr(everything, 'load_state_from_memory', reload_state)
    if stage == 'queued':
        disable_owner()
    everything.everything_update_brief_task.run(
        {'doc_id': 1, 'user_message': 'question', 'assistant_response': 'answer'})
    assert len(provider_calls) == int(stage != 'queued')
    db.session.expire_all()
    after = workspace.load(1)
    for key in ('conversation_brief', 'constraints', 'draft', 'draft_version', 'chat_history'):
        assert after[key] == before[key]
