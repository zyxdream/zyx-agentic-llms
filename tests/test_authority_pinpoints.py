"""Reporter-page evidence is preserved, not inferred from other numbering.

Provider doubles here verify information delivery and saved artifact behavior.
They do not establish whether a live model follows the supplied guidance.
"""
import hashlib
import json
from pathlib import Path
import re

import pytest

from backend.ai import base
from backend.database import db
from backend.domains.legal import authority_research as research, authority_sources as sources
from backend.domains.legal.instructions import AUTHORITY_LOCATION_NOTE
from backend.models import AgentArtifact
from backend.util import document_drafts as drafts
from tests.test_agent_controller import agent, call, last_result, operation, tick
from tests.test_authority_research import case_result, lookup_args, ops, read_args, record
from tests.test_document_preparation import app


FIXTURES = Path(__file__).parent / 'fixtures' / 'authority-pinpoints'
CASES = ('naranjo', 'naranjo_without_page_marker', 'reaves')


def fixture(name):
    info = json.loads((FIXTURES / 'provenance.json').read_text())['cases'][name]
    text = (FIXTURES / info['filename']).read_text()
    assert hashlib.sha256(text.encode()).hexdigest() == info['source_text_sha256']
    return info, text


def citation_response(info):
    response = case_result(name=info['case_name'], cluster_id=info['opinion_id'],
                           citation=info['citation'])
    response['clusters'][0]['sub_opinions'] = [
        f"{sources.API_BASE}opinions/{info['opinion_id']}/"]
    return response


def fetched(info, text, *, pdf=False):
    return {'status': 'read', 'text': text, 'opinion_id': info['opinion_id'],
        'title': info['case_name'], 'url': info['url'],
        'content_type': 'application/pdf' if pdf else 'text/plain',
        'blocks': [{'start': 0, 'end': len(text), **({'page': 5, 'location': 'PDF page 5'}
            if pdf else {'location': f"CourtListener opinion {info['opinion_id']}"})}]}


def assert_case_evidence(name, passage):
    if name == 'naranjo':
        quote = passage.index('Relevant evidence is inherently prejudicial')
        assert passage.index('*1469') < quote < passage.index('*1470')
    elif name == 'naranjo_without_page_marker':
        assert 'Relevant evidence is inherently prejudicial' in passage
        assert '*1469' not in passage and '*1470' not in passage
    else:
        assert 'insurance fraud' in passage and '608(b)' in passage
        assert not re.search(r'\b86[678]\b', passage)


@pytest.mark.parametrize('name', CASES)
def test_faithful_sources_separate_content_from_reporter_page_evidence(name):
    info, text = fixture(name)
    assert_case_evidence(name, text[info['recorded_passage_start']:info['recorded_passage_end']])
    if name == 'naranjo_without_page_marker':
        _, full = fixture('naranjo')
        span = info['original_text_range']
        assert text == full[span['start']:span['end']]


@pytest.mark.parametrize('name', CASES)
def test_reader_keeps_exact_source_offsets_cache_and_location_note(ops, monkeypatch, name):
    info, text = fixture(name)
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([citation_response(info)], None))
    found = research.lookup(ops, lookup_args(info['citation']))
    sid = found['sources'][0]['id']
    fetches = []
    def fetch(*args, **kwargs):
        fetches.append(kwargs.get('opinion_id'))
        return fetched(info, text)
    monkeypatch.setattr(sources, 'fetch_source', fetch)
    result = research.read(ops, read_args(found['research_id'], sid,
        opinion_id=info['opinion_id'], start=info['recorded_passage_start']))
    assert result['text'] == text[result['start']:result['end']]
    assert result['start'] == info['recorded_passage_start']
    assert result['end'] == info['recorded_passage_end']
    assert result['location_note'] == AUTHORITY_LOCATION_NOTE
    assert result['match_start'] is None and result['match_end'] is None
    assert_case_evidence(name, result['text'])
    for field in ('reporter_page', 'pinpoint_verified', 'published_page', 'verified'):
        assert field not in result
    passage_id, cache_id = result['passage_artifact_id'], result['source_artifact_id']
    db.session.commit()
    original_passage = db.session.get(AgentArtifact, passage_id).content_json
    original_cache = db.session.get(AgentArtifact, cache_id).content_json
    db.session.expire_all()
    saved = record(passage_id)
    assert saved['location_note'] == AUTHORITY_LOCATION_NOTE
    for field in ('text', 'start', 'end', 'match_start', 'match_end', 'next_start', 'locations'):
        assert saved[field] == result[field]
    query = 'insurance fraud' if name == 'reaves' else 'Relevant evidence is inherently prejudicial'
    matched = research.read(ops, read_args(result['research_id'], sid,
        opinion_id=info['opinion_id'], query=query))
    expected = research._find_source_phrase(text, query, 0)
    assert (matched['match_start'], matched['match_end']) == expected
    assert matched['text'] == text[matched['start']:matched['end']]
    assert matched['location_note'] == AUTHORITY_LOCATION_NOTE
    if result['next_start'] is not None:
        following = research.read(ops, read_args(matched['research_id'], sid,
            opinion_id=info['opinion_id'], start=result['next_start']))
        assert following['text'] == text[result['next_start']:following['end']]
        assert following['location_note'] == AUTHORITY_LOCATION_NOTE
    assert fetches == [info['opinion_id']]
    assert db.session.get(AgentArtifact, passage_id).content_json == original_passage
    assert db.session.get(AgentArtifact, cache_id).content_json == original_cache


def test_pdf_page_position_is_retained_without_becoming_a_reporter_page(ops, monkeypatch):
    info, text = fixture('naranjo_without_page_marker')
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([citation_response(info)], None))
    found = research.lookup(ops, lookup_args('The uploaded brief claims ' + info['citation'] + ', 1469.'))
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: fetched(info, text, pdf=True))
    result = research.read(ops, read_args(found['research_id'], found['sources'][0]['id'],
        opinion_id=info['opinion_id']))
    assert result['locations'] == [{'start': 0, 'end': len(text), 'page': 5, 'location': 'PDF page 5'}]
    assert result['location_note'] == AUTHORITY_LOCATION_NOTE
    assert '*1469' not in result['text']
    assert 'reporter_page' not in result and 'pinpoint_verified' not in result


@pytest.mark.parametrize('name', CASES)
@pytest.mark.parametrize('tool', ['analyze_material', 'create_draft', 'revise_draft', 'verify_draft'])
def test_note_and_actual_page_evidence_reach_controller_and_material_operations(agent, monkeypatch, name, tool):
    info, text = fixture(name)
    monkeypatch.setattr(research, '_discover_mentions', lambda *a, **kw: [])
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([citation_response(info)], None))
    claimed_pinpoint = ', 867–68' if name == 'reaves' else ', 1469'
    operation(agent, monkeypatch, 'lookup_authorities', {'targets': [{'file_id': None,
        'draft_id': None, 'text': info['citation'] + claimed_pinpoint, 'start': None, 'end': None}],
        'mode': 'overview', 'purpose': 'Check the supplied citation and discussion.', 'research_id': None})
    found = last_result(agent)
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: fetched(info, text))
    operation(agent, monkeypatch, 'read_authority', {'research_id': found['research_id'],
        'source_id': found['sources'][0]['id'], 'opinion_id': info['opinion_id'],
        'query': None, 'start': info['recorded_passage_start']})
    observation = last_result(agent)
    passage_id = observation['passage_artifact_id']
    original = db.session.get(AgentArtifact, passage_id).content_json
    db.session.expire_all()
    values = {'instructions': 'Assess the discussion and its location using the supplied evidence.',
              'material_ids': [passage_id]}
    if tool in {'revise_draft', 'verify_draft'}:
        values['target'] = 'current_draft'
    if tool == 'revise_draft':
        values['revision_mode'] = 'general_revision'
    if tool == 'verify_draft':
        values['check_citations'] = False
    calls = []
    def check(materials, options):
        item = materials[0]
        assert item['location_note'] == AUTHORITY_LOCATION_NOTE
        assert item['text'] == observation['text']
        assert item['source_artifact_id'] == observation['source_artifact_id']
        assert_case_evidence(name, item['text'])
        assert drafts.SOURCE_LOCATION_GUIDANCE in options['sys_prompt']
        calls.append(tool)
    def writer(prompt, schema, **options):
        check(json.loads(prompt)['materials'], options)
        if schema is drafts.MarkdownDraftResult:
            return schema(title='Review', markdown='Evidence reviewed.', update_court_format=False)
        return schema(mode='patch', title=None, markdown=None,
            replacements=[{'old_text': 'Original facts.', 'new_text': 'Reviewed facts.'}], update_court_format=False)
    def reviewer(prompt, **options):
        marker = '\nSupporting materials:\n' if tool == 'verify_draft' else '\nMaterials (source evidence and prior generated work are labeled):\n'
        check(json.loads(prompt.split(marker)[1]), options)
        return 'Recorded source-location review.'
    monkeypatch.setattr(base, 'chat_completion_parse', writer)
    monkeypatch.setattr(base, 'chat_completion', reviewer)
    def controller(request):
        system = next(x['content'] for x in request['input'] if x.get('role') == 'system')
        assert drafts.SOURCE_LOCATION_GUIDANCE in system
        outputs = [json.loads(x['output']) for x in request['input'] if x.get('type') == 'function_call_output']
        assert outputs[-1]['location_note'] == AUTHORITY_LOCATION_NOTE
        assert outputs[-1]['text'] == observation['text']
        return call(tool, values)
    monkeypatch.setattr(base, 'responses_completion', controller)
    assert tick(agent) == {'continued': True}
    assert tick(agent) == {'continued': True}
    assert not last_result(agent).get('error'), last_result(agent)
    assert calls == [tool]
    assert db.session.get(AgentArtifact, passage_id).content_json == original


def test_historical_passage_without_note_stays_readable_and_unchanged(agent, monkeypatch):
    info, text = fixture('naranjo_without_page_marker')
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': None,
        'ranges': [{'file_id': json.loads(agent.data_json)['document_snapshot']['files'][0]['file_id'],
                    'start': 0, 'end': 10}], 'start': 0, 'end': None})
    uid = last_result(agent)['artifact_id']
    artifact = db.session.get(AgentArtifact, uid)
    artifact.kind = research.SOURCE_KIND
    artifact.content_json = json.dumps({'kind': research.SOURCE_KIND, 'role': 'passage',
        'opinion_id': info['opinion_id'], 'text': text, 'start': 0, 'end': len(text),
        'locations': [{'start': 0, 'end': len(text), 'location': 'Historical opinion text'}]})
    db.session.commit()
    original = artifact.content_json
    db.session.expire_all()
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': uid,
        'ranges': [], 'start': 0, 'end': None})
    loaded = json.loads(last_result(agent)['text'])
    assert loaded['text'] == text and 'location_note' not in loaded
    assert db.session.get(AgentArtifact, uid).content_json == original


def test_shared_guidance_allows_supported_pinpoints_and_qualifies_insufficient_evidence():
    guidance = drafts.SOURCE_LOCATION_GUIDANCE
    assert 'Accept explicit matching pagination' in guidance
    assert 'not verified from the retrieved sources' in guidance
    assert 'Character offsets do not establish reporter pages' in guidance
    assert 'Missing evidence does not establish fabrication or invalidity' in guidance
    assert 'Preserve claimed citations' in guidance
