"""Correction guidance and evidence handoff; provider responses are mocked.

These tests establish what each model receives and that requested edits are not
blocked. They do not establish a live model's substantive judgment or compliance.
"""
import json

import pytest

from backend import agent_tasks as tasks
from backend.ai import agent_tools, base
from backend.database import db
from backend.models import AgentArtifact
from backend.util import document_drafts as drafts, workspace_service as service
from tests.test_agent_controller import agent, last_result, operation, state
from tests.test_document_preparation import app
from tests.test_drafting_materials import source_material


@pytest.mark.parametrize('legal_enabled', [False, True])
def test_correction_guidance_applies_without_requiring_legal_research(monkeypatch, legal_enabled):
    monkeypatch.setattr(agent_tools, 'legal_enabled', lambda: legal_enabled)
    system = tasks.system_instructions()
    assert 'Follow user-requested revisions, including user-supplied facts' in system
    assert 'verification merely to edit' in system
    assert 'A request to check accuracy does not establish an error' in system
    assert 'quote the exact original wording' in system
    assert 'classify the alleged defect as a contradiction, overstatement, omission, or clarification' in system
    assert drafts.CONDITION_STRENGTH_GUIDANCE in system
    assert 'pass supporting source passages through material_ids' in system
    assert 'include the original passage when outside the editing target' in system
    assert 'Prior generated conclusions are not evidence' in system
    assert 'describe uncertainty rather than instruct a definitive correction' in system


@pytest.mark.parametrize('assignment,replacement', [
    ('Make the Facts paragraph shorter.', 'Facts.'),
    ('Replace the Facts paragraph with: The amount is $5,000.', 'The amount is $5,000.'),
])
def test_requested_edit_without_evidence_still_publishes_without_reviewer(agent, monkeypatch, assignment, replacement):
    data = json.loads(agent.data_json)
    data['query'] = assignment
    agent.data_json = service.encode(data)
    db.session.commit()
    calls = []
    def provider(prompt, schema, **options):
        if schema is tasks.EditorWriteCheck:
            return schema(decision='not_forbidden', reason='User requested editing.', supporting_wording=assignment)
        calls.append(json.loads(prompt))
        assert schema is drafts.MarkdownRevisionResult
        assert calls[-1]['assignment'] == assignment
        assert calls[-1]['materials'] == []
        assert 'Original facts.' in calls[-1]['target_document']['markdown']
        assert 'Perform user-requested edits, including user-supplied factual changes' in options['sys_prompt']
        assert 'do not label them independently verified' in options['sys_prompt']
        return schema(mode='patch', title=None, markdown=None,
            replacements=[{'old_text':'Original facts.', 'new_text':replacement}], update_court_format=False)
    monkeypatch.setattr(base, 'chat_completion_parse', provider)
    operation(agent, monkeypatch, 'revise_draft', {'target':'working_draft',
        'revision_mode':'general_revision', 'instructions':assignment, 'material_ids':[]})
    result = last_result(agent)
    assert not result.get('error'), result
    operation(agent, monkeypatch, 'complete_task', {'candidate_id':'working_draft', 'response':'Edited as requested.'})
    assert not last_result(agent).get('error'), last_result(agent)
    assert replacement in state(agent)['draft']['markdown']
    assert len(calls) == 1
    assert tasks.steps(agent).filter_by(name='verify_draft').count() == 0
    assert tasks.steps(agent).filter_by(name='lookup_authorities').count() == 0


@pytest.mark.parametrize('tool', ['create_draft', 'revise_draft'])
@pytest.mark.parametrize('proposal', [
    'The source describes the middle event. Replace the inaccurate description accordingly.',
    'The source describes the last event. Correct the document to say last event.',
    'A prior generated answer calls the description wrong, but no supporting passage establishes that conclusion.',
])
def test_writer_receives_actual_source_separately_from_proposed_correction(agent, monkeypatch, tool, proposal):
    # The uploaded original is outside the editing target. Keep its actual wording
    # available even when the generated operation instruction misdescribes it.
    source_id = source_material(agent, monkeypatch)
    original = db.session.get(AgentArtifact, source_id).content_json
    source = json.loads(original)
    assert 'middle event' in json.dumps(source)
    calls = []
    def provider(prompt, schema, **options):
        if schema is tasks.EditorWriteCheck:
            return schema(decision='not_forbidden', reason='Editing permitted.', supporting_wording='')
        payload = json.loads(prompt)
        calls.append(payload)
        assert payload['assignment'] == json.loads(agent.data_json)['query']
        assert payload['instructions'] == proposal
        assert payload['materials'][0] == {**source, 'artifact_id':source_id}
        assert 'middle event' in json.dumps(payload['materials'][0])
        assert 'Check agent-proposed factual or source-based corrections against the exact original wording and supplied evidence' in options['sys_prompt']
        assert 'If an instruction misstates the original or exceeds the evidence' in options['sys_prompt']
        assert 'preserve or qualify the disputed point and complete other requested edits' in options['sys_prompt']
        assert drafts.CONDITION_STRENGTH_GUIDANCE in options['sys_prompt']
        assert 'Prior generated conclusions are not evidence' in options['sys_prompt']
        # Scripted result, not evidence that a live model would make this judgment.
        if tool == 'create_draft':
            assert payload['target_document'] is None
            return schema(title='Review', markdown='Description remains subject to the supplied evidence.', update_court_format=False)
        assert 'Original facts.' in payload['target_document']['markdown']
        return schema(mode='patch', title=None, markdown=None,
            replacements=[{'old_text':'Original facts.', 'new_text':'Facts remain subject to the supplied evidence.'}], update_court_format=False)
    monkeypatch.setattr(base, 'chat_completion_parse', provider)
    values = {'instructions':proposal, 'material_ids':[source_id]}
    if tool == 'revise_draft':
        values.update(target='working_draft', revision_mode='general_revision')
    operation(agent, monkeypatch, tool, values)
    result = last_result(agent)
    assert not result.get('error'), result
    assert len(calls) == 1
    assert db.session.get(AgentArtifact, source_id).content_json == original
