"""Editor interleavings and all-or-nothing draft/turn publication on real SQL."""
import copy
import json
from types import SimpleNamespace

import pytest

from backend import agent_tasks
from backend.workspace import state as everything, routes as views
from backend.database import db
from backend.models import ConversationTurn, Drafts, TextEntry
from backend.util import workspace_service as workflow, document_drafts, workspace_store
from tests.test_agent_controller import agent, operation, choose, tick, last_result
from tests.test_document_preparation import app


def editor(agent, app, monkeypatch, draft, version, *, endpoint=None):
    monkeypatch.setattr(views, 'current_user', SimpleNamespace(id=1))
    document = workspace_store.document_for_chat(agent.chat_thread_id)
    with app.test_request_context(method='PATCH', json={'draft':draft,'expected_version':version}):
        result = views.document.__wrapped__(document.public_id)
    body,status = result if isinstance(result,tuple) else (result,200)
    body = {**body, 'draft_version':body.get('document_version')}
    if body.get('code')=='document_conflict': body['code']='draft_conflict'
    return body,status


def candidate(agent, monkeypatch):
    monkeypatch.setattr(document_drafts, 'write_document', lambda **kwargs:
        document_drafts.MarkdownDraftResult(title='Published candidate',
            markdown='# Published candidate\n\nNew content.', update_court_format=False))
    operation(agent, monkeypatch, 'create_draft', {'instructions': 'Create a candidate.', 'material_ids': []})
    return last_result(agent)['artifact_id']


def test_answer_only_publication_and_later_editor_save_preserve_each_other(agent, app, monkeypatch):
    before = workspace_store.load(agent.draft_id)
    edited = copy.deepcopy(before['draft'])
    edited['title'] = 'Manual title'
    assert operation(agent, monkeypatch, 'complete_task',
                     {'response': 'Durable answer.', 'candidate_id': None}) == {'completed': True}
    published = workspace_store.load(agent.draft_id)
    assert published['draft_version'] == before['draft_version']
    body, status = editor(agent, app, monkeypatch, edited, before['draft_version'])
    assert status == 200 and body['draft_version'] == before['draft_version'] + 1
    restored = workspace_store.load(agent.draft_id)
    assert restored['draft']['title'] == 'Manual title'
    assert restored['chat_history'] == [[json.loads(agent.data_json)['query'], 'Durable answer.']]
    assert restored['chat_history_meta'][0]['workflow_completed']
    assert agent.status == 'completed'


def test_editor_change_before_publication_keeps_freshness_protection(agent, app, monkeypatch):
    before = workspace_store.load(agent.draft_id)
    edited = copy.deepcopy(before['draft'])
    edited['title'] = 'Manual edit wins'
    assert editor(agent, app, monkeypatch, edited, before['draft_version'])[1] == 200
    assert operation(agent, monkeypatch, 'complete_task',
                     {'response': 'Stale answer.', 'candidate_id': None}) == {'stopped': True}
    restored = workspace_store.load(agent.draft_id)
    assert restored['draft']['title'] == 'Manual edit wins'
    assert restored['chat_history'] == [] and agent.status == 'failed'


def test_answer_publication_preserves_concurrent_case_enrichment(agent, monkeypatch):
    def finalize(text, *args, **kwargs):
        workspace_store.patch_auxiliary(agent.draft_id,
            {'validated_cases': {'new-case': {'case_name': 'Newly enriched source'}}}, merge=('validated_cases',))
        db.session.commit()
        return text, {}, {}
    monkeypatch.setattr(everything, 'finalize_llm_response_for_display', finalize)
    assert operation(agent, monkeypatch, 'complete_task',
                     {'response': 'Answer only.', 'candidate_id': None}) == {'completed': True}
    assert workspace_store.load(agent.draft_id)['validated_cases']['new-case']['case_name'] == 'Newly enriched source'


def test_candidate_publication_rejects_stale_editor_save_without_losing_answer(agent, app, monkeypatch):
    uid = candidate(agent, monkeypatch)
    before = workspace_store.load(agent.draft_id)
    assert operation(agent, monkeypatch, 'complete_task',
                     {'response': 'Draft and answer published.', 'candidate_id': uid}) == {'completed': True}
    body, status = editor(agent, app, monkeypatch, before['draft'], before['draft_version'])
    assert status == 409 and body['code'] == 'draft_conflict'
    restored = workspace_store.load(agent.draft_id)
    assert body['draft_version'] == restored['draft_version'] == before['draft_version'] + 1
    assert restored['draft']['revision_id'] == uid
    assert restored['draft_before'] == before['draft']
    assert restored['chat_history'][0][1] == 'Draft and answer published.'


def test_two_editor_saves_from_one_version_do_not_silently_overwrite(agent, app, monkeypatch):
    before = workspace_store.load(agent.draft_id)
    first = copy.deepcopy(before['draft'])
    first['title'] = 'First editor'
    assert editor(agent, app, monkeypatch, first, before['draft_version'])[1] == 200
    second = copy.deepcopy(before['draft'])
    second['title'] = 'Second stale editor'
    body, status = editor(agent, app, monkeypatch, second, before['draft_version'])
    assert status == 409 and body['code'] == 'draft_conflict'
    assert workspace_store.load(agent.draft_id)['draft']['title'] == 'First editor'








def test_publication_rolls_back_staged_draft_before_error_observation(agent, monkeypatch):
    uid = candidate(agent, monkeypatch)
    before = workspace_store.load(agent.draft_id)
    def fail(*args, **kwargs):
        raise ValueError('Injected turn-storage failure.')
    monkeypatch.setattr(workspace_store, 'append_turn', fail)
    assert operation(agent, monkeypatch, 'complete_task',
                     {'response': 'Must not publish partially.', 'candidate_id': uid}) == {'continued': True}
    restored = workspace_store.load(agent.draft_id)
    assert restored['draft'] == before['draft']
    assert restored['draft_version'] == before['draft_version']
    assert not restored['chat_history']
    assert ConversationTurn.query.filter_by(run_id=agent.id).count() == 0
    assert agent.status == 'running'
    assert last_result(agent)['error'] is True


def test_publication_rolls_back_turn_and_draft_when_completion_staging_fails(agent, monkeypatch):
    uid = candidate(agent, monkeypatch)
    before = workspace_store.load(agent.draft_id)
    def fail(*args, **kwargs):
        raise ValueError('Injected completion failure.')
    monkeypatch.setattr(workflow, 'stage_publication', fail)
    assert operation(agent, monkeypatch, 'complete_task',
                     {'response': 'No half publication.', 'candidate_id': uid}) == {'continued': True}
    restored = workspace_store.load(agent.draft_id)
    assert restored['draft'] == before['draft'] and not restored['chat_history']
    assert restored['draft_version'] == before['draft_version']
    assert ConversationTurn.query.filter_by(run_id=agent.id).count() == 0


def test_recovery_uses_canonical_turn_even_when_workspace_blob_has_no_history(agent, monkeypatch):
    assert operation(agent, monkeypatch, 'complete_task',
                     {'response': 'Once only.', 'candidate_id': None}) == {'completed': True}
    # Simulate stale bookkeeping, not a second publication: the canonical turn
    # is the durable completion identity even if an old JSON blob is empty.
    agent.status, agent.active_draft_id, agent.result_json = 'running', agent.draft_id, None
    db.session.commit()
    assert workflow.complete_published_answer(agent)
    assert agent.status == 'completed'
    assert json.loads(agent.result_json)['response'] == 'Once only.'
    assert ConversationTurn.query.filter_by(run_id=agent.id).count() == 1
    assert len(workspace_store.load(agent.draft_id)['chat_history']) == 1






def test_admission_pins_document_and_controller_receives_public_identities(agent, monkeypatch):
    document = workspace_store.document_for_chat(agent.chat_thread_id)
    data = json.loads(agent.data_json)
    assert agent.draft_document_id == document.id
    assert agent.base_draft_version == document.draft_version
    assert data['chat_thread_id'] == 'draft-1'
    assert data['draft_document_id'] == document.public_id
    choose(monkeypatch, 'complete_task', {'response': 'Answer only.', 'candidate_id': None})
    tick(agent)
    context = json.loads(agent.payload_json)['agent_context']
    user_input = json.loads(context['input'][1]['content'])
    assert user_input['chat_thread_id'] == 'draft-1'
    assert user_input['current_draft']['draft_document_id'] == document.public_id
    assert context['state']['draft_document_id'] == document.public_id










def test_publication_records_owned_document_in_turn_step_and_workflow(agent, monkeypatch):
    before = workspace_store.load(agent.chat_thread_id)
    uid = candidate(agent, monkeypatch)
    assert operation(agent, monkeypatch, 'complete_task',
        {'response': 'Published with identity.', 'candidate_id': uid}) == {'completed': True}
    restored = workspace_store.load(agent.chat_thread_id)
    turn = workspace_store.turn_for_workflow(agent.id)
    for payload in (json.loads(turn.metadata_json), json.loads(agent.result_json), last_result(agent)):
        assert payload['chat_thread_id'] == before['chat_thread_id']
        assert payload['draft_document_id'] == before['draft_document_id']
    assert restored['draft_document_id'] == before['draft_document_id']


def test_version_only_change_blocks_new_candidate_without_losing_saved_artifact(agent, monkeypatch):
    uid = candidate(agent, monkeypatch)
    before = workspace_store.load(agent.chat_thread_id)
    workspace_store.update_document(agent.chat_thread_id, before['draft_document_id'],
        before['draft'], expected_version=before['draft_version'])
    db.session.commit()
    assert operation(agent, monkeypatch, 'complete_task',
        {'response': 'Stale version.', 'candidate_id': uid}) == {'stopped': True}
    assert workspace_store.load(agent.chat_thread_id)['draft'] == before['draft']
    assert workspace_store.turn_for_workflow(agent.id) is None
    from backend.models import AgentArtifact
    assert db.session.get(AgentArtifact, uid) is not None




def test_new_admission_accepts_explicit_ids_without_legacy_alias(agent, monkeypatch):
    assert operation(agent, monkeypatch, 'complete_task',
        {'response': 'First answer.', 'candidate_id': None}) == {'completed': True}
    state = workspace_store.load(agent.chat_thread_id)
    snapshot = json.loads(agent.data_json)['document_snapshot']
    row = workflow.submit(1, {'chat_thread_id': state['chat_thread_id'],
        'draft_document_id': state['draft_document_id'], 'query': 'Next request.',
        'document_snapshot': snapshot})
    assert row.chat_thread_id == agent.chat_thread_id
    assert row.draft_document_id == workspace_store.document_for_chat(agent.chat_thread_id).id
    assert row.base_draft_version == state['draft_version']






def test_candidate_with_another_document_identity_is_not_published(agent, monkeypatch):
    uid = candidate(agent, monkeypatch)
    from backend.models import AgentArtifact
    saved = db.session.get(AgentArtifact, uid)
    content = json.loads(saved.content_json)
    content['draft_document_id'] = workspace_store.document_for_chat(2).public_id
    saved.content_json = workflow.encode(content)
    db.session.commit()
    assert operation(agent, monkeypatch, 'complete_task',
        {'response': 'Wrong target.', 'candidate_id': uid}) == {'continued': True}
    assert workspace_store.turn_for_workflow(agent.id) is None
    assert last_result(agent)['error'] is True


def capture_write_lock_order(monkeypatch):
    from sqlalchemy.orm import Query
    entities = []
    original = Query.with_for_update
    def capture(query, *args, **kwargs):
        entities.extend(description['entity'].__name__ for description in query.column_descriptions
                        if description.get('entity') is not None)
        return original(query, *args, **kwargs)
    monkeypatch.setattr(Query, 'with_for_update', capture)
    return entities


def test_publication_locks_chat_before_workflow_and_document(agent, monkeypatch):
    from backend.ai import agent_tools
    from backend.workspace.execution import claim_workflow
    uid = candidate(agent, monkeypatch)
    arguments = {'response': 'Ordered locks.', 'candidate_id': uid}
    choose(monkeypatch, 'complete_task', arguments)
    assert tick(agent) == {'continued': True}
    step = agent_tasks.steps(agent).filter_by(name='complete_task').one()
    token = claim_workflow(agent.id, task_id=agent.expected_task_id, task_field='expected_task_id')
    monkeypatch.setattr(agent_tasks, 'check_editor_write', lambda *args: None)
    entities = capture_write_lock_order(monkeypatch)
    with workflow.execution(agent.id, token):
        agent_tasks.publish(agent, agent_tools.arguments('complete_task', arguments), step)
    assert entities[0] == 'ChatThread'
    assert entities.index('ChatThread') < entities.index('WorkspaceRun') < entities.index('DraftDocument')


def test_revision_download_keeps_the_saved_document_after_manual_edit(agent,app,monkeypatch,tmp_path):
    uid=candidate(agent,monkeypatch)
    assert operation(agent,monkeypatch,'complete_task',{'response':'Saved.','candidate_id':uid})=={'completed':True}
    before=workspace_store.load(agent.chat_thread_id)
    edited={**before['draft'],'markdown':'# Later manual edit','revision_id':None}
    assert editor(agent,app,monkeypatch,edited,before['draft_version'])[1]==200
    document=workspace_store.document_for_chat(agent.chat_thread_id)
    app.config['DOCUMENT_EXPORT_STORAGE_DIR']=str(tmp_path/'exports')
    with app.test_request_context(method='POST',json={'format':'markdown'}):
        response=views.download_revision.__wrapped__(document.public_id,uid)
        assert response.status_code==200
        response.direct_passthrough=False
        assert b'Published candidate' in response.get_data()
        assert b'Later manual edit' not in response.get_data()
        response.close()
