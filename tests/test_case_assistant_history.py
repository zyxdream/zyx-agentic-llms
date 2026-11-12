"""Restore stopped turns among saved answers without duplicating chat history."""
import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend import views
from backend.database import db
from backend.util.case_assistant_workflow import Workflow
from backend.util.workspace_store import load
from tests.test_case_assistant_workflows import workflow
from tests.test_document_preparation import app


@pytest.fixture
def history_document(workflow, app, monkeypatch):
    workflow.status, workflow.phase, workflow.active_draft_id = 'failed', 'finalize', None
    workflow.error_message = 'The earlier request failed.'
    original = json.loads(workflow.data_json)
    original['query'] = 'Repeat this request.'
    original['promptAttachments'] = [{'file_id': 'failed-source', 'file_name': 'Failed source.pdf'}]
    workflow.data_json = json.dumps(original)
    db.session.commit()
    state = {'draft': {'title': 'Saved draft', 'sections': []}, 'chat_history': [], 'chat_history_meta': []}
    canonical = load(workflow.chat_thread_id)
    state.update({key: canonical[key] for key in ('chat_thread_id', 'draft_document_id', 'draft_version')})
    def ownership(d, jwt_id):
        ownership._last_draft = SimpleNamespace(id=workflow.chat_thread_id, public_id=canonical['chat_thread_id'])
    monkeypatch.setattr(views, 'ownership_check', ownership)
    monkeypatch.setattr(views, 'current_user', SimpleNamespace(id=workflow.user_id))
    monkeypatch.setattr(views, 'DBMemory', lambda **kw: object())
    monkeypatch.setattr(views, 'load_state_from_memory', lambda memory: state)
    def add_request(uid, minutes, status='completed', **overrides):
        values = {column.name: getattr(workflow, column.name) for column in Workflow.__table__.columns
                  if column.name not in ('id', 'active_chat_thread_id')}
        values.update(status=status, created_at=workflow.created_at + timedelta(minutes=minutes), **overrides)
        row = Workflow(id=uid, active_draft_id=None, **values)
        db.session.add(row)
        db.session.commit()
        return row
    def read():
        with app.test_request_context(json={'draftId': 'draft-1'}):
            body, status = views.get_document.__wrapped__()
        assert status == 200
        return body
    return state, add_request, read


@pytest.mark.parametrize('already_saved', [False, True])
def test_failed_turns_restore_before_later_answers_with_aligned_metadata(workflow, history_document, already_saved):
    state, add_request, read = history_document
    add_request('wf_earlier', -1)
    add_request('wf_later', 2)
    add_request('wf_second_failure', 1, status='failed')
    add_request('wf_other_chat', 0, status='failed', draft_id=2)
    add_request('wf_other_user', 0, status='failed', user_id=2)
    state['chat_history'] = [['Legacy request', 'Legacy answer'],
        ['Repeat this request.', 'Earlier answer'], ['Repeat this request.', 'Later answer']]
    state['chat_history_meta'] = [{}, {'workflow_run_id': 'wf_earlier', 'marker': 'earlier'},
        {'workflow_run_id': 'wf_later', 'prompt_attachments': [{'file_id': 'later-source'}]}]
    if already_saved:
        state['chat_history'].insert(2, ['Repeat this request.', 'Saved failure text'])
        state['chat_history_meta'].insert(2, {'workflow_run_id': workflow.id, 'marker': 'keep'})
    original_history = copy.deepcopy(state['chat_history'])
    for _ in range(2):
        body = read()
        meta = body['chatHistoryMeta']
        assert [item.get('workflow_run_id') for item in meta] == [
            None, 'wf_earlier', workflow.id, 'wf_second_failure', 'wf_later']
        assert len(body['chatHistory']) == len(meta) == 5
        assert [item[1] for item in body['chatHistory']] == ['Legacy answer', 'Earlier answer',
            'Saved failure text' if already_saved else workflow.error_message,
            workflow.error_message, 'Later answer']
        assert meta[1]['marker'] == 'earlier'
        assert meta[2]['workflow_failed'] and meta[2]['extraction_report'] == workflow.id
        assert meta[3]['workflow_failed'] and meta[3]['extraction_report'] == 'wf_second_failure'
        assert meta[4]['prompt_attachments'] == [{'file_id': 'later-source'}]
        if already_saved:
            assert meta[2]['marker'] == 'keep'
        else:
            assert meta[2]['prompt_attachments'] == json.loads(workflow.data_json)['promptAttachments']
        assert body['draft'] == state['draft']
    assert state['chat_history'] == original_history


def test_failed_turn_restores_after_legacy_history_without_workflow_ids(history_document):
    state, _, read = history_document
    state['chat_history'] = [['Legacy request', 'Legacy answer']]
    body = read()
    assert body['chatHistory'][0] == ['Legacy request', 'Legacy answer']
    assert len(body['chatHistory']) == len(body['chatHistoryMeta']) == 2
    assert body['chatHistoryMeta'][0] == {}
    assert body['chatHistoryMeta'][1]['workflow_failed']


@pytest.mark.parametrize('already_saved', [False, True])
def test_cancelled_agent_turn_restores_with_results_and_distinct_status(workflow, history_document, already_saved):
    state, add_request, read = history_document
    workflow.status, workflow.execution_version, workflow.error_message = 'cancelled', 1, None
    db.session.commit()
    add_request('wf_later', 2)
    add_request('wf_legacy_cancelled', 1, status='cancelled', execution_version=0)
    add_request('wf_other_chat', 0, status='cancelled', draft_id=2)
    add_request('wf_other_user', 0, status='cancelled', user_id=2)
    state['chat_history'] = [['Later request', 'Later answer']]
    state['chat_history_meta'] = [{'workflow_run_id': 'wf_later', 'marker': 'later'}]
    if already_saved:
        state['chat_history'].insert(0, ['Repeat this request.', 'Request cancelled.'])
        state['chat_history_meta'].insert(0, {'workflow_run_id': workflow.id, 'marker': 'keep'})
    original_history = copy.deepcopy(state['chat_history'])
    for _ in range(2):
        body = read()
        assert [meta.get('workflow_run_id') for meta in body['chatHistoryMeta']] == [workflow.id, 'wf_later']
        assert body['chatHistory'][0] == ['Repeat this request.', 'Request cancelled.']
        metadata = body['chatHistoryMeta'][0]
        assert metadata['workflow_cancelled'] and not metadata['workflow_failed']
        assert metadata['agent_run'] and metadata['extraction_report'] == workflow.id
        if already_saved:
            assert metadata['marker'] == 'keep'
        else:
            assert metadata['prompt_attachments'] == json.loads(workflow.data_json)['promptAttachments']
        assert body['chatHistoryMeta'][1]['marker'] == 'later'
        assert body['draft'] == state['draft']
    assert state['chat_history'] == original_history


@pytest.mark.parametrize('status', ['running', 'completed'])
def test_resumed_agent_replaces_cancelled_overlay_with_current_turn(workflow, history_document, status):
    state, _, read = history_document
    workflow.execution_version, workflow.status = 1, 'cancelled'
    db.session.commit()
    assert read()['chatHistoryMeta'][0]['workflow_cancelled']
    workflow.status, workflow.result_json = status, None
    workflow.active_draft_id = workflow.draft_id if status == 'running' else None
    if status == 'completed':
        state['chat_history'] = [['Repeat this request.', 'Completed answer']]
        state['chat_history_meta'] = [{'workflow_run_id': workflow.id, 'workflow_completed': True, 'agent_run': True}]
    db.session.commit()
    body = read()
    if status == 'running':
        assert body['chatHistory'] == body['chatHistoryMeta'] == []
        assert body['pendingTurn']['workflowRunId'] == workflow.id and body['pendingTurn']['agentRun']
        assert not body['pendingTurn'].get('isCancelled') and not body['pendingTurn'].get('workflow_cancelled')
    else:
        assert body['chatHistory'] == [['Repeat this request.', 'Completed answer']]
        assert len(body['chatHistoryMeta']) == 1 and not body['chatHistoryMeta'][0].get('workflow_cancelled')
        assert 'pendingTurn' not in body
