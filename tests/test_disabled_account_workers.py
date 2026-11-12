"""Queued and in-flight work must recheck account access before saving output."""
from importlib import import_module

import pytest
from sqlalchemy.orm.attributes import set_committed_value
from unittest.mock import MagicMock

from backend import document_preparation_tasks as preparation_tasks
from backend.ai import base
from backend.database import db
from backend.models import (AgentArtifact, ChatThread, ConversationTurn, DocumentChunk,
                            DocumentPreparation, DocumentPreparedArtifact, DocumentSummarySegment,
                            TextEntry, UploadedFiles, User)
from backend.util import preparation_summary, workspace_store
from backend.util.account_access import AccountUnavailable
from backend.util.attachment_parser import WorkPending
from backend.word_addin import routes as word_routes, tasks as word_tasks
from tests.test_agent_controller import agent, app, choose, last_result, operation, state, tick
from tests.test_document_preparation import admit, prepared_provider, receive
from tests.test_word_addin import app as word_app, headers, payload
from tests.test_caseviewer_tasks import FakeRedis, _payload, _valid_caseviewer_response


def disable_owner():
    # Keep the worker's cached User object stale, as in a long-running worker.
    owner = db.session.get(User, 1)
    assert not owner.is_disabled
    User.query.filter_by(id=1).update({'is_disabled': True}, synchronize_session=False)
    db.session.commit()
    set_committed_value(owner, 'is_disabled', False)
    return owner


def test_disabled_queued_agent_stops_before_model(agent, monkeypatch):
    before = state(agent)
    disable_owner()
    monkeypatch.setattr(base, 'responses_completion', lambda *a, **kw: pytest.fail('Disabled work called the model'))

    assert tick(agent) == {'stopped': True}
    assert agent.status == 'failed'
    assert state(agent) == before
    assert ConversationTurn.query.count() == 0
    assert AgentArtifact.query.count() == 0


def test_disable_during_analysis_preserves_saved_work_but_rejects_late_output(agent, monkeypatch):
    monkeypatch.setattr(base, 'chat_completion', lambda *a, **kw: 'Previously completed analysis.')
    operation(agent, monkeypatch, 'analyze_material', {'instructions': 'Analyze', 'material_ids': []})
    saved_id = last_result(agent)['artifact_id']
    saved_content = db.session.get(AgentArtifact, saved_id).content_json
    before = state(agent)
    choose(monkeypatch, 'analyze_material', {'instructions': 'Continue analysis', 'material_ids': [saved_id]})
    assert tick(agent) == {'continued': True}

    def delayed_provider(*a, **kw):
        disable_owner()
        return 'Late analysis must not be saved.'

    monkeypatch.setattr(base, 'chat_completion', delayed_provider)
    assert tick(agent) == {'stopped': True}
    assert agent.status == 'failed'
    assert AgentArtifact.query.count() == 1
    assert db.session.get(AgentArtifact, saved_id).content_json == saved_content
    assert state(agent) == before
    assert ConversationTurn.query.count() == 0


def test_disabled_queued_preparation_does_not_parse_or_summarize(app, monkeypatch):
    entry = receive(admit('disabled-upload'))
    disable_owner()
    monkeypatch.setattr(preparation_tasks, 'parse_attachment', lambda *a, **kw: pytest.fail('Disabled upload was parsed'))
    monkeypatch.setattr(preparation_summary, '_call', lambda *a, **kw: pytest.fail('Disabled upload called the model'))

    assert preparation_tasks.prepare_document.run(entry['document_id'], entry['generation']) == {
        'ok': False, 'reason': 'account_unavailable'}
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    assert doc.status == 'failed' and doc.error_code == 'account_unavailable'
    assert doc.lease_token is None and not doc.dispatch_pending
    assert DocumentPreparedArtifact.query.count() == 0


@pytest.mark.parametrize('pending', [False, True])
def test_disable_during_preparation_preserves_ready_segments_and_stops_continuation(app, monkeypatch, pending):
    entry = receive(admit('partial-upload'))
    assert preparation_tasks.prepare_document.run(entry['document_id'], entry['generation'])['continuing']
    ready = {row.segment_key: row.result_json for row in DocumentSummarySegment.query.filter_by(state='ready')}
    assert ready

    def delayed_provider(*args, **kwargs):
        disable_owner()
        if pending:
            raise WorkPending()
        return prepared_provider(*args, **kwargs)

    monkeypatch.setattr(preparation_summary, '_call', delayed_provider)
    result = preparation_tasks.prepare_document.run(entry['document_id'], entry['generation'])
    assert result == {'ok': False, 'reason': 'account_unavailable'}
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    assert doc.status == 'failed' and doc.lease_token is None and not doc.dispatch_pending
    assert {row.segment_key: row.result_json for row in DocumentSummarySegment.query.filter_by(state='ready')} == ready
    assert DocumentPreparedArtifact.query.count() == 0


def queue_word(app):
    operation_id = app.test_client().post('/api/word/operations', headers=headers(), json=payload()).json['operation_id']
    return ChatThread.query.filter_by(public_id=operation_id).one()


def test_disabled_queued_word_operation_does_not_call_provider(word_app, monkeypatch):
    chat = queue_word(word_app)
    disable_owner()
    monkeypatch.setattr(word_tasks, 'execute', lambda *a, **kw: pytest.fail('Disabled work called the provider'))

    word_tasks.word_operation.run(chat.id)
    assert word_routes.entry(chat.id, 'word.status')['state'] == 'failed'
    assert word_routes.entry(chat.id, 'word.result') is None
    assert not workspace_store.load(chat.id)['chat_history']
    assert word_app.extensions['word_redis'].get('word:active:1') is None


@pytest.mark.parametrize('checkpoint', [False, True])
def test_disable_during_word_work_prevents_checkpoint_or_final_publication(word_app, monkeypatch, checkpoint):
    chat = queue_word(word_app)
    before = workspace_store.load(chat.id)

    def delayed_provider(request, project_id, check):
        disable_owner()
        if checkpoint:
            check('Preparing the next provider call…')
            pytest.fail('Disabled work passed its checkpoint')
        return {'kind': 'revision', 'text': 'Late proposed text', 'findings': [], 'notes': [], 'sources': []}

    monkeypatch.setattr(word_tasks, 'execute', delayed_provider)
    word_tasks.word_operation.run(chat.id)
    assert word_routes.entry(chat.id, 'word.status')['state'] == 'failed'
    assert word_routes.entry(chat.id, 'word.result') is None
    after = workspace_store.load(chat.id)
    assert (after['draft'], after['draft_version'], after['chat_history']) == (
        before['draft'], before['draft_version'], before['chat_history'])
    assert ConversationTurn.query.count() == 0
    assert word_app.extensions['word_redis'].get('word:active:1') is None


@pytest.mark.parametrize('queued', [True, False])
def test_caseviewer_account_denial_skips_both_caches_and_releases_active_key(app, monkeypatch, queued):
    from backend import caseviewer_tasks
    cached_owner = db.session.get(User, 1)
    fake_redis = FakeRedis()
    monkeypatch.setattr(caseviewer_tasks, '_caseviewer_redis', fake_redis)
    cached = MagicMock(return_value=(None, {'reason': 'cache_miss'}, None))
    monkeypatch.setattr(caseviewer_tasks, 'load_cached_caseviewer_response', cached)
    global_save, local_save = MagicMock(), MagicMock()
    monkeypatch.setattr(caseviewer_tasks, 'save_caseviewer_global_cache', global_save)
    monkeypatch.setattr(caseviewer_tasks, 'save_caseviewer_validated_case', local_save)

    def delayed_provider(**kwargs):
        if queued:
            pytest.fail('Disabled queued work fetched live case details')
        disable_owner()
        return _valid_caseviewer_response(), {}

    monkeypatch.setattr(caseviewer_tasks, 'build_live_caseviewer_payload', delayed_provider)
    if queued:
        disable_owner()
    active_key = 'caseviewer:active:1:draft-public-id:123'
    result = caseviewer_tasks.run_caseviewer_enrichment(_payload(user_id=1, active_task_key=active_key))
    assert result['error'] is True and result['cluster'] is None
    assert result['error_message'] == 'This account cannot use LexText.'
    assert cached_owner.is_disabled is False  # The guard must read committed DB status.
    assert cached.call_count == (0 if queued else 1)
    global_save.assert_not_called()
    local_save.assert_not_called()
    fake_redis.delete.assert_called_once_with(active_key)


@pytest.mark.parametrize('stage', ['queued', 'parsing', 'enabled'])
def test_standalone_hg_upload_rechecks_owner_and_cleans_active_task(app, tmp_path, monkeypatch, stage):
    from backend import hallucination_guard as hg
    from backend.util.db import DBMemory
    chat = db.session.get(ChatThread, 1)
    chat.docCategory = hg.STANDALONE_HG_DRAFT_TYPE
    db.session.commit()
    source = tmp_path / 'legal-source.txt'
    source.write_text('A legal source document.', encoding='utf-8')
    memory = DBMemory(db=db, doc_id=chat.id)
    memory[hg.STANDALONE_HG_ACTIVE_UPLOAD_TASK_KEY] = 'hg-upload-task'
    monkeypatch.setattr(hg, '_standalone_hg_progress', lambda *a, **kw: None)

    def delayed_parser(path):
        if stage == 'queued':
            pytest.fail('Disabled upload entered parsing')
        if stage == 'parsing':
            disable_owner()
        return 'A legal source document.', '', False

    monkeypatch.setattr(hg, 'get_complaint_text_from_file', delayed_parser)
    monkeypatch.setattr(hg, 'get_last_ocr_warning', lambda: None)
    if stage == 'queued':
        disable_owner()
    result = hg.standalone_hg_upload_file_task.run({
        'draftId': chat.public_id, 'temp_path': str(source), 'filename': source.name})

    assert not source.exists()
    assert memory.get(hg.STANDALONE_HG_ACTIVE_UPLOAD_TASK_KEY, '') == ''
    if stage == 'enabled':
        assert result['ok'] is True
        assert UploadedFiles.query.count() == 1 and DocumentChunk.query.count() > 0
        assert memory.get('inputs/uploaded_filename') == source.name
    else:
        assert result['ok'] is False and result['error'] == 'This account cannot use LexText.'
        assert UploadedFiles.query.count() == 0 and DocumentChunk.query.count() == 0
        assert TextEntry.query.filter(TextEntry.textName.like('inputs/%')).count() == 0


@pytest.mark.parametrize('prefix', ['answer', 'rog', 'rfa', 'rfp', 'rog_response', 'rfp_response'])
@pytest.mark.parametrize('stage', ['queued', 'parsing', 'enabled'])
@pytest.mark.parametrize('combined', [False, True])
def test_legacy_parser_account_checks_preserve_cleanup(app, monkeypatch, prefix, stage, combined):
    from backend.util.db import DBMemory
    module = import_module(f'backend.{prefix}_drafter')
    response = prefix.endswith('_response')
    if combined:
        task = module.state0
        generator_name = ('generate_answer_analysis_from_memory' if prefix == 'answer'
                          else f'generate_{prefix}_setup_from_memory' if response
                          else f'generate_{prefix}_requests_from_memory')
        generator = MagicMock(return_value={'message': 'Form processed successfully'})
        monkeypatch.setattr(module, generator_name, generator)
    else:
        task = getattr(module, f'{prefix}_parse_document_task' if response else f'{prefix}_parse_complaint_task')
    parser_name = 'get_complaint_text_from_file' if response else 'get_complaint_text_with_line_metadata_from_file'
    active_key = getattr(module, f'{prefix.upper()}_ACTIVE_PARSE_TASK_KEY')
    memory = DBMemory(db=db, doc_id=1)
    if not combined:
        memory[active_key] = 'queued-parser'
    monkeypatch.setattr(module, f'_{prefix}_progress', lambda *a, **kw: None)
    if hasattr(module, 'get_last_ocr_warning'):
        monkeypatch.setattr(module, 'get_last_ocr_warning', lambda: None)

    def delayed_parser(path):
        if stage == 'queued':
            pytest.fail('Disabled queued task entered parsing')
        if stage == 'parsing':
            disable_owner()
        return ('A parsed source document.', 'case-123', False) if response else (
            'A parsed source document.', 'case-123', False, [])

    monkeypatch.setattr(module, parser_name, delayed_parser)
    if stage == 'queued':
        disable_owner()
    args = ['source.txt', 'draft-1'] + ([] if prefix == 'answer' else ['defendant'])
    if combined and stage != 'enabled':
        with pytest.raises(AccountUnavailable, match='This account cannot use LexText.'):
            task.run(*args)
        generator.assert_not_called()
        assert TextEntry.query.filter(TextEntry.textName.like('inputs/%')).count() == 0
        return
    result = task.run(*args)
    assert memory.get(active_key, '') == ''
    if stage == 'enabled':
        assert not result.get('error_message')
        assert memory.get('inputs/uploaded_filename') == 'source.txt'
        if combined:
            generator.assert_called_once()
    else:
        assert result['message'] == 'error'
        assert 'This account cannot use LexText.' in result['error_message']
        assert TextEntry.query.filter(TextEntry.textName.like('inputs/%')).count() == 0
