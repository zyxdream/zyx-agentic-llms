"""Exercise uploaded revision results through the real phase-2 agent bridge."""
import copy
import json

from backend import everything
from backend.database import db
from backend.models import AgentArtifact
from tests.test_agent_controller import agent, last_result, operation, state
from tests.test_document_preparation import app


def uploaded_revision(agent, monkeypatch, preserve_result):
    """Stub the existing revision helper, keeping run_draft_operation real."""
    target = json.loads(agent.data_json)['document_snapshot']['files'][0]['file_id']
    calls = []
    def preserve(**kwargs):
        calls.append(kwargs)
        return copy.deepcopy(preserve_result)
    monkeypatch.setattr(everything, 'run_preserve_original_uploaded_revision', preserve)
    # Draft normalization may make a provider call for blocks. The helper's
    # already structured draft is sufficient for this result-contract test.
    monkeypatch.setattr(everything, 'normalize_draft_for_phase2', lambda draft, **kwargs: draft)
    operation(agent, monkeypatch, 'revise_draft', {
        'instructions': 'Correct only the identified source passage.',
        'material_ids': [], 'target': target, 'revision_mode': 'preserve_original',
    })
    assert len(calls) == 1
    assert calls[0]['primary_draft_file_id'] == target
    assert calls[0]['supporting_evidence']
    return last_result(agent)


def test_real_uploaded_bridge_retains_nested_rejections_and_truthful_status(agent, monkeypatch):
    rejected = [{'anchor': 'p_9999', 'reason': 'unknown_anchor'}]
    operations = [
        {'op': 'preserve_original_target_selection', 'anchors': ['p_0001']},
        {'op': 'preserve_original_edit', 'edits_applied': 1, 'edits_rejected': rejected},
    ]
    before = state(agent)
    result = uploaded_revision(agent, monkeypatch, {
        'error': False, 'blocked': False,
        'message': 'I changed the current draft to the right. One proposed edit was rejected.',
        'draft': {'title': 'Uploaded candidate', 'sections': [
            {'section_id': '1', 'section_name': 'Facts', 'section_content': 'Corrected source facts.'},
        ]},
        'operations': operations,
    })
    assert result['message'] == 'Draft candidate saved; editor unchanged. Some requested edits were not applied.'
    assert result['rejected_operations'] == rejected
    saved = db.session.get(AgentArtifact, result['artifact_id'])
    body = json.loads(saved.content_json)
    assert body['operations'] == operations
    assert body['rejected_operations'] == rejected
    assert body['generation_notes'].startswith('I changed the current draft to the right.')
    assert body['draft']['sections'][0]['section_content'] == 'Corrected source facts.'
    assert state(agent) == before
    assert AgentArtifact.query.filter_by(workflow_run_id=agent.id, kind='draft').count() == 1


def test_real_uploaded_bridge_reports_blocked_revision_without_candidate_or_publication(agent, monkeypatch):
    message = 'I need a narrower instruction before preserving and revising the uploaded draft. Specify the passage.'
    before = state(agent)
    result = uploaded_revision(agent, monkeypatch, {
        'error': False, 'blocked': True, 'message': message, 'draft': None,
        'operations': [{'op': 'preserve_original_target_selection', 'needs_confirmation': True}],
    })
    assert result == {'error': True, 'message': message}
    assert AgentArtifact.query.filter_by(workflow_run_id=agent.id, kind='draft').count() == 0
    assert state(agent) == before
    assert agent.status == 'running'
