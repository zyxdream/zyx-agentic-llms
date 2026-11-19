"""Standalone web search uses normal saved agent results, without legal lookup."""
import json
from datetime import datetime, timezone

import pytest

from backend import agent_tasks
from backend.ai import agent_tools, base
from backend.database import db
from backend.models import AgentArtifact
from backend.util import document_drafts
from backend.domains.legal import authority_sources
from backend.domains.legal import authority_research
from backend.util import workspace_service as workflow
from tests.test_agent_controller import agent, operation, last_result, state, choose, tick, call
from tests.test_document_preparation import app


QUERY = 'Recent developments in the Example v. Agency lawsuit'
SUMMARY = 'Search synthesis: the court scheduled a hearing; this is not an opinion passage.'
URL = 'https://court.example/news/hearing'


def search_result(**changes):
    return {'provider': 'web', 'query': QUERY, 'error': None,
        'retrieved_at': '2026-11-10T20:00:00+00:00',
        'generated_summary': SUMMARY, 'summary_is_source_text': False,
        'results': [{'type': 'web', 'provider': 'web', 'title': 'Hearing scheduled',
            'url': URL, 'snippet': 'A hearing is scheduled.', 'snippet_is_source_text': False}],
        **changes}


@pytest.fixture
def no_documents(agent):
    data = json.loads(agent.data_json)
    data['query'] = QUERY
    data['document_snapshot']['files'] = []
    agent.data_json = workflow.encode(data)
    db.session.commit()
    return agent


def test_web_search_schema_needs_only_a_nonempty_query():
    tool = next(item for item in agent_tools.definitions() if item['name'] == 'web_search')
    assert tool['strict'] and tool['parameters']['additionalProperties'] is False
    assert tool['parameters']['required'] == ['query']
    assert set(tool['parameters']['properties']) == {'query'}
    assert agent_tools.arguments('web_search', {'query': QUERY}).query == QUERY
    for arguments in ({}, {'query': ''}, {'query': '  \n '}, {'query': None}, {'query': 123},
                      {'query': QUERY, 'research_id': 'not-needed'}):
        with pytest.raises(ValueError):
            agent_tools.arguments('web_search', arguments)


def test_standalone_search_saves_without_documents_or_legal_research(no_documents, app, monkeypatch):
    from backend.util import web_search
    seen = []
    before = state(no_documents)

    def search(query, **kwargs):
        seen.append((query, kwargs))
        kwargs['check_execution']()
        return search_result()

    def no_legal_call(*args, **kwargs):
        pytest.fail('Standalone web search invoked legal research or CourtListener')

    monkeypatch.setattr(web_search, 'search', search)
    monkeypatch.setattr(authority_research, 'lookup', no_legal_call)
    monkeypatch.setattr(authority_research, 'search', no_legal_call)
    monkeypatch.setattr(authority_sources, 'lookup_citations', no_legal_call)
    assert operation(no_documents, monkeypatch, 'web_search', {'query': QUERY}) == {'continued': True}
    result = last_result(no_documents)
    assert result['kind'] == 'web_search'
    assert result['generated_summary'] == SUMMARY and result['results'] == search_result()['results']
    assert result['retrieved_at'] == search_result()['retrieved_at']
    assert result['summary_is_source_text'] is False
    assert SUMMARY in result['text'] and URL in result['text']
    assert seen[0][0] == QUERY
    assert seen[0][1]['model'] == 'gpt-6-luna'
    assert seen[0][1]['reasoning_effort'] == agent_tools.Operations(
        no_documents, agent_tasks.steps(no_documents).filter_by(kind='tool').first(), None).role('search')['effort']
    assert AgentArtifact.query.filter_by(run_id=no_documents.id, kind='authority_research').count() == 0
    saved = db.session.get(AgentArtifact, result['artifact_id'])
    assert saved.kind == 'web_search'
    assert json.loads(saved.content_json)['results'] == result['results']
    assert state(no_documents) == before

    # A new request to the artifact API and the ordinary reader both reload SQL,
    # rather than depending on an in-memory provider response.
    db.session.expire_all()
    response = app.test_client().get(
        f'/api/workspace/runs/{no_documents.id}/artifacts/{result["artifact_id"]}')
    assert response.status_code == 200
    assert response.json['kind'] == 'web_search'
    operation(no_documents, monkeypatch, 'read_sources', {
        'artifact_id': result['artifact_id'], 'ranges': [], 'start': 0, 'end': None})
    assert json.loads(last_result(no_documents)['text']) == response.json['content']


def test_saved_search_reaches_analysis_and_drafting_as_search_synthesis(agent, monkeypatch):
    from backend.util import web_search
    monkeypatch.setattr(web_search, 'search', lambda *args, **kwargs: search_result())
    operation(agent, monkeypatch, 'web_search', {'query': QUERY})
    search_id = last_result(agent)['artifact_id']
    analysis_materials, draft_materials = [], []

    def analyze(prompt, **kwargs):
        analysis_materials.extend(json.loads(prompt.split(
            'Materials (source evidence and prior generated work are labeled):\n', 1)[1]))
        return 'A search-based update; the underlying opinion was not read.'

    def draft(**kwargs):
        draft_materials.extend(kwargs['materials'])
        return document_drafts.MarkdownDraftResult(title='Update', markdown='# Update\n\nSearch-based update.',
                                                   update_court_format=False)

    monkeypatch.setattr(base, 'chat_completion', analyze)
    operation(agent, monkeypatch, 'analyze_material', {
        'instructions': 'Explain these search findings and their limits.', 'material_ids': [search_id]})
    monkeypatch.setattr(document_drafts, 'write_document', draft)
    operation(agent, monkeypatch, 'create_draft', {
        'instructions': 'Draft a short update from this search synthesis.', 'material_ids': [search_id]})
    for materials in (analysis_materials, draft_materials):
        assert len(materials) == 1 and materials[0]['artifact_id'] == search_id
        assert materials[0]['kind'] == 'web_search'
        assert materials[0]['summary_is_source_text'] is False
        assert materials[0]['results'][0]['url'] == URL


def test_search_provider_failure_is_saved_as_an_observation(agent, monkeypatch):
    from backend.util import web_search
    monkeypatch.setattr(web_search, 'search', lambda *args, **kwargs: search_result(
        error='Web search unavailable (RuntimeError).', results=[], generated_summary=''))
    assert operation(agent, monkeypatch, 'web_search', {'query': QUERY}) == {'continued': True}
    result = last_result(agent)
    assert result['error'] is True and 'RuntimeError' in result['message']
    saved = json.loads(db.session.get(AgentArtifact, result['artifact_id']).content_json)
    assert saved['error'] == 'Web search unavailable (RuntimeError).'
    assert saved['results'] == [] and 'RuntimeError' in saved['text']
    assert agent.status == 'running'


def test_empty_search_results_remain_explicit_without_starting_legal_research(agent, monkeypatch):
    from backend.util import web_search
    monkeypatch.setattr(web_search, 'search', lambda *args, **kwargs: search_result(
        results=[], generated_summary='', limitations=['No observed source URLs were returned.']))
    operation(agent, monkeypatch, 'web_search', {'query': QUERY})
    result = last_result(agent)
    assert result['results'] == []
    assert 'No observed source URLs were returned.' in result['text']
    assert AgentArtifact.query.filter_by(run_id=agent.id, kind='authority_research').count() == 0


def test_cancelled_search_cannot_save_a_late_result(agent, monkeypatch):
    from backend.util import web_search
    before = state(agent)

    def cancel_during_provider(*args, **kwargs):
        workflow.stop(agent, cancelled=True)
        kwargs['check_execution']()
        pytest.fail('Cancellation did not propagate')

    monkeypatch.setattr(web_search, 'search', cancel_during_provider)
    choose(monkeypatch, 'web_search', {'query': QUERY})
    assert tick(agent) == {'continued': True}
    assert tick(agent) == {'stopped': True}
    assert agent.status == 'cancelled'
    assert AgentArtifact.query.filter_by(run_id=agent.id).count() == 0
    assert state(agent) == before


def test_controller_context_supplies_current_utc_date_and_available_search(no_documents, monkeypatch):
    from backend.util import web_search
    monkeypatch.setattr(web_search, 'search', lambda *args, **kwargs: search_result())
    before = datetime.now(timezone.utc).date().isoformat()
    operation(no_documents, monkeypatch, 'web_search', {'query': QUERY})
    after = datetime.now(timezone.utc).date().isoformat()
    initial = json.loads(json.loads(no_documents.payload_json)['agent_context']['input'][1]['content'])
    assert initial['current_date'] in {before, after}
    assert initial['documents'] == []
    guidance = ' '.join(agent_tasks.system_instructions().lower().split())
    assert 'web_search' in guidance and 'news' in guidance and 'case developments' in guidance
    assert 'citation' in guidance and 'search_legal_sources' in guidance


def test_legal_search_findings_survive_reload_and_reach_the_next_controller_step(agent, app, monkeypatch):
    monkeypatch.setattr(authority_sources, 'lookup_citations', lambda *args, **kwargs: ([], None))
    monkeypatch.setattr(authority_research, '_discover_mentions', lambda *args, **kwargs: [])
    monkeypatch.setattr(authority_sources, 'fetch_source',
                        lambda *args, **kwargs: pytest.fail('Search fetched a source passage'))
    operation(agent, monkeypatch, 'lookup_authorities', {
        'targets': [{'file_id': None, 'draft_id': None, 'text': QUERY, 'start': None, 'end': None}],
        'mode': 'overview', 'purpose': QUERY, 'research_id': None})
    initial_id = last_result(agent)['research_id']
    initial_bytes = db.session.get(AgentArtifact, initial_id).content_json
    initial_source_ids = {item.id for item in AgentArtifact.query.filter_by(
        run_id=agent.id, kind='authority_source').all()}
    sources = [{'type': 'web', 'provider': 'web', 'url': f'https://court.example/research/{index}',
                'title': f'Research page {index}', 'snippet': '', 'snippet_is_source_text': False}
               for index in range(1, 14)]
    cited_url = sources[-1]['url']
    summary = f'Generated research findings: see the hearing announcement at {cited_url}.'
    citation = {'url': cited_url, 'title': 'Hearing announcement',
                'start_index': summary.index(cited_url), 'end_index': len(summary) - 1}
    provider_calls = []

    def search(query, provider, **kwargs):
        provider_calls.append((query, provider))
        kwargs['check_execution']()
        return search_result(generated_summary=summary, results=sources, citations=[citation],
                             limitations=['Underlying page text has not been read.'])

    monkeypatch.setattr(authority_sources, 'search_sources', search)
    operation(agent, monkeypatch, 'search_legal_sources', {
        'research_id': initial_id, 'authority_ids': [], 'query': QUERY, 'provider': 'web'})
    result = last_result(agent)
    assert result['generated_summary'] == summary
    assert result['summary_is_source_text'] is False
    assert result['source_count'] == 13 and result['sources_truncated']
    assert len(result['sources']) == 5 and cited_url not in [source['url'] for source in result['sources']]
    assert len(result['citations']) == 1
    cited_source_id = result['citations'][0]['source_id']
    assert result['citations'] == [{**citation, 'source_id': cited_source_id}]
    assert 'passage_artifact_id' not in result
    assert {item.id for item in AgentArtifact.query.filter_by(
        run_id=agent.id, kind='authority_source').all()} == initial_source_ids

    # Reload through the ordinary owned-artifact API, independent of the provider response.
    db.session.expire_all()
    response = app.test_client().get(f'/api/workspace/runs/{agent.id}/artifacts/{result["artifact_id"]}')
    assert response.status_code == 200
    saved = response.json['content']
    query = saved['queries'][-1]
    for field in ('generated_summary', 'summary_is_source_text', 'citations', 'retrieved_at', 'limitations'):
        assert query[field] == result[field]
    cited_source = next(source for source in saved['sources'] if source['id'] == cited_source_id)
    assert cited_source['url'] == cited_url and cited_source['passage_artifact_ids'] == []
    assert saved['authorities'] == []
    assert db.session.get(AgentArtifact, initial_id).content_json == initial_bytes

    requests = []

    def controller(request):
        requests.append(request)
        return call('list_documents', {})

    monkeypatch.setattr(base, 'responses_completion', controller)
    assert tick(agent) == {'continued': True}
    assert len(requests) == 1
    history = [json.loads(item['output']) for item in requests[0]['input']
               if item.get('type') == 'function_call_output']
    assert history[-1] == result
    assert history[-1]['generated_summary'] == summary
    assert history[-1]['summary_is_source_text'] is False
    assert history[-1]['citations'][0]['source_id'] == cited_source_id
    assert provider_calls == [(QUERY, 'web')]
    assert agent_tasks.steps(agent).filter_by(name='read_authority').count() == 0
