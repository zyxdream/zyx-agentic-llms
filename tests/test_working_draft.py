"""Continuous draft selection and confirmed rollback, all providers mocked."""
import copy
import json
from datetime import timedelta

import pytest

from backend import agent_tasks as tasks
from backend.ai import base, agent_tools
from backend.database import db
from backend.models import AgentArtifact, AgentStep, ConversationTurn, PublishedDraftVersion
from backend.util import document_drafts, workspace_service as service, workspace_store, working_draft, draft_history
from tests.test_agent_controller import agent, operation, tick, choose, last_result, state
from tests.test_document_preparation import app


def write(agent, monkeypatch, text, *, target=None):
    monkeypatch.setattr(document_drafts, 'write_document', lambda **kw:
        document_drafts.MarkdownDraftResult(title='Payroll', markdown=text, update_court_format=False))
    args = {'instructions': 'Apply the requested change.', 'material_ids': []}
    if target is not None:
        args.update(target=target, revision_mode='general_revision')
    assert operation(agent, monkeypatch, 'create_draft' if target is None else 'revise_draft', args) == {'continued': True}
    result = last_result(agent)
    assert not result.get('error'), result
    return result['artifact_id']


def head(agent):
    return json.loads(agent.payload_json)['agent_context']['working_candidate_id']


def test_payroll_correct_then_add_citation_keeps_corrected_base_and_publishes_last_only(agent, monkeypatch):
    initial = copy.deepcopy(state(agent)['draft'])
    first = write(agent, monkeypatch, 'Original paragraph.')
    corrected = write(agent, monkeypatch, 'Corrected paragraph.', target='working_draft')
    provider_calls = []
    def revision(**kwargs):
        provider_calls.append(kwargs)
        return document_drafts.MarkdownDraftResult(title='Payroll',
            markdown=kwargs['target']['markdown'] + ' [Citation]', update_court_format=False)
    monkeypatch.setattr(document_drafts, 'write_document', revision)
    operation(agent, monkeypatch, 'revise_draft', {'instructions': 'Add citation only.',
        'material_ids': [], 'target': first, 'revision_mode': 'general_revision'})
    assert last_result(agent)['error'] and 'working_draft' in last_result(agent)['message']
    assert provider_calls == [] and head(agent) == corrected
    operation(agent, monkeypatch, 'revise_draft', {'instructions': 'Add citation only.',
        'material_ids': [], 'target': 'working_draft', 'revision_mode': 'general_revision'})
    final = last_result(agent)['artifact_id']
    assert last_result(agent)['base_candidate_id'] == corrected
    assert state(agent)['draft'] == initial
    assert operation(agent, monkeypatch, 'complete_task', {'response': 'Done.', 'candidate_id': first}) == {'continued': True}
    assert last_result(agent)['error'] and state(agent)['draft'] == initial
    assert operation(agent, monkeypatch, 'complete_task', {'response': 'Done.', 'candidate_id': 'working_draft'}) == {'completed': True}
    assert state(agent)['draft']['markdown'] == 'Corrected paragraph. [Citation]'
    assert state(agent)['draft']['parent_revision_id'] == corrected
    assert state(agent)['chat_history_meta'][-1]['candidate_id'] == final
    assert AgentArtifact.query.filter_by(run_id=agent.id, kind='draft').count() == 3
    assert PublishedDraftVersion.query.count() == 2
    assert PublishedDraftVersion.query.filter_by(source='agent').one().candidate_id == final
    assert tick(agent) == {'stale': True}
    assert PublishedDraftVersion.query.count() == 2


@pytest.mark.parametrize('target', ['current_draft', 'upload', 'create'])
def test_old_snapshot_upload_and_extra_creation_rejected_before_writer(agent, monkeypatch, target):
    first = write(agent, monkeypatch, 'Current content.')
    monkeypatch.setattr(document_drafts, 'write_document', lambda **kw: pytest.fail('Must reject before writer'))
    args = {'instructions': 'Edit.', 'material_ids': []}
    if target != 'create':
        args.update(target=json.loads(agent.data_json)['document_snapshot']['files'][0]['file_id'] if target == 'upload' else target,
                    revision_mode='preserve_original' if target == 'upload' else 'general_revision')
    operation(agent, monkeypatch, 'create_draft' if target == 'create' else 'revise_draft', args)
    assert last_result(agent)['error'] and head(agent) == first
    assert AgentArtifact.query.filter_by(run_id=agent.id, kind='draft').count() == 1


def test_read_verify_and_context_resolve_current_head_without_selecting_old_candidate(agent, monkeypatch):
    first = write(agent, monkeypatch, 'First.')
    second = write(agent, monkeypatch, 'Second.', target=first)
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': 'working_draft', 'ranges': [], 'start': 0, 'end': None})
    assert last_result(agent)['artifact_id'] == second
    assert json.loads(last_result(agent)['text'])['draft']['markdown'] == 'Second.'
    monkeypatch.setattr(base, 'chat_completion', lambda *a, **kw: 'Report.')
    operation(agent, monkeypatch, 'verify_draft', {'target': 'working_draft', 'instructions': 'Check.', 'material_ids': [], 'check_citations': False})
    assert last_result(agent)['target'] == second
    report = db.session.get(AgentArtifact, last_result(agent)['artifact_id'])
    assert json.loads(report.content_json)['target'] == second
    operation(agent, monkeypatch, 'verify_draft', {'target': first, 'instructions': 'Compare old.', 'material_ids': [], 'check_citations': False})
    assert last_result(agent)['target'] == first and head(agent) == second
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': 'current_draft', 'ranges': [], 'start': 0, 'end': None})
    assert json.loads(last_result(agent)['text'])['draft']['title'] == 'Original'
    from backend.util import agent_context
    observed = agent_context.prepare_input(agent, {'model': 'unregistered-test-model'})
    authoritative = next(item['content'] for item in observed if item.get('content', '').startswith('Application working-draft state ('))
    assert second in authoritative and '"resolved_target": "' + second in authoritative


def test_failed_revision_leaves_head_and_retry_uses_saved_success(agent, monkeypatch):
    first = write(agent, monkeypatch, 'First.')
    monkeypatch.setattr(document_drafts, 'write_document', lambda **kw: (_ for _ in ()).throw(ValueError('Writer failed')))
    operation(agent, monkeypatch, 'revise_draft', {'target': 'working_draft', 'revision_mode': 'general_revision', 'instructions': 'Edit.', 'material_ids': []})
    assert last_result(agent)['error'] and head(agent) == first
    db.session.expire_all()
    second = write(agent, monkeypatch, 'Second.', target='working_draft')
    assert head(agent) == second
    assert json.loads(db.session.get(AgentArtifact, second).content_json)['draft']['parent_revision_id'] == first


def test_cancel_during_writer_discards_candidate_and_pointer(agent, monkeypatch):
    first = write(agent, monkeypatch, 'First.')
    def canceled(**kwargs):
        service.stop(agent, cancelled=True)
        return document_drafts.MarkdownDraftResult(title='Late', markdown='Late.', update_court_format=False)
    monkeypatch.setattr(document_drafts, 'write_document', canceled)
    operation(agent, monkeypatch, 'revise_draft', {'target': 'working_draft', 'revision_mode': 'general_revision', 'instructions': 'Edit.', 'material_ids': []})
    assert head(agent) == first
    assert AgentArtifact.query.filter_by(run_id=agent.id, kind='draft').count() == 1


def test_old_run_head_recovery_uses_successful_sequence_not_timestamps(agent, monkeypatch):
    first = write(agent, monkeypatch, 'First.')
    second = write(agent, monkeypatch, 'Second.', target=first)
    db.session.get(AgentArtifact, first).created_at = service.now() + timedelta(days=1)
    payload = json.loads(agent.payload_json)
    del payload['agent_context']['working_candidate_id']
    agent.payload_json = service.encode(payload)
    db.session.commit()
    assert working_draft.ensure(agent) == second
    db.session.commit()
    db.session.expire_all()
    assert head(agent) == second
    operation(agent, monkeypatch, 'revise_draft', {'target': first, 'revision_mode': 'general_revision', 'instructions': 'Wrong base.', 'material_ids': []})
    assert last_result(agent)['error']


def manual(chat, text):
    document = workspace_store.document_for_chat(chat)
    draft = {'format_version': 2, 'source_format': 'markdown', 'title': 'Manual', 'markdown': text,
             'export_settings': {'custom': 'retained'}}
    workspace_store.update_document(chat, document.id, draft, document.draft_version)
    db.session.commit()
    return draft


def test_manual_versions_restore_into_new_candidate_then_publish(agent, monkeypatch):
    # New run must capture the already manually saved editor, not a stale one.
    manual(agent.chat_thread_id, 'Version two.')
    manual(agent.chat_thread_id, 'Version three.')
    agent.base_draft_version = workspace_store.document_for_chat(agent.chat_thread_id).draft_version
    agent.draft_hash = service.fingerprint(service.draft_value(agent.chat_thread_id))
    db.session.commit()
    operation(agent, monkeypatch, 'list_draft_versions', {})
    versions = last_result(agent)['versions']
    assert len(versions) == 3 and all('_draft' not in item for item in versions)
    second = next(v for v in versions if v['draft_version'] == 2)
    current = workspace_store.load(agent.chat_thread_id)['draft']
    operation(agent, monkeypatch, 'restore_draft', {'version_id': second['version_id']})
    restored_id = last_result(agent)['artifact_id']
    assert state(agent)['draft'] == current
    operation(agent, monkeypatch, 'complete_task', {'response': 'Restored.', 'candidate_id': 'working_draft'})
    restored = state(agent)['draft']
    assert restored['markdown'] == 'Version two.' and restored['export_settings'] == {'custom': 'retained'}
    assert restored['revision_id'] == restored_id
    assert state(agent)['draft_version'] == 4
    assert PublishedDraftVersion.query.count() == 4
    assert json.loads(db.session.get(PublishedDraftVersion, second['version_id']).draft_json)['markdown'] == 'Version two.'


def test_restore_same_content_is_noop_and_does_not_save_candidate(agent, monkeypatch):
    operation(agent, monkeypatch, 'list_draft_versions', {})
    current = next(v for v in last_result(agent)['versions'] if v['current'])
    operation(agent, monkeypatch, 'restore_draft', {'version_id': current['version_id']})
    assert last_result(agent)['unchanged'] is True and head(agent) is None
    assert AgentArtifact.query.filter_by(kind='draft').count() == 0
    operation(agent, monkeypatch, 'complete_task', {'response': 'Already current.', 'candidate_id': 'working_draft'})
    assert PublishedDraftVersion.query.count() == 0
    assert state(agent)['draft_version'] == current['draft_version']


def test_restore_after_edits_records_continuous_parent_and_rejects_old_edit_target(agent, monkeypatch):
    operation(agent, monkeypatch, 'list_draft_versions', {})
    baseline = last_result(agent)['versions'][0]['version_id']
    first = write(agent, monkeypatch, 'New candidate.')
    operation(agent, monkeypatch, 'restore_draft', {'version_id': baseline})
    restored = last_result(agent)['artifact_id']
    value = json.loads(db.session.get(AgentArtifact, restored).content_json)
    assert value['draft']['parent_revision_id'] == first and value['restored_from_version_id'] == baseline
    assert head(agent) == restored
    operation(agent, monkeypatch, 'revise_draft', {'target': first, 'revision_mode': 'general_revision', 'instructions': 'Old.', 'material_ids': []})
    assert last_result(agent)['error'] and head(agent) == restored


def test_restore_cannot_select_foreign_document_or_unpublished_candidate(agent, monkeypatch):
    manual(2, 'Other owner content.')
    foreign = PublishedDraftVersion.query.filter_by(document_id=workspace_store.document_for_chat(2).id).first()
    operation(agent, monkeypatch, 'restore_draft', {'version_id': foreign.id})
    assert last_result(agent)['error'] and head(agent) is None
    first = write(agent, monkeypatch, 'Never published.')
    operation(agent, monkeypatch, 'restore_draft', {'version_id': first})
    assert last_result(agent)['error'] and head(agent) == first


def test_legacy_history_requires_durable_publication_not_any_candidate(agent, monkeypatch):
    first = write(agent, monkeypatch, 'Unpublished.')
    second = write(agent, monkeypatch, 'Published.', target=first)
    operation(agent, monkeypatch, 'complete_task', {'response': 'Done.', 'candidate_id': second})
    # Simulate the history available before 4A, then a later manual change.
    PublishedDraftVersion.query.delete()
    document = workspace_store.document_for_chat(agent.chat_thread_id)
    document.draft_json = service.encode({'title': 'Later manual', 'sections': []})
    document.draft_version += 1
    db.session.commit()
    versions = draft_history.entries(document)
    legacy = [v for v in versions if v['source'] == 'legacy_agent']
    assert len(legacy) == 1 and legacy[0]['version_id'] == second
    assert all(v['version_id'] != first for v in versions)
    assert draft_history.restore_content(document, second)['markdown'] == 'Published.'


def test_snapshot_and_pointer_roll_back_when_atomic_publication_fails(agent, monkeypatch):
    first = write(agent, monkeypatch, 'Current.')
    monkeypatch.setattr(service, 'stage_publication', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('Injected atomic failure')))
    with pytest.raises(RuntimeError, match='Injected atomic failure'):
        operation(agent, monkeypatch, 'complete_task', {'response': 'Done.', 'candidate_id': 'working_draft'})
    assert head(agent) == first and PublishedDraftVersion.query.count() == 0
    assert ConversationTurn.query.count() == 0
    assert state(agent)['draft']['title'] == 'Original'


def test_retry_of_interrupted_revision_retains_head_and_does_not_repeat_success(agent, monkeypatch):
    first = write(agent, monkeypatch, 'First.')
    def interrupted(**kwargs):
        raise tasks.ControllerError('Temporary failure')
    monkeypatch.setattr(document_drafts, 'write_document', interrupted)
    assert operation(agent, monkeypatch, 'revise_draft', {'target': 'working_draft',
        'revision_mode': 'general_revision', 'instructions': 'Continue.', 'material_ids': []}) == {'stopped': True}
    assert head(agent) == first
    service.retry(agent)
    captured = []
    def resume(**kwargs):
        captured.append(kwargs['target'])
        return document_drafts.MarkdownDraftResult(title='Payroll', markdown='Second.', update_court_format=False)
    monkeypatch.setattr(document_drafts, 'write_document', resume)
    assert tick(agent) == {'continued': True}
    assert captured[0]['revision_id'] == first and head(agent) != first
    assert AgentArtifact.query.filter_by(run_id=agent.id, kind='draft').count() == 2


def test_save_conflict_keeps_history_and_editor_unchanged(agent, monkeypatch):
    original = workspace_store.document_for_chat(agent.chat_thread_id)
    stale = original.draft_version
    manual(agent.chat_thread_id, 'Winner.')
    before = PublishedDraftVersion.query.count()
    with pytest.raises(workspace_store.WorkspaceConflict):
        workspace_store.update_document(agent.chat_thread_id, original.id,
            {'title': 'Loser', 'sections': []}, stale)
    db.session.rollback()
    assert PublishedDraftVersion.query.count() == before
    assert state(agent)['draft']['markdown'] == 'Winner.'
