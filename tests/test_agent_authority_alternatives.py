"""Alternative authority phrases preserve strict inputs and controller evidence."""
import copy
import json

import pytest

from backend import agent_tasks
from backend.ai import agent_tools, base
from backend.database import db
from backend.domains.legal import authority_research as research
from backend.domains.legal import authority_sources as sources
from backend.models import AgentArtifact, AgentStep
from tests.test_agent_controller import agent, call, last_result, operation, tick
from tests.test_authority_research import add_web_source, ops, record
from tests.test_document_preparation import app


def arguments(**updates):
    return {'research_id': 'saved-research', 'source_id': 'saved-source',
            'opinion_id': None, 'query': 'Original phrase', 'alternative_queries': [],
            'start': 0, 'resolve_page_metadata': False, **updates}


def test_read_schema_requires_alternatives_but_saved_calls_remain_compatible():
    definition = next(item for item in agent_tools.definitions() if item['name'] == 'read_authority')
    schema = definition['parameters']
    assert definition['strict'] is True and schema['additionalProperties'] is False
    assert set(schema['required']) == set(schema['properties'])
    alternatives = schema['properties']['alternative_queries']
    assert alternatives['type'] == 'array' and alternatives['items']['type'] == 'string'
    assert 'default' not in alternatives

    legacy = arguments()
    legacy.pop('alternative_queries')
    legacy.pop('resolve_page_metadata')
    original = copy.deepcopy(legacy)
    with pytest.raises(ValueError):
        agent_tools.ReadAuthority.model_validate(legacy)
    parsed = agent_tools.arguments('read_authority', legacy)
    assert parsed.alternative_queries == []
    assert legacy == original and 'alternative_queries' not in legacy
    assert parsed.model_dump() == {**original, 'alternative_queries': [], 'resolve_page_metadata': False}

    explicit = arguments(alternative_queries=['  HOURLY rate EMPLOYEE\n', '§ 778.110'])
    original = copy.deepcopy(explicit)
    parsed = agent_tools.arguments('read_authority', explicit)
    assert parsed.alternative_queries == explicit['alternative_queries']
    assert explicit == original
    assert agent_tools.arguments('read_authority', arguments(query=None)).alternative_queries == []


@pytest.mark.parametrize('invalid', [
    None, '', 'a phrase', {}, 1, True,
    [None], [1], [False], [{}], [[]],
    [''], [' \t\r\n'], ['\u00ad'], [' \t\u00ad\n'], ['valid', '\u00ad'],
])
def test_alternative_queries_reject_invalid_types_and_blank_phrases(invalid):
    raw = arguments(alternative_queries=invalid)
    original = copy.deepcopy(raw)
    with pytest.raises(ValueError):
        agent_tools.ReadAuthority.model_validate(raw)
    with pytest.raises(ValueError):
        agent_tools.arguments('read_authority', raw)
    assert raw == original


def test_alternatives_require_a_primary_query_without_mutating_arguments():
    raw = arguments(query=None, alternative_queries=['Alternative phrase'])
    original = copy.deepcopy(raw)
    with pytest.raises(ValueError):
        agent_tools.ReadAuthority.model_validate(raw)
    with pytest.raises(ValueError):
        agent_tools.arguments('read_authority', raw)
    assert raw == original


@pytest.mark.parametrize('body,query,alternatives,matched,original_found,source_span', [
    ('Alternative phrase first. Original phrase later.', 'Original phrase',
     ['Alternative phrase'], 'Original phrase', True, 'Original phrase'),
    ('Second alternative first. First alternative later.', 'Absent original',
     ['First alternative', 'Second alternative'], 'First alternative', False, 'First alternative'),
    ('Compen\u00adsation and hourly\nrate employee.', 'Absent original',
     ['Not here', '  COMPENSATION\n', 'hourly rate employee'], '  COMPENSATION\n', False, 'Compen\u00adsation'),
])
def test_primary_then_submitted_alternatives_preserve_exact_queries_and_positions(
        ops, monkeypatch, body, query, alternatives, matched, original_found, source_span):
    rid, sid = add_web_source(ops, monkeypatch)
    fetches = []

    def fetch(*args, **kwargs):
        fetches.append(1)
        return {'status': 'read', 'text': body}

    monkeypatch.setattr(sources, 'fetch_source', fetch)
    raw = arguments(research_id=rid, source_id=sid, query=query, alternative_queries=alternatives)
    original = copy.deepcopy(raw)
    result = research.read(ops, agent_tools.arguments('read_authority', raw))
    assert result['query'] == query
    assert result['match_found'] is True
    assert result['original_query_found'] is original_found
    assert result['matched_query'] == matched
    assert (result['match_start'], result['match_end']) == (body.index(source_span), body.index(source_span) + len(source_span))
    assert result['text'] == body[result['start']:result['end']]
    saved = record(result['passage_artifact_id'])
    for field in ('query', 'original_query_found', 'matched_query', 'match_start', 'match_end'):
        assert saved[field] == result[field]
    assert record(result['research_id'])['sources'][0]['passage_artifact_ids'] == [result['passage_artifact_id']]
    assert raw == original and fetches == [1]


def test_no_submitted_phrase_matches_returns_false_and_no_passage(ops, monkeypatch):
    rid, sid = add_web_source(ops, monkeypatch)
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: {'status': 'read', 'text': 'Readable source.'})
    result = research.read(ops, agent_tools.arguments('read_authority', arguments(
        research_id=rid, source_id=sid, query='Absent original', alternative_queries=['Also absent'])))
    assert result['query'] == 'Absent original'
    assert result['match_found'] is False and result['original_query_found'] is False
    assert result['matched_query'] is None
    assert result['match_start'] is None and result['match_end'] is None
    assert 'passage_artifact_id' not in result


@pytest.mark.parametrize('outcome', ['plain-read', 'metadata', 'error', 'end-of-source'])
def test_reads_without_a_search_have_null_query_match_metadata(ops, monkeypatch, outcome):
    rid, sid = add_web_source(ops, monkeypatch)
    fetched = {'status': 'read', 'text': 'Readable source.'}
    if outcome == 'metadata':
        fetched = {'status': 'metadata', 'opinions': [{'id': 17, 'type': 'lead-opinion'}]}
    elif outcome == 'error':
        fetched = {'status': 'unavailable', 'error': 'Source is unavailable.'}
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: fetched)
    query = None if outcome == 'plain-read' else 'Source phrase'
    raw = arguments(research_id=rid, source_id=sid, query=query,
                    start=len(fetched['text']) if outcome == 'end-of-source' else 0,
                    alternative_queries=[] if query is None else ['Alternative phrase'])
    result = research.read(ops, agent_tools.arguments('read_authority', raw))
    assert result['original_query_found'] is None and result['matched_query'] is None
    assert result['match_start'] is None and result['match_end'] is None
    if outcome == 'plain-read':
        saved = record(result['passage_artifact_id'])
        assert saved['query'] is None
        assert saved['original_query_found'] is None and saved['matched_query'] is None
    else:
        assert 'passage_artifact_id' not in result
    if outcome == 'error':
        assert result['error'] is True
    if outcome == 'end-of-source':
        assert result['end_of_source'] is True


def test_alternative_match_survives_controller_handoff_reload_and_occurrence_continuation(agent, app, monkeypatch):
    original_query = '§ 778.110 Hourly rate employee'
    matched_query = '  HOURLY rate EMPLOYEE\n'
    first_text, second_text = 'Hourly\nrate employee', 'HOURLY\u00a0rate EMPLOYEE'
    body = ('Contents\n' + first_text + '\n' + 'Context. ' * 20 + '\n§ 778.110\nApplicability\n'
            + second_text + '\n' + 'Remaining explanation. ' * 1000)
    url = 'https://official.example/part778.pdf'
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([], None))
    monkeypatch.setattr(research, '_discover_mentions', lambda *a, **kw: [])
    operation(agent, monkeypatch, 'lookup_authorities', {
        'targets': [{'file_id': None, 'draft_id': None, 'text': 'Find the hourly rate rule.',
                     'start': None, 'end': None}],
        'mode': 'overview', 'purpose': 'Read the relevant provision.', 'research_id': None})
    initial_id = last_result(agent)['research_id']
    searches, fetches = [], []

    def search(*args, **kwargs):
        searches.append(1)
        return {'results': [{'type': 'web', 'provider': 'web', 'url': url, 'title': 'Part 778'}],
                'error': None}

    def fetch(*args, **kwargs):
        fetches.append(1)
        return {'status': 'read', 'text': body, 'url': url, 'sha256': 'original-source-hash',
                'blocks': [{'start': 0, 'end': len(body), 'page': 1}]}

    monkeypatch.setattr(sources, 'search_sources', search)
    monkeypatch.setattr(sources, 'fetch_source', fetch)
    operation(agent, monkeypatch, 'search_legal_sources', {
        'research_id': initial_id, 'authority_ids': [], 'query': original_query, 'provider': 'web'})
    found = last_result(agent)
    source_id = found['sources'][0]['id']
    submitted = arguments(research_id=found['research_id'], source_id=source_id,
        query=original_query, alternative_queries=['Still absent', matched_query, 'Remaining explanation'])
    operation(agent, monkeypatch, 'read_authority', submitted)
    first = last_result(agent)
    assert first['query'] == original_query and first['match_found'] is True
    assert first['original_query_found'] is False and first['matched_query'] == matched_query
    assert (first['match_start'], first['match_end']) == (
        body.index(first_text), body.index(first_text) + len(first_text))
    second_position = body.index(second_text)
    assert first['match_end'] < second_position < first['next_start']
    step = agent_tasks.steps(agent).filter_by(name='read_authority').order_by(AgentStep.sequence.desc()).first()
    assert json.loads(step.arguments_json) == {'raw': json.dumps(submitted)}

    db.session.expire_all()
    response = app.test_client().get(
        f'/api/workspace/runs/{agent.id}/artifacts/{first["passage_artifact_id"]}')
    assert response.status_code == 200
    saved = response.json['content']
    for field in ('query', 'original_query_found', 'matched_query', 'match_start', 'match_end',
                  'start', 'end', 'next_start', 'locations'):
        assert saved[field] == first[field]
    assert saved['text'] == body[first['start']:first['end']]
    assert saved['source_sha256'] == 'original-source-hash'

    requests = []

    def controller(request):
        requests.append(request)
        outputs = [json.loads(item['output']) for item in request['input']
                   if item.get('type') == 'function_call_output']
        previous = outputs[-1]
        assert previous == (first if len(requests) == 1 else second)
        return call('read_authority', arguments(research_id=previous['research_id'], source_id=source_id,
            query=previous['matched_query'] if len(requests) == 1 else None,
            alternative_queries=[], start=previous['match_end'] if len(requests) == 1 else previous['next_start']))

    monkeypatch.setattr(base, 'responses_completion', controller)
    assert tick(agent) == {'continued': True}
    assert tick(agent) == {'continued': True}
    second = last_result(agent)
    assert second['query'] == matched_query and second['matched_query'] == matched_query
    assert second['original_query_found'] is True and second['match_found'] is True
    assert (second['match_start'], second['match_end']) == (second_position, second_position + len(second_text))
    assert second['text'] == body[second['start']:second['end']]
    assert second['source_artifact_id'] == first['source_artifact_id']
    assert tick(agent) == {'continued': True}
    assert tick(agent) == {'continued': True}
    page = last_result(agent)
    assert page['start'] == second['next_start'] and page['query'] is None
    assert page['original_query_found'] is None and page['matched_query'] is None
    assert page['match_start'] is None and page['match_end'] is None
    assert page['text'] == body[page['start']:page['end']]
    assert page['source_artifact_id'] == first['source_artifact_id']
    assert searches == [1] and fetches == [1]
    cache = json.loads(db.session.get(AgentArtifact, first['source_artifact_id']).content_json)
    assert cache['text'] == body



def test_all_alternative_searches_respect_the_supplied_start(ops, monkeypatch):
    rid, sid = add_web_source(ops, monkeypatch)
    body = 'Original phrase. First alternative. Next alternative.'
    start = body.index('Next alternative')
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: {'status': 'read', 'text': body})
    result = research.read(ops, agent_tools.arguments('read_authority', arguments(
        research_id=rid, source_id=sid, query='Original phrase',
        alternative_queries=['First alternative', 'Next alternative'], start=start)))
    assert result['original_query_found'] is False
    assert result['matched_query'] == 'Next alternative'
    assert result['match_start'] == start
    assert result['match_end'] == start + len('Next alternative')


def test_controller_guidance_explains_recovery_and_both_continuation_options():
    from backend.domains.legal.instructions import SOURCE_NAVIGATION_GUIDANCE
    assert SOURCE_NAVIGATION_GUIDANCE in agent_tasks.system_instructions()
    guidance = ' '.join(SOURCE_NAVIGATION_GUIDANCE.split())
    for term in ('shorter', 'surrounding text', 'sources already returned', 'broad research',
                 'not a mandatory tool sequence', 'original_query_found', 'matched_query',
                 'query=matched_query', 'alternative_queries=[]', 'start=match_end',
                 'query=null', 'start=next_start'):
        assert term in guidance
    description = agent_tools.TOOL_TYPES['read_authority'][1]
    assert 'match_found=True can mean an alternative matched' in description
    assert 'original_query_found' in description and 'one passage' in description
