"""Agent integration: optional research, saved evidence, and publication boundaries."""
import json

import pytest

from backend.workspace import state as everything
from backend import agent_tasks
from backend.workspace.state import finalize_llm_response_for_display as real_finalizer
from backend.ai import agent_tools, base
from backend.database import db
from backend.models import AgentArtifact
from backend.util import document_drafts, workspace_service
from tests.test_agent_controller import agent, operation, last_result, state
from tests.test_document_preparation import app
from tests.test_agent_uploaded_result_contract import uploaded_revision


def test_publication_formats_saved_links_without_automatic_lookup(agent, monkeypatch):
    # Use the actual finalizer, rather than the controller fixture's boundary
    # double. A legal citation alone must not trigger a hidden network pass.
    monkeypatch.setattr(everything, 'finalize_llm_response_for_display', real_finalizer)
    monkeypatch.setattr(everything, 'verify_and_annotate_response_citations',
                        lambda *a, **kw: pytest.fail('Publication performed citation lookup'))
    response = '[Example](https://example.com/opinion) — citation 123 F.3d 456 remains unresolved.'
    assert operation(agent, monkeypatch, 'complete_task',
                     {'response': response, 'candidate_id': None}) == {'completed': True}
    assert state(agent)['chat_history'][-1][1] == response


def test_uploaded_revision_bridge_does_not_perform_legacy_lookup(agent, monkeypatch):
    monkeypatch.setattr(everything, 'finalize_llm_response_for_display', real_finalizer)
    monkeypatch.setattr(everything, 'verify_and_annotate_response_citations',
                        lambda *a, **kw: pytest.fail('Revision performed hidden citation lookup'))
    result = uploaded_revision(agent, monkeypatch, {
        'error': False, 'blocked': False, 'message': 'Revised reference to 123 F.3d 456.',
        'draft': {'title': 'Candidate', 'sections': [
            {'section_id': '1', 'section_name': 'Facts', 'section_content': 'Targeted correction.'}]},
        'operations': [],
    })
    assert result['kind'] == 'draft'


def test_new_tool_schemas_are_explicit_and_strict():
    definitions = {item['name']: item for item in agent_tools.definitions()}
    for name in ('lookup_authorities', 'search_legal_sources', 'read_authority'):
        tool = definitions[name]
        assert tool['strict'] and tool['parameters']['additionalProperties'] is False
        assert set(tool['parameters']['required']) == set(tool['parameters']['properties'])
    mode = definitions['lookup_authorities']['parameters']['properties']['mode']
    assert mode['enum'] == ['overview', 'audit']
    assert 'default' not in mode
    with pytest.raises(ValueError):
        agent_tools.arguments('search_legal_sources', {'research_id': 'r', 'authority_ids': [],
            'query': 'section 1', 'provider': 'invented'})


def test_lookup_mode_is_explicit_with_a_documented_old_call_default():
    legacy = {'targets': [], 'purpose': 'Continue selected research.', 'research_id': 'saved-research'}
    assert agent_tools.arguments('lookup_authorities', legacy).mode == 'overview'
    assert 'mode' not in legacy
    # New model-facing arguments are required; only the saved-call input boundary
    # inserts the documented compatibility value.
    with pytest.raises(ValueError):
        agent_tools.LookupAuthorities.model_validate(legacy)
    for mode in ('overview', 'audit'):
        assert agent_tools.arguments('lookup_authorities', {**legacy, 'mode': mode}).mode == mode
    for invalid in ('automatic', '', None):
        with pytest.raises(ValueError):
            agent_tools.arguments('lookup_authorities', {**legacy, 'mode': invalid})


@pytest.mark.parametrize('mode', ['overview', 'audit'])
def test_controller_dispatches_explicit_lookup_mode_and_unchanged_source_range(agent, monkeypatch, mode):
    from backend.domains.legal import authority_research
    uid = json.loads(agent.data_json)['document_snapshot']['files'][0]['file_id']
    requested = {'file_id': uid, 'draft_id': None, 'text': None, 'start': 3, 'end': 19}
    seen = []
    def lookup(ops, args):
        seen.append(args.model_dump())
        return {'mode': args.mode, 'targets': [item.model_dump() for item in args.targets],
            'coverage': {'complete': True}}
    monkeypatch.setattr(authority_research, 'lookup', lookup)
    assert operation(agent, monkeypatch, 'lookup_authorities', {
        'targets': [requested], 'mode': mode, 'purpose': 'Identify the selected authorities.',
        'research_id': None}) == {'continued': True}
    assert seen == [{'targets': [requested], 'mode': mode,
        'purpose': 'Identify the selected authorities.', 'research_id': None}]
    assert last_result(agent)['mode'] == mode
    assert last_result(agent)['targets'] == [requested]


def test_controller_describes_scope_modes_without_automatic_verification():
    assert 'normally choose overview' in agent_tasks.system_instructions()
    assert 'choose audit when the user wants every citation occurrence' in agent_tasks.system_instructions()
    assert 'lookup does not silently\nsample or select passages' in agent_tasks.system_instructions()
    assert 'Discovery mode does not automatically add source reading' in agent_tasks.system_instructions()


def test_writer_gets_actual_passages_and_keeps_research_provenance(agent, monkeypatch):
    # These are source artifacts, not model-generated holdings or case metadata.
    step = agent_tasks.steps(agent).filter_by(kind='tool').first()
    if step is None:
        operation(agent, monkeypatch, 'list_documents', {})
        step = agent_tasks.steps(agent).filter_by(kind='tool').first()
    research_id, passage_id, cache_id = 'test-research-record', 'test-opinion-passage', 'test-opinion-cache'
    research = {'kind': 'authority_research', 'purpose': 'Identify the authority.',
        'targets': [], 'authorities': [], 'sources': [], 'references': [], 'coverage': {}}
    passage = {'kind': 'authority_source', 'role': 'passage', 'research_id': research_id,
        'source_artifact_id': cache_id,
        'text': 'Actual retrieved opinion language with surrounding reasoning.',
        'url': 'https://example.com/opinion', 'opinion_id': 17, 'opinion_type': 'lead-opinion',
        'viewer_url': 'http://localhost:3000/CaseViewer/test-chat/123%20U.S.%20456&51',
        'references': []}
    cache = {'kind': 'authority_source', 'role': 'source_cache', 'text': passage['text']}
    for uid, value in ((research_id, research), (passage_id, passage), (cache_id, cache)):
        db.session.add(AgentArtifact(id=uid, run_id=agent.id, step_id=step.id,
            kind=value['kind'], content_json=json.dumps(value), created_at=workspace_service.now()))
    db.session.commit()
    seen = []
    def writer(**kwargs):
        seen.extend(kwargs['materials'])
        return document_drafts.MarkdownDraftResult(title='Memo', markdown='# Memo\n\nSource-backed analysis.',
                                                   update_court_format=False)
    monkeypatch.setattr(document_drafts, 'write_document', writer)
    operation(agent, monkeypatch, 'create_draft', {'instructions': 'Use the supplied opinion passage.',
                                                'material_ids': [research_id, passage_id]})
    assert seen[1]['text'] == passage['text']
    assert seen[1]['artifact_id'] == passage_id
    assert seen[1]['source_artifact_id'] == cache_id
    assert seen[1]['opinion_id'] == 17
    assert seen[1]['viewer_url'] == passage['viewer_url']
    saved = json.loads(db.session.get(AgentArtifact, last_result(agent)['artifact_id']).content_json)['draft']
    assert saved['authority_research_ids'] == [research_id]
    assert saved['authority_source_ids'] == [passage_id]


def test_writer_source_reading_principle_is_in_the_existing_call(monkeypatch):
    seen = {}
    def parse(prompt, response_model, **kwargs):
        seen.update(kwargs)
        return response_model(title='Memo', markdown='# Memo', update_court_format=False)
    monkeypatch.setattr(base, 'chat_completion_parse', parse)
    document_drafts.write_document(assignment='Draft a memo', instructions='Discuss the holding',
        conversation={}, materials=[], target=None, answers=[], model='gpt-6.1-sol', reasoning_effort='medium')
    assert 'relevant retrieved source passages' in seen['sys_prompt']
    assert 'metadata, search previews' in seen['sys_prompt']
