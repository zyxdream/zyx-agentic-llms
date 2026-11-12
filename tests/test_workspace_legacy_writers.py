"""Old entry points obey the same canonical publication boundary as the agent."""
import copy
import json

import pytest

from backend import case_assistant_tasks, everything
from backend.database import db
from backend.models import ConversationTurn, TextEntry
from backend.util import case_assistant_workflow as workflows, workspace_store as store
from backend.util.db import DBMemory
from tests.test_case_assistant_workflows import workflow
from tests.test_document_preparation import app


def test_stale_auxiliary_snapshot_cannot_replace_draft_or_new_answer(app):
    memory = DBMemory(db, 1)
    stale = everything.load_state_from_memory(memory)
    store.update_draft(1, {'title': 'New editor content', 'sections': []}, stale['draft_version'])
    store.append_turn(1, 'Question', 'Completed answer')
    db.session.commit()
    stale['validated_cases'] = {'one': {'case_name': 'Example'}}
    everything.save_state_to_memory(stale, memory, auxiliary_fields=('validated_cases',))
    restored = store.load(1)
    assert restored['draft']['title'] == 'New editor content'
    assert restored['chat_history'] == [['Question', 'Completed answer']]
    assert restored['validated_cases'] == stale['validated_cases']
    assert 'draft' not in json.loads(TextEntry.query.filter_by(textName='state').one().content)
    with pytest.raises(ValueError, match='read projection'):
        memory['state'] = json.dumps(stale)


def test_legacy_publication_commits_turn_draft_and_completion_once(workflow):
    token = case_assistant_tasks.claim_workflow(workflow.id)
    memory = DBMemory(db, workflow.draft_id)
    state = everything.load_state_from_memory(memory)
    before = state['draft_version']
    state['active_workflow'] = {'status': 'completed', 'workflow_run_id': workflow.id}
    everything.save_draft_to_state(state, {'title': 'Legacy candidate', 'sections': []})
    everything.add_chat_message(state, ('Find every deadline.', 'Legacy answer.'),
        {'workflow_run_id': workflow.id})
    with workflows.execution(workflow.id, token):
        everything.save_state_to_memory(state, memory, auxiliary_fields=('active_workflow',))
    restored = store.load(workflow.draft_id)
    assert restored['draft']['title'] == 'Legacy candidate'
    assert restored['draft_version'] == before + 1
    assert restored['chat_history'] == [['Find every deadline.', 'Legacy answer.']]
    assert workflow.status == 'completed' and workflow.active_draft_id is None
    assert json.loads(workflow.result_json)['response'] == 'Legacy answer.'
    assert workflows.complete_published_answer(workflow)
    assert ConversationTurn.query.filter_by(workflow_run_id=workflow.id).count() == 1


def test_legacy_failed_response_is_not_a_completed_retry_marker(workflow):
    token = case_assistant_tasks.claim_workflow(workflow.id)
    memory = DBMemory(db, workflow.draft_id)
    state = everything.load_state_from_memory(memory)
    state['active_workflow'] = {'status': 'failed', 'workflow_run_id': workflow.id}
    everything.add_chat_message(state, ('Question', 'Please retry.'), {'workflow_run_id': workflow.id})
    with workflows.execution(workflow.id, token):
        everything.save_state_to_memory(state, memory, auxiliary_fields=('active_workflow',))
    assert not workflows.complete_published_answer(workflow)
    assert store.turn_for_workflow(workflow.id) is None
    assert workflow.status == 'running'


@pytest.mark.parametrize('failure_point', ['append_turn', 'stage_publication'])
def test_legacy_write_failure_rolls_back_before_caller_can_commit(workflow, monkeypatch, failure_point):
    token = case_assistant_tasks.claim_workflow(workflow.id)
    memory = DBMemory(db, workflow.draft_id)
    state = everything.load_state_from_memory(memory)
    before = copy.deepcopy(state)
    state['active_workflow'] = {'status': 'completed', 'workflow_run_id': workflow.id}
    everything.save_draft_to_state(state, {'title': 'Must roll back', 'sections': []})
    everything.add_chat_message(state, ('Question', 'Must roll back'), {'workflow_run_id': workflow.id})
    def fail(*args, **kwargs):
        raise ValueError('Injected legacy publication failure')
    monkeypatch.setattr(store if failure_point == 'append_turn' else workflows, failure_point, fail)
    with workflows.execution(workflow.id, token), pytest.raises(ValueError, match='Injected'):
        everything.save_state_to_memory(state, memory, auxiliary_fields=('active_workflow',))
    db.session.commit()  # A caller may now commit a separate failure observation.
    restored = store.load(workflow.draft_id)
    assert restored['draft'] == before['draft']
    assert restored['draft_version'] == before['draft_version']
    assert restored['chat_history'] == before['chat_history']
    assert workflow.status == 'running'
