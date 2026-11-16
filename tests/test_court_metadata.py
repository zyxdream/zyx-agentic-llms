"""Provider court evidence survives lookup, reading and operation handoffs."""
import copy
import json

import pytest

from backend.ai import agent_tools, base
from backend.database import db
from backend.models import AgentArtifact
from backend.domains.legal import authority_research as research, authority_sources as sources
from backend.domains.legal.instructions import COURT_ATTRIBUTION_GUIDANCE
from backend.util import document_drafts as drafts
from tests.test_agent_controller import agent, call, last_result, operation, tick
from tests.test_authority_research import ops, lookup_args, read_args, record
from tests.test_document_preparation import app


CITATION = '576 F. Supp. 627'
HEADER = 'Ginter v. Northwestern Mutual Life Insurance\nUnited States District Court, E.D. Kentucky, Covington Division.\nJan. 16, 1984.'
LOOKUP = sources.lookup_citations
FETCH = sources.fetch_source


def lookup_response(*, header=True, court=None):
    cluster = {'id': 2593291, 'case_name': 'Ginter v. Northwestern Mutual Life Insurance',
        'court': court, 'date_filed': '1984-01-16',
        'absolute_url': '/opinion/2593291/ginter/',
        'citations': [{'volume': 576, 'reporter': 'F. Supp.', 'page': 627}],
        'sub_opinions': [f'{sources.API_BASE}opinions/2593291/'],
        'docket': f'{sources.API_BASE}dockets/243865/'}
    if header:
        cluster['headmatter'] = '<p>Ginter v. Northwestern Mutual Life Insurance</p><p>United States District Court, E.D. Kentucky, Covington Division.</p><p>Jan. 16, 1984.</p>'
    return [{'citation': CITATION, 'normalized_citations': [CITATION], 'status': 200,
        'error_message': '', 'clusters': [cluster]}]


def assert_header(value):
    assert value['court'] is None
    assert value['case_header'] == HEADER
    assert value['case_header_source'] == {
        'url': f'{sources.API_BASE}citation-lookup/', 'field': 'headmatter'}
    assert value['cluster_id'] == 2593291 and value['citations'] == [CITATION]


def test_ginter_lookup_returns_header_to_controller_and_discovery_without_extra_requests(ops, monkeypatch):
    raw = lookup_response()
    unchanged = copy.deepcopy(raw)
    requests, discovery = [], []
    def api(method, path, **kwargs):
        requests.append((method, path))
        assert (method, path) == ('POST', 'citation-lookup/')
        return raw, 'lookup-digest'
    monkeypatch.setattr(sources, '_api_json', api)
    monkeypatch.setattr(sources, 'lookup_citations', LOOKUP)
    monkeypatch.setattr(research, '_discover_mentions', lambda *a, **kw: discovery.append(kw) or [])
    result = research.lookup(ops, lookup_args('Ginter, '+CITATION+' (D. Colo. 1984).'))
    source = result['sources'][0]
    assert_header(source)
    assert source['source_id'] == source['id']
    assert source['research_id'] == result['research_id']
    assert_header(discovery[0]['courtlistener_findings']['results'][0]['candidates'][0])
    saved = record(result['research_id'])
    assert_header(saved['sources'][0])
    assert_header(saved['authorities'][0]['candidates'][0])
    assert saved['authorities'][0]['checking'] == {
        'identity': 'not_assessed', 'opinion_read': False, 'proposition': 'not_assessed'}
    assert requests == [('POST', 'citation-lookup/')] and raw == unchanged


def test_header_stays_outside_opinion_text_and_match_positions_with_one_body_fetch(ops, monkeypatch):
    body = 'OPINION\nBERTELSMAN, District Judge.\n' + 'a' * 1800 + ' known standard ' + 'b' * 400 + 'known standard' + 'c' * 15000
    requests = []
    def api(method, path, **kwargs):
        requests.append((method, path))
        if path == 'citation-lookup/':
            return lookup_response(), 'lookup-digest'
        if path == 'opinions/':
            return {'results': [{'id': 2593291, 'type': '010combined'}], 'next': None}, 'metadata-digest'
        assert path == 'opinions/2593291/'
        return {'id': 2593291, 'cluster': f'{sources.API_BASE}clusters/2593291/',
                'type': '010combined', 'plain_text': body}, 'body-digest'
    monkeypatch.setattr(sources, '_api_json', api)
    monkeypatch.setattr(sources, 'lookup_citations', LOOKUP)
    monkeypatch.setattr(sources, 'fetch_source', FETCH)
    result = research.lookup(ops, lookup_args(CITATION))
    sid = result['sources'][0]['id']
    first = research.read(ops, read_args(result['research_id'], sid, opinion_id=2593291, query='known standard'))
    assert_header(first)
    assert first['match_start'] == body.index('known standard')
    assert first['match_end'] == first['match_start'] + len('known standard')
    assert first['text'] == body[first['start']:first['end']]
    assert 'Kentucky' not in first['text']
    original_passage = db.session.get(AgentArtifact, first['passage_artifact_id']).content_json
    db.session.expire_all()
    second = research.read(ops, read_args(first['research_id'], sid, opinion_id=2593291,
        query='known standard', start=first['match_end']))
    assert second['match_start'] == body.index('known standard', first['match_end'])
    chunk = research.read(ops, read_args(second['research_id'], sid,
        opinion_id=2593291, start=first['next_start']))
    assert chunk['text'] == body[first['next_start']:]
    assert chunk['next_start'] is None
    assert record(first['source_artifact_id'])['text'] == body
    assert_header(record(first['source_artifact_id']))
    assert_header(record(first['passage_artifact_id']))
    assert db.session.get(AgentArtifact, first['passage_artifact_id']).content_json == original_passage
    assert requests == [('POST', 'citation-lookup/'), ('GET', 'opinions/'), ('GET', 'opinions/2593291/')]


def test_opinion_choices_misses_and_unavailable_reads_keep_identifying_information(ops, monkeypatch):
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: (lookup_response(), None))
    result = research.lookup(ops, lookup_args(CITATION))
    sid = result['sources'][0]['id']
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: {'status': 'metadata',
        'opinions': [{'id': 1, 'type': 'lead'}, {'id': 2, 'type': 'dissent'}]})
    choices = research.read(ops, read_args(result['research_id'], sid))
    assert_header(choices)
    assert len(choices['opinions']) == 2 and 'passage_artifact_id' not in choices
    assert record(choices['research_id'])['authorities'][0]['checking']['opinion_read'] is False
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: {'status': 'read',
        'opinion_id': 1, 'text': 'Actual body only.', 'blocks': []})
    miss = research.read(ops, read_args(choices['research_id'], sid, opinion_id=1, query='absent phrase'))
    assert_header(miss)
    assert miss['match_found'] is False and 'passage_artifact_id' not in miss
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: {'status': 'unavailable',
        'error': 'External source unavailable.'})
    unavailable = research.read(ops, read_args(choices['research_id'], sid, opinion_id=2))
    assert_header(unavailable)
    assert unavailable['error'] and 'passage_artifact_id' not in unavailable


def test_later_lookup_enriches_legacy_source_without_rewriting_cached_body_or_conflicting_facts(ops, monkeypatch):
    response = lookup_response(header=False)
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: (response, None))
    initial = research.lookup(ops, lookup_args(CITATION))
    sid = initial['sources'][0]['id']
    assert 'case_header' not in initial['sources'][0]
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: {
        'status': 'read', 'opinion_id': 2593291, 'text': 'Historical body.', 'blocks': []})
    old_read = research.read(ops, read_args(initial['research_id'], sid, opinion_id=2593291))
    unchanged = {uid: db.session.get(AgentArtifact, uid).content_json for uid in
                 [initial['research_id'], old_read['research_id'], old_read['source_artifact_id'], old_read['passage_artifact_id']]}
    response = lookup_response()
    enriched = research.lookup(ops, lookup_args(CITATION+' Additional source context.', old_read['research_id']))
    assert enriched['sources'][0]['id'] == sid
    assert_header(enriched['sources'][0])
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: pytest.fail('Historical body must be reused'))
    reread = research.read(ops, read_args(enriched['research_id'], sid, opinion_id=2593291))
    assert_header(reread)
    assert reread['text'] == 'Historical body.'
    response = lookup_response(court='cod')
    response[0]['clusters'][0]['headmatter'] = '<p>A conflicting provider court header.</p>'
    conflict = research.lookup(ops, lookup_args(CITATION+' Another selected source.', reread['research_id']))
    value = conflict['sources'][0]
    assert value['case_header'] == HEADER and value['court'] == 'cod'
    assert any(o.get('case_header') == 'A conflicting provider court header.' for o in value['observations'])
    response = lookup_response(court='kyed')
    later = research.lookup(ops, lookup_args(CITATION+' A further selected source.', conflict['research_id']))
    value = later['sources'][0]
    assert value['court'] == 'cod' and value['case_header'] == HEADER
    assert any(o.get('court') == 'kyed' for o in value['observations'])
    assert all(db.session.get(AgentArtifact, uid).content_json == raw for uid, raw in unchanged.items())


def test_ambiguous_lookup_keeps_each_case_and_its_court_evidence_separate(ops, monkeypatch):
    response = lookup_response()
    other = copy.deepcopy(response[0]['clusters'][0])
    other.update(id=77, court='nysd', case_name='Different case', absolute_url='/opinion/77/other/',
                 headmatter='<p>Different case. Southern District of New York.</p>')
    response[0].update(status=300, clusters=[response[0]['clusters'][0], other])
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: (response, None))
    result = research.lookup(ops, lookup_args(CITATION))
    first, second = result['sources']
    assert_header(first)
    assert second['cluster_id'] == 77 and second['court'] == 'nysd'
    assert 'New York' in second['case_header'] and first['id'] != second['id']
    assert result['authorities'][0]['candidates'] == 2


@pytest.mark.parametrize('header', [None, '', '  ', [], {}, 7, '<p></p>'])
def test_missing_or_unusable_optional_headers_do_not_infer_a_court(header):
    row = lookup_response(header=False)[0]['clusters'][0]
    row['headmatter'] = header
    value = research._cluster_source(row)
    assert value['court'] is None
    assert 'case_header' not in value and 'case_header_source' not in value


def test_search_results_preserve_existing_court_and_available_header():
    row = lookup_response()[0]['clusters'][0]
    row['court'] = 'kyed'
    value = sources._case_result(row)
    assert value['court'] == 'kyed' and value['case_header'] == HEADER
    assert value['case_header_source']['url'] == f'{sources.API_BASE}search/'


@pytest.mark.parametrize('name', ['analyze_material', 'create_draft', 'revise_draft', 'verify_draft'])
def test_court_evidence_and_guidance_reach_controller_and_all_material_operations(agent, monkeypatch, name):
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: (lookup_response(), None))
    monkeypatch.setattr(research, '_discover_mentions', lambda *a, **kw: [])
    operation(agent, monkeypatch, 'lookup_authorities', {'targets': [{'file_id': None,
        'draft_id': None, 'text': CITATION, 'start': None, 'end': None}],
        'mode': 'overview', 'purpose': 'Check the court attribution.', 'research_id': None})
    found = last_result(agent)
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: {'status': 'read',
        'text': 'OPINION\nBERTELSMAN, District Judge.\nActual opinion body.',
        'opinion_id': 2593291, 'blocks': []})
    operation(agent, monkeypatch, 'read_authority', {'research_id': found['research_id'],
        'source_id': found['sources'][0]['id'], 'opinion_id': 2593291, 'query': None, 'start': 0})
    result = last_result(agent)
    passage_id = result['passage_artifact_id']
    original = db.session.get(AgentArtifact, passage_id).content_json
    db.session.expire_all()
    values = {'instructions': 'Check or retain the supported court attribution.', 'material_ids': [passage_id]}
    if name in {'revise_draft', 'verify_draft'}:
        values['target'] = 'current_draft'
    if name == 'revise_draft':
        values['revision_mode'] = 'general_revision'
    if name == 'verify_draft':
        values['check_citations'] = False
    calls = []
    def check_material(materials, options):
        assert_header(materials[0])
        assert materials[0]['text'].startswith('OPINION') and 'Kentucky' not in materials[0]['text']
        assert COURT_ATTRIBUTION_GUIDANCE in options['sys_prompt']
        calls.append(1)
    def writer(prompt, schema, **options):
        check_material(json.loads(prompt)['materials'], options)
        if schema is drafts.MarkdownDraftResult:
            return schema(title='Review', markdown='Supported court attribution.', update_court_format=False)
        return schema(mode='patch', title=None, markdown=None, replacements=[{
            'old_text': 'Original facts.', 'new_text': 'Reviewed facts.'}], update_court_format=False)
    def reviewer(prompt, **options):
        marker = '\nSupporting materials:\n' if name == 'verify_draft' else '\nMaterials (source evidence and prior generated work are labeled):\n'
        check_material(json.loads(prompt.split(marker)[1]), options)
        return 'Court attribution reviewed against supplied evidence.'
    monkeypatch.setattr(base, 'chat_completion_parse', writer)
    monkeypatch.setattr(base, 'chat_completion', reviewer)
    def controller(request):
        system = next(x['content'] for x in request['input'] if x.get('role') == 'system')
        assert COURT_ATTRIBUTION_GUIDANCE in system
        outputs = [json.loads(x['output']) for x in request['input'] if x.get('type') == 'function_call_output']
        assert_header(outputs[-1])
        return call(name, values)
    monkeypatch.setattr(base, 'responses_completion', controller)
    assert tick(agent) == {'continued': True}
    assert tick(agent) == {'continued': True}
    assert not last_result(agent).get('error'), last_result(agent)
    assert calls == [1]
    assert db.session.get(AgentArtifact, passage_id).content_json == original
