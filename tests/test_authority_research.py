"""Authority research uses real SQL artifacts with offline provider boundaries."""
import copy
import json
from types import SimpleNamespace
import uuid

import pytest

from backend.database import db
from backend.models import AgentArtifact, AgentStep, Drafts
from backend.domains.legal import authority_sources as sources
from backend.domains.legal import authority_research as research
from backend.util import workspace_service as workflow, source_context
from tests.test_agent_controller import agent
from tests.test_document_preparation import app


CITATION = '123 U.S. 456'
REAL_DISCOVER_MENTIONS = research._discover_mentions


def target(text=None, **kwargs):
    return SimpleNamespace(**{'file_id': None, 'draft_id': None, 'text': text,
                              'start': None, 'end': None, **kwargs})


def lookup_args(text, research_id=None, mode='overview'):
    return SimpleNamespace(targets=[target(text)], purpose='Research the authorities as used in this text.',
                           research_id=research_id, mode=mode)


def step_ops(row):
    count = AgentStep.query.filter_by(run_id=row.id).count()
    step = AgentStep(id=str(uuid.uuid4()), run_id=row.id, sequence=count + 1,
        kind='tool', name='lookup_authorities', arguments_json='{}', state='running',
        created_at=workflow.now(), updated_at=workflow.now())
    db.session.add(step)
    db.session.commit()
    ops = SimpleNamespace(row=row, step=step, task=db.session.get(Drafts, row.draft_id),
        snapshot=json.loads(row.data_json)['document_snapshot'], model='gpt-6-luna', effort='medium')
    def save(kind, value, artifact_id=None):
        uid = artifact_id or str(uuid.uuid4())
        db.session.add(AgentArtifact(id=uid, run_id=row.id, step_id=step.id,
            kind=kind, content_json=json.dumps(value), created_at=workflow.now()))
        return uid
    ops.save = save
    ops.document = lambda uid: source_context.document(ops.snapshot, uid)
    ops.target = lambda uid: {'title': 'Current', 'format_version': 2, 'source_format': 'markdown',
        'revision_id': 'original-revision', 'markdown': 'Current source text.'}
    return ops


@pytest.fixture
def ops(agent, monkeypatch):
    monkeypatch.setattr(research, '_discover_mentions', lambda *a, **kw: [])
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([], None))
    monkeypatch.setattr(sources, 'search_sources', lambda *a, **kw: {'results': [], 'error': None})
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: pytest.fail('Unexpected source fetch'))
    return step_ops(agent)


def record(uid):
    return json.loads(db.session.get(AgentArtifact, uid).content_json)


def case_result(status=200, name='Smith v. Jones', cluster_id=51, citation=CITATION):
    return {'citation': citation, 'normalized_citations': [citation], 'status': status,
        'start_index': 999_999, 'end_index': 1_000_001, 'error_message': '',
        'clusters': [] if cluster_id is None else [{'id': cluster_id, 'case_name': name,
            'absolute_url': f'/opinion/{cluster_id}/smith/', 'citations': [citation],
            'date_filed': '2020-01-02', 'court': 'Test court',
            'sub_opinions': [f'https://www.courtlistener.com/api/rest/v4/opinions/{cluster_id + 1}/']}]}


def web_result(url='https://court.example/opinion.pdf'):
    return {'type': 'web', 'provider': 'web', 'title': 'Official opinion', 'url': url,
            'snippet': 'A search preview is not opinion text.'}


def add_web_source(ops, monkeypatch, text='A cited case ' + CITATION):
    initial = research.lookup(ops, lookup_args(text))
    authority_ids = [item['id'] for item in initial['authorities']]
    monkeypatch.setattr(sources, 'search_sources', lambda *a, **kw: {'results': [web_result()], 'error': None})
    found = research.search(ops, SimpleNamespace(research_id=initial['research_id'], authority_ids=authority_ids,
        query='Smith Jones official opinion', provider='web'))
    return found['research_id'], found['sources'][0]['id']


def read_args(research_id, source_id, *, opinion_id=None, query=None, start=0):
    return SimpleNamespace(research_id=research_id, source_id=source_id, opinion_id=opinion_id, query=query, start=start)


def anchored_mentions(mentions, window):
    """Supply explicit source anchors for mocked model responses."""
    result = []
    for incoming in mentions:
        item = dict(incoming)
        citation, context = item.get('citation_as_written', ''), item.get('context', '')
        local_context = window['text'].find(context) if context else -1
        offset = (window['start'] + local_context + context.find(citation)
                  if local_context >= 0 and citation in context else window['core_start'])
        unit = next((unit for unit in window['units'] if unit['start'] <= offset < unit['end']), window['units'][0])
        item.setdefault('unit_id', unit['id'])
        item.setdefault('occurrence_index', unit['text'][:max(0, offset - unit.get('visible_start', unit['start']))].count(citation) + 1)
        result.append(item)
    return result


def discovery_rows(mentions):
    return lambda *args, **kwargs: anchored_mentions(mentions, kwargs['window'])


def capture_discovery_model(monkeypatch, mentions):
    from backend.ai import base
    calls = []
    def parse(prompt, schema, **kwargs):
        payload = json.loads(prompt)
        calls.append({'input': payload, 'schema': schema, 'options': kwargs})
        window = next(research._iter_lookup_windows(payload['original_text']))
        return schema.model_validate({'mentions': anchored_mentions(mentions, window)})
    monkeypatch.setattr(research, '_discover_mentions', REAL_DISCOVER_MENTIONS)
    monkeypatch.setattr(base, 'chat_completion_parse', parse)
    return calls






@pytest.mark.parametrize('cluster_id', [None, 0, ''])
def test_viewer_url_is_absent_without_a_cluster(ops, cluster_id):
    assert research._viewer_url(ops, {'cluster_id': cluster_id, 'citations': [CITATION]}) is None


def test_discovery_receives_exact_original_and_compact_all_candidate_findings(ops, monkeypatch):
    text = f'Smith v. Jones, {CITATION}; an unresolved citation is 99 F.4th 222.'
    found = case_result(name='Returned Caption')
    found['extra_provider_field'] = {'large_unrelated_payload': 'not needed'}
    found['clusters'][0]['opinion_body'] = 'This body must not enter discovery.'
    found['clusters'].append({'id': 52, 'case_name': 'Another Returned Caption',
        'date_filed': '2021-04-05', 'court': 'Second court',
        'citations': [{'volume': 99, 'reporter': 'F.4th', 'page': 222}],
        'sub_opinions': ['https://www.courtlistener.com/api/rest/v4/opinions/53/'],
        'absolute_url': '/opinion/52/another/', 'extra': 'not needed'})
    unresolved = case_result(status=404, cluster_id=None, citation='99 F.4th 222')
    unresolved['error_message'] = 'Parsed citation was not located.'
    raw = [found, unresolved]
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: (raw, None))
    calls = capture_discovery_model(monkeypatch, [])
    args = lookup_args(text)
    result = research.lookup(ops, args)
    assert len(calls) == 1 and calls[0]['schema'] is research.DiscoveredAuthorities
    payload = calls[0]['input']
    assert payload['purpose'] == args.purpose and payload['original_text'] == text
    assert payload['mode'] == 'overview'
    assert payload['extraction_range'] == payload['context_range'] == {'start': 0, 'end': len(text)}
    assert ''.join(unit['text'] for unit in payload['source_units']) == text
    assert payload['courtlistener_findings'] == {'results': [
            {'citation': CITATION, 'normalized_citations': [CITATION], 'status': 200,
             'error_message': '', 'candidates': [
                 {'cluster_id': 51, 'case_name': 'Returned Caption', 'court': 'Test court',
                  'date': '2020-01-02', 'citations': [CITATION]},
                 {'cluster_id': 52, 'case_name': 'Another Returned Caption', 'court': 'Second court',
                  'date': '2021-04-05', 'citations': ['99 F.4th 222']},
             ]},
            {'citation': '99 F.4th 222', 'normalized_citations': ['99 F.4th 222'],
             'status': 404, 'error_message': 'Parsed citation was not located.', 'candidates': []},
        ], 'error': None}
    instructions = calls[0]['options']['sys_prompt']
    assert 'Never substitute a returned candidate name' in instructions
    assert 'not established identities or exhaustive coverage' in instructions
    saved = record(result['research_id'])
    assert record(saved['targets'][0]['text_artifact_id'])['text'] == text
    assert [candidate['case_name'] for candidate in saved['authorities'][0]['candidates']] == [
        'Returned Caption', 'Another Returned Caption']
    assert saved['authorities'][1]['provider_outcomes'][0]['status'] == 404


def test_candidate_assisted_discovery_retains_matched_uses_conflicts_and_other_references(ops, monkeypatch):
    first = f'Smith v. Jones, {CITATION}, is cited for the first proposition.'
    second = f'Other v. Caption, {CITATION}, is cited for a different proposition.'
    name_only = 'Smith is also discussed by name without a reporter citation.'
    short_form = 'Id. is cited for another point.'
    statute = 'The draft also invokes 42 U.S.C. § 1983.'
    text = '\n\n'.join([first, second, name_only, short_form, statute])
    mentions = [
        {'kind': 'case', 'citation_as_written': f'Smith v. Jones, {CITATION}',
         'case_name': 'Smith v. Jones', 'context': first, 'proposition': 'First proposition as drafted.'},
        {'kind': 'case', 'citation_as_written': f'Other v. Caption, {CITATION}',
         'case_name': 'Other v. Caption', 'context': second, 'proposition': 'Different proposition as drafted.'},
        {'kind': 'case', 'citation_as_written': 'Smith', 'case_name': 'Smith',
         'context': name_only, 'proposition': None},
        {'kind': 'case', 'citation_as_written': 'Id.', 'case_name': None,
         'context': short_form, 'proposition': 'Another point as drafted.'},
        {'kind': 'statute', 'citation_as_written': '42 U.S.C. § 1983', 'case_name': None,
         'context': statute, 'proposition': None},
    ]
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: (
        [case_result(name='CourtListener Caption')], None))
    calls = capture_discovery_model(monkeypatch, mentions)
    result = research.lookup(ops, lookup_args(text, mode='audit'))
    saved = record(result['research_id'])
    authority = saved['authorities'][0]
    assert len(authority['occurrences']) == 2
    assert [item['citation_as_written'] for item in authority['occurrences']] == [
        f'Smith v. Jones, {CITATION}', f'Other v. Caption, {CITATION}']
    assert [item['case_name_as_used'] for item in authority['occurrences']] == ['Smith v. Jones', 'Other v. Caption']
    assert [item['context'] for item in authority['occurrences']] == [first, second]
    assert [item['proposition'] for item in authority['occurrences']] == [
        'First proposition as drafted.', 'Different proposition as drafted.']
    assert [item['source_location']['start'] for item in authority['occurrences']] == [
        text.index(f'Smith v. Jones, {CITATION}'), text.index(f'Other v. Caption, {CITATION}')]
    assert authority['candidates'][0]['case_name'] == 'CourtListener Caption'
    assert [item['citation_as_written'] for item in saved['authorities'][1:]] == ['Smith', 'Id.', '42 U.S.C. § 1983']
    assert all(item['checking']['identity'] == 'not_assessed' for item in saved['authorities'])
    assert calls[0]['input']['courtlistener_findings']['results'][0]['candidates'][0]['case_name'] == 'CourtListener Caption'
    assert result['coverage']['complete']


@pytest.mark.parametrize('lookup_error', [None, 'CourtListener is temporarily unavailable.'])
def test_discovery_still_runs_when_lookup_is_empty_or_errors(ops, monkeypatch, lookup_error):
    text = 'The draft invokes 42 U.S.C. § 1983.'
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([], lookup_error))
    calls = capture_discovery_model(monkeypatch, [
        {'kind': 'statute', 'citation_as_written': '42 U.S.C. § 1983',
         'case_name': None, 'context': text, 'proposition': None},
    ])
    result = research.lookup(ops, lookup_args(text))
    assert len(calls) == 1
    assert calls[0]['input']['courtlistener_findings'] == {'results': [], 'error': lookup_error}
    assert result['authority_count'] == 1 and result['authorities'][0]['kind'] == 'statute'
    assert result['coverage']['complete'] is (lookup_error is None)
    assert result['error'] is bool(lookup_error)
    assert [item['stage'] for item in result['coverage']['failed_chunks']] == (
        ['courtlistener'] if lookup_error else [])


def test_original_anchors_ignore_provider_offsets_and_keep_repeated_propositions(ops, monkeypatch, app):
    first = f'Smith v. Jones, {CITATION}, is cited for the first proposition.'
    second = f'Other v. Caption, {CITATION}, is cited for a different proposition.'
    text = first + '\n\n' + second
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([case_result()], None))
    monkeypatch.setattr(research, '_discover_mentions', discovery_rows([
        {'kind': 'case', 'citation_as_written': f'Smith v. Jones, {CITATION}', 'case_name': 'Smith v. Jones',
         'context': first, 'proposition': 'First proposition as drafted.'},
        {'kind': 'case', 'citation_as_written': f'Other v. Caption, {CITATION}', 'case_name': 'Other v. Caption',
         'context': second, 'proposition': 'Different proposition as drafted.'},
    ]))
    app.config['PUBLIC_APP_URL'] = 'https://legal.example'
    result = research.lookup(ops, lookup_args(text, mode='audit'))
    saved = record(result['research_id'])
    assert len(saved['authorities']) == 1
    authority = saved['authorities'][0]
    assert len(authority['occurrences']) == 2
    assert [item['source_location']['start'] for item in authority['occurrences']] == [0, text.index('Other v. Caption')]
    assert authority['occurrences'][0]['context'] == first
    assert [item['case_name_as_used'] for item in authority['occurrences']] == ['Smith v. Jones', 'Other v. Caption']
    assert [item['proposition'] for item in authority['occurrences']] == ['First proposition as drafted.', 'Different proposition as drafted.']
    assert authority['checking']['identity'] == 'not_assessed'
    assert 'is_valid' not in authority
    assert authority['candidates'][0]['viewer_url']=='https://www.courtlistener.com/opinion/51/smith/'
    assert saved['coverage']['complete']


def test_overview_examples_expand_to_audit_occurrences_without_rewriting_prior_work(ops, monkeypatch):
    text = f'{CITATION} supports standing; {CITATION} supports remedies.'
    mentions = [
        {'kind': 'case', 'citation_as_written': CITATION, 'case_name': None, 'context': text,
         'proposition': 'Standing as drafted.', 'occurrence_index': 1},
        {'kind': 'case', 'citation_as_written': CITATION, 'case_name': None, 'context': text,
         'proposition': 'Remedies as drafted.', 'occurrence_index': 2},
    ]
    calls = []
    def discover(*args, **kwargs):
        calls.append(kwargs['mode'])
        return anchored_mentions(mentions, kwargs['window'])
    monkeypatch.setattr(research, '_discover_mentions', discover)
    overview = research.lookup(ops, lookup_args(text))
    before = db.session.get(AgentArtifact, overview['research_id']).content_json
    assert overview['discovery_mode'] == overview['coverage']['discovery_mode'] == 'overview'
    assert overview['authorities'][0]['occurrences'] == 1
    audited = research.lookup(ops, SimpleNamespace(targets=[], purpose='Audit the same selected scope.',
        research_id=overview['research_id'], mode='audit'))
    saved = record(audited['research_id'])
    assert calls == ['overview', 'audit']
    assert audited['discovery_mode'] == audited['coverage']['discovery_mode'] == 'audit'
    assert saved['authorities'][0]['id'] == record(overview['research_id'])['authorities'][0]['id']
    assert [item['proposition'] for item in saved['authorities'][0]['occurrences']] == [
        'Standing as drafted.', 'Remedies as drafted.']
    assert len(saved['authorities'][0]['occurrences']) == 2
    assert db.session.get(AgentArtifact, overview['research_id']).content_json == before
    assert research.catalogue(ops.row)[0]['discovery_mode'] == 'audit'


def test_audit_repeated_same_paragraph_uses_and_short_forms_have_independent_locations(ops, monkeypatch):
    text = f'{CITATION} supports A; {CITATION} supports B. Later Id. supports C.'
    rows = [
        {'kind': 'case', 'citation_as_written': CITATION, 'case_name': None,
         'context': text, 'proposition': 'A', 'occurrence_index': 1},
        {'kind': 'case', 'citation_as_written': CITATION, 'case_name': None,
         'context': text, 'proposition': 'B', 'occurrence_index': 2},
        {'kind': 'case', 'citation_as_written': 'Id.', 'case_name': None,
         'context': text, 'proposition': 'C', 'occurrence_index': 1},
    ]
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([case_result()], None))
    monkeypatch.setattr(research, '_discover_mentions', discovery_rows(rows))
    result = research.lookup(ops, lookup_args(text, mode='audit'))
    occurrences = sorted((item for authority in record(result['research_id'])['authorities']
                          for item in authority['occurrences']), key=lambda item: item['source_location']['start'])
    assert [item['source_location']['start'] for item in occurrences] == [
        0, text.rindex(CITATION), text.index('Id.')]
    assert [item['proposition'] for item in occurrences] == ['A', 'B', 'C']
    assert [item['citation_as_written'] for item in occurrences] == [CITATION, CITATION, 'Id.']
    for item in occurrences:
        span = item['source_location']
        assert text[span['start']:span['end']] == item['citation_as_written']


def test_overview_preserves_conflicting_captions_and_suspect_citation_variants(ops, monkeypatch):
    references = [f'Smith v. Jones, {CITATION}', f'Smyth v. Jones, {CITATION}',
                  'Smith v. Jones, 123 U.S. 465']
    text = '; '.join(references)
    rows = [{'kind': 'case', 'citation_as_written': reference,
             'case_name': reference.split(',')[0], 'context': text, 'proposition': None}
            for reference in references]
    monkeypatch.setattr(research, '_discover_mentions', discovery_rows(rows))
    result = research.lookup(ops, lookup_args(text))
    occurrences = [item for authority in record(result['research_id'])['authorities']
                   for item in authority['occurrences']]
    assert {item['citation_as_written'] for item in occurrences} == set(references)
    assert result['coverage']['complete']


def test_pinned_explicit_range_is_not_expanded_when_switching_discovery_modes(ops, monkeypatch):
    selected = f'Only {CITATION} is selected.'
    text = 'Outside 99 F.4th 222.\n\n' + selected + '\n\nOutside 42 U.S.C. § 1983.'
    lo, hi = text.index(selected), text.index(selected) + len(selected)
    calls = []
    monkeypatch.setattr(sources, 'lookup_citations', lambda part, **kw: (calls.append(part) or [], None))
    monkeypatch.setattr(research, '_discover_mentions', discovery_rows([
        {'kind': 'case', 'citation_as_written': CITATION, 'case_name': None,
         'context': selected, 'proposition': None}]))
    first = research.lookup(ops, SimpleNamespace(targets=[target(text, start=lo, end=hi)],
        purpose='Inspect the selected range.', research_id=None, mode='overview'))
    second = research.lookup(ops, SimpleNamespace(targets=[], purpose='Audit the same range.',
        research_id=first['research_id'], mode='audit'))
    assert calls == [selected, selected]
    saved = record(second['research_id'])
    assert [(item['start'], item['end']) for item in saved['coverage']['scope']] == [(lo, hi)]
    location = saved['authorities'][0]['occurrences'][0]['source_location']
    assert location['start'] == text.index(CITATION)
    assert text[location['start']:location['end']] == CITATION


def test_empty_targets_continue_only_the_latest_explicit_scope(ops, monkeypatch):
    first_text, second_text = 'Document A mentions Smith.', 'Document B mentions Jones.'
    first = research.lookup(ops, lookup_args(first_text))
    second = research.lookup(ops, lookup_args(second_text, research_id=first['research_id']))
    previous = record(second['research_id'])
    assert len(previous['targets']) == 2
    calls = []
    monkeypatch.setattr(sources, 'lookup_citations', lambda text, **kw: (calls.append(text) or [], None))
    audited = research.lookup(ops, SimpleNamespace(targets=[], purpose='Audit the current scope.',
        research_id=second['research_id'], mode='audit'))
    assert calls == [second_text]
    assert audited['coverage']['scope'] == second['coverage']['scope']
    saved = record(audited['research_id'])
    assert len(saved['targets']) == 2  # Earlier pinned text remains available as history.


def test_same_source_span_does_not_duplicate_when_later_lookup_changes_authority_group(ops, monkeypatch):
    citation_as_written = f'Smith v. Jones, {CITATION}'
    text = citation_as_written + ' supports the drafted point.'
    monkeypatch.setattr(research, '_discover_mentions', discovery_rows([
        {'kind': 'case', 'citation_as_written': citation_as_written, 'case_name': 'Smith v. Jones',
         'context': text, 'proposition': 'The drafted point.'}]))
    initial = research.lookup(ops, lookup_args(text))
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([case_result()], None))
    continued = research.lookup(ops, SimpleNamespace(targets=[], purpose='Audit and attach database findings.',
        research_id=initial['research_id'], mode='audit'))
    saved = record(continued['research_id'])
    occurrences = [item for authority in saved['authorities'] for item in authority['occurrences']]
    assert len(occurrences) == 1
    assert occurrences[0]['citation_as_written'] == citation_as_written
    assert occurrences[0]['discovery_mode'] == 'audit'
    assert occurrences[0]['source_location']['start'] == 0
    assert occurrences[0]['source_location']['end'] == len(citation_as_written)
    assert any(authority['candidates'] for authority in saved['authorities'])


def test_old_record_checkpoints_cannot_certify_new_audit_windows(ops, monkeypatch):
    first = research.lookup(ops, lookup_args(CITATION))
    old = record(first['research_id'])
    old.pop('discovery_mode', None)
    old.pop('chunk_plan_version', None)
    old['coverage'].pop('discovery_mode', None)
    old['coverage'].pop('chunk_plan_version', None)
    old['coverage'].pop('plan_id', None)
    old['coverage']['completed_chunks'] = ['old-character-chunk-key']
    legacy_id = ops.save(research.RESEARCH_KIND, old)
    db.session.commit()
    before = db.session.get(AgentArtifact, legacy_id).content_json
    calls = []
    monkeypatch.setattr(sources, 'lookup_citations', lambda text, **kw: (calls.append(text) or [], None))
    result = research.lookup(ops, SimpleNamespace(targets=[], purpose='Audit pinned original source.',
        research_id=legacy_id, mode='audit'))
    assert calls == [CITATION]
    assert result['discovery_mode'] == 'audit' and result['coverage']['complete']
    assert 'old-character-chunk-key' not in result['coverage']['completed_chunks']
    assert db.session.get(AgentArtifact, legacy_id).content_json == before


def test_missing_and_conflicting_candidates_remain_provider_observations(ops, monkeypatch):
    raw = [case_result(name='Different Case', cluster_id=10), case_result(status=404, cluster_id=None, citation='99 F.4th 222')]
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: (raw, None))
    result = research.lookup(ops, lookup_args(f'Expected Name, {CITATION}; and 99 F.4th 222.'))
    saved = record(result['research_id'])
    first, second = saved['authorities']
    assert first['candidates'][0]['case_name'] == 'Different Case'
    assert second['provider_outcomes'][0]['status'] == 404
    assert not second['candidates']
    assert all(item['checking']['identity'] == 'not_assessed' for item in saved['authorities'])
    assert result['error'] is False


def test_search_appends_all_observed_sources_without_overwriting_prior_snapshot(ops, monkeypatch):
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([case_result()], None))
    initial = research.lookup(ops, lookup_args(CITATION))
    before = db.session.get(AgentArtifact, initial['research_id']).content_json
    monkeypatch.setattr(sources, 'search_sources', lambda *a, **kw: {
        'results': [web_result(f'https://court.example/{i}.pdf') for i in range(7)], 'error': None})
    result = research.search(ops, SimpleNamespace(research_id=initial['research_id'],
        authority_ids=[initial['authorities'][0]['id']], query='official case', provider='web'))
    saved = record(result['research_id'])
    assert saved['parent_research_id'] == initial['research_id']
    assert len(saved['sources']) == 8 and len(result['sources']) == 5
    assert result['source_count'] == 7 and result['sources_truncated']
    assert saved['authorities'][0]['id'] == initial['authorities'][0]['id']
    assert len(saved['authorities'][0]['candidates']) == 8
    assert db.session.get(AgentArtifact, initial['research_id']).content_json == before


def test_search_preserves_findings_and_readable_citation_outside_source_preview(ops, monkeypatch):
    initial = research.lookup(ops, lookup_args('Research the overtime calculation.'))
    before = db.session.get(AgentArtifact, initial['research_id']).content_json
    urls = [f'https://official.example/section/{i}' for i in range(14)]
    summary = 'The bonus changes the regular rate; read section 12 for the rule.'
    citation = {'url': urls[12], 'title': 'Hourly rate employee', 'start_index': 43, 'end_index': 53}
    retrieved_at = '2026-11-14T15:00:00+00:00'
    limitations = ['Generated search synthesis; underlying pages have not been read.']
    calls = []

    def search_sources(*args, **kwargs):
        calls.append(args)
        return {'results': [web_result(url) for url in urls], 'error': None,
                'generated_summary': summary, 'summary_is_source_text': False,
                'citations': [citation], 'retrieved_at': retrieved_at, 'limitations': limitations}

    monkeypatch.setattr(sources, 'search_sources', search_sources)
    result = research.search(ops, SimpleNamespace(research_id=initial['research_id'],
        authority_ids=[], query='regular rate and overtime', provider='web'))
    db.session.commit()
    db.session.expire_all()
    saved = record(result['research_id'])
    cited_source = next(item for item in saved['sources'] if item['url'] == urls[12])
    expected = {'generated_summary': summary, 'summary_is_source_text': False,
                'citations': [{**citation, 'source_id': cited_source['id']}],
                'retrieved_at': retrieved_at, 'limitations': limitations}
    for key, value in expected.items():
        assert result[key] == saved['queries'][-1][key] == value
    assert len(calls) == 1
    assert result['source_count'] == 14 and result['sources_truncated']
    assert urls[12] not in {item['url'] for item in result['sources']}
    assert cited_source['passage_artifact_ids'] == [] and not cited_source.get('cached_sources')
    assert db.session.get(AgentArtifact, initial['research_id']).content_json == before

    # The returned citation is a usable reading handle, not a claim of a read.
    fetched = []
    def fetch_source(source, **kwargs):
        fetched.append(source['id'])
        return {'status': 'read', 'text': 'The actual source passage.', 'url': source['url'],
                'sha256': 'sourcehash', 'retrieved_at': retrieved_at}

    monkeypatch.setattr(sources, 'fetch_source', fetch_source)
    passage = research.read(ops, read_args(result['research_id'], result['citations'][0]['source_id']))
    assert fetched == [cited_source['id']]
    assert passage['text'] == 'The actual source passage.'
    assert passage['passage_artifact_id']


@pytest.mark.parametrize('provider', ['web', 'courtlistener'])
@pytest.mark.parametrize('error', [None, 'Search provider unavailable.'])
def test_search_without_summary_metadata_preserves_provider_outcome(ops, monkeypatch, provider, error):
    initial = research.lookup(ops, lookup_args('Research a question.'))
    monkeypatch.setattr(sources, 'search_sources', lambda *a, **kw: {
        'results': [] if error else [web_result()], 'error': error})
    result = research.search(ops, SimpleNamespace(research_id=initial['research_id'],
        authority_ids=[], query='research question', provider=provider))
    saved = record(result['research_id'])['queries'][-1]
    expected = {'generated_summary': '', 'summary_is_source_text': False,
                'citations': [], 'retrieved_at': None, 'limitations': []}
    for key, value in expected.items():
        assert result[key] == saved[key] == value
    assert result['error'] is bool(error)
    assert result['search_error'] == saved['error'] == error


def test_read_returns_exact_passage_and_reuses_cached_body_across_pages(ops, monkeypatch):
    rid, sid = add_web_source(ops, monkeypatch)
    body = 'First page. ' * 1500 + 'Important holding.' + ' More text.' * 1500
    calls = []
    def fetch(*a, **kw):
        calls.append(1)
        return {'status': 'read', 'text': body, 'url': web_result()['url'], 'title': 'Official opinion',
            'sha256': 'bodyhash', 'retrieved_at': '2026-11-09T00:00:00Z',
            'blocks': [{'start': 0, 'end': 18_000, 'page': 1}, {'start': 18_000, 'end': len(body), 'page': 2}]}
    monkeypatch.setattr(sources, 'fetch_source', fetch)
    first = research.read(ops, read_args(rid, sid))
    assert first['text'] == body[:12_000]
    assert first['next_start'] == 12_000
    material = record(first['passage_artifact_id'])
    assert material['role'] == 'passage' and material['research_id'] == rid
    assert first['source_artifact_id'] == material['source_artifact_id']
    cache = record(first['source_artifact_id'])
    assert cache['role'] == 'source_cache' and cache['text'] == body
    assert material['locations'][0]['page'] == 1
    second = research.read(ops, read_args(first['research_id'], sid, query='important holding', start=12_000))
    assert 'Important holding.' in second['text']
    assert second['source_artifact_id'] == first['source_artifact_id']
    assert record(second['passage_artifact_id'])['source_artifact_id'] == second['source_artifact_id']
    assert len(calls) == 1
    saved = record(second['research_id'])
    assert len(saved['sources'][0]['passage_artifact_ids']) == 2
    assert saved['authorities'] == []  # No authorities were invented from an uncited research question.


def test_opinion_metadata_does_not_read_all_opinions_or_claim_support(ops, monkeypatch):
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([case_result()], None))
    initial = research.lookup(ops, lookup_args(CITATION))
    sid = record(initial['research_id'])['sources'][0]['id']
    calls = []
    def fetch(source, opinion_id=None, **kwargs):
        calls.append(opinion_id)
        if opinion_id is None:
            return {'status': 'metadata', 'opinions': [{'id': 7, 'type': 'lead-opinion'}, {'id': 8, 'type': 'dissent'}]}
        return {'status': 'read', 'text': 'The dissent explains this different view.', 'opinion_id': 8,
                'opinion_type': 'dissent', 'url': 'https://www.courtlistener.com/opinion/51/case/'}
    monkeypatch.setattr(sources, 'fetch_source', fetch)
    metadata = research.read(ops, read_args(initial['research_id'], sid))
    assert len(metadata['opinions']) == 2 and 'passage_artifact_id' not in metadata
    assert not record(metadata['research_id'])['authorities'][0]['checking']['opinion_read']
    selected = research.read(ops, read_args(metadata['research_id'], sid, opinion_id=8))
    assert calls == [None, 8]
    passage = record(selected['passage_artifact_id'])
    source = record(metadata['research_id'])['sources'][0]
    assert passage['opinion_type'] == 'dissent'
    assert passage['url'] == selected['url'] == 'https://www.courtlistener.com/opinion/51/case/'
    for key in ('viewer_url', 'cluster_id', 'case_name', 'citations'):
        assert passage[key] == selected[key] == source[key]
    assert passage['cluster_id'] == 51 and passage['case_name'] == 'Smith v. Jones'
    assert passage['citations'] == [CITATION]
    assert passage['viewer_url']=='https://www.courtlistener.com/opinion/51/smith/'
    assert record(selected['research_id'])['authorities'][0]['checking']['proposition'] == 'not_assessed'


def test_partial_pdf_extraction_and_observed_links_remain_visible(ops, monkeypatch):
    rid, sid = add_web_source(ops, monkeypatch)
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: {
        'status': 'read', 'text': 'Readable first page.', 'extraction_complete': False, 'unreadable_pages': [2],
        'url': web_result()['url'], 'links': [{'url': 'https://court.example/section', 'title': 'Specific section'}]})
    result = research.read(ops, read_args(rid, sid))
    saved = record(result['research_id'])
    assert saved['coverage']['complete'] is True  # Discovery of selected source text already finished.
    assert saved['coverage']['limitations'][0]['unreadable_pages'] == [2]
    assert record(result['passage_artifact_id'])['unreadable_pages'] == [2]
    assert saved['sources'][0]['extraction_complete'] is False
    assert result['links'][0]['url'] == 'https://court.example/section'
    assert len(saved['sources']) == 2
    assert saved['sources'][1]['discovered_from'] == sid
    continued = research.lookup(ops, SimpleNamespace(targets=[], purpose='Continue the same research.',
        research_id=result['research_id']))
    assert continued['coverage']['complete']
    assert continued['coverage']['limitations'][0]['unreadable_pages'] == [2]


def test_identification_context_budget_exhaustion_propagates_without_a_coverage_error(ops, monkeypatch):
    from backend.ai.token_budget import ContextBudgetExceeded
    def discover(*args, **kwargs):
        raise ContextBudgetExceeded('The identification request exceeds its context budget.')
    monkeypatch.setattr(research, '_discover_mentions', discover)
    args = lookup_args('A source to inspect.')
    with pytest.raises(ContextBudgetExceeded, match='context budget'):
        research.lookup(ops, args)
    checkpoint = (AgentArtifact.query.filter_by(run_id=ops.row.id, kind=research.RESEARCH_KIND)
        .order_by(AgentArtifact.created_at.desc()).first())
    saved = record(checkpoint.id)
    assert saved['coverage']['failed_chunks'] == []
    assert saved['coverage']['completed_chunks'] == []
    monkeypatch.setattr(research, '_discover_mentions', lambda *a, **kw: [])
    recovered = research.lookup(ops, args)
    assert recovered['coverage']['complete']


def test_lookup_resumes_successful_chunks_after_interruption(ops, monkeypatch):
    build = research._iter_lookup_windows
    monkeypatch.setattr(research, '_iter_lookup_windows',
                        lambda text, *args, **kwargs: build(text, *args, max_chars=5, context_chars=0, **kwargs))
    calls = []
    def lookup(text, **kwargs):
        calls.append(text)
        if len(calls) == 2:
            raise workflow.WorkflowStopped('Simulated interrupted attempt')
        return [], None
    monkeypatch.setattr(sources, 'lookup_citations', lookup)
    args = lookup_args('FirstOther')
    with pytest.raises(workflow.WorkflowStopped):
        research.lookup(ops, args)
    result = research.lookup(ops, args)
    assert calls == ['First', 'Other', 'Other']
    assert record(result['research_id'])['coverage']['complete']


def test_lookup_reuses_completed_default_chunks_after_interruption(ops, monkeypatch):
    text = 'A' * 20_000 + 'B' * 5_000
    calls = []
    def lookup(chunk, **kwargs):
        calls.append(chunk)
        if len(calls) == 2:
            raise workflow.WorkflowStopped('Interrupt the second real chunk.')
        return [], None
    monkeypatch.setattr(sources, 'lookup_citations', lookup)
    args = lookup_args(text)
    with pytest.raises(workflow.WorkflowStopped):
        research.lookup(ops, args)
    result = research.lookup(ops, args)
    assert calls == [text[:21_500], text[18_500:], text[18_500:]]
    assert len(result['coverage']['completed_chunks']) == 2
    assert result['coverage']['complete']


def test_explicit_continuation_retries_failed_chunks_without_invalidity(ops, monkeypatch):
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([case_result(status=429, cluster_id=None)], None))
    initial = research.lookup(ops, lookup_args(CITATION))
    assert initial['error'] and not initial['coverage']['complete']
    assert initial['authorities'][0]['checking']['identity'] == 'not_assessed'
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([case_result()], None))
    followup_ops = step_ops(ops.row)
    result = research.lookup(followup_ops, SimpleNamespace(targets=[], purpose='Retry the unavailable lookup.', research_id=initial['research_id']))
    saved = record(result['research_id'])
    assert saved['coverage']['complete'] and not saved['coverage']['failed_chunks']
    assert [item['status'] for item in saved['authorities'][0]['provider_outcomes']] == [429, 200]


def test_full_file_scan_uses_canonical_text_beyond_preview_and_original_blocks(ops, monkeypatch):
    from backend.util.attachment_parser import manifest_from_blocks
    uid = ops.snapshot['files'][0]['file_id']
    original = ops.document(uid)
    text = 'Background ' * 5000 + '\n\n42 U.S.C. § 1983 appears near the end.'
    manifest = manifest_from_blocks([{'text': text, 'page': 3, 'location': 'Page 3'}])
    ops.document = lambda file_id: {**original, 'manifest': manifest}
    calls = []
    monkeypatch.setattr(sources, 'lookup_citations', lambda text, **kw: (calls.append(text) or [], None))
    needle = '42 U.S.C. § 1983'
    def discover(part, purpose, **kwargs):
        return [] if needle not in part else anchored_mentions([{'kind': 'statute', 'citation_as_written': needle,
            'context': '42 U.S.C. § 1983 appears near the end.', 'proposition': None, 'case_name': None}], kwargs['window'])
    monkeypatch.setattr(research, '_discover_mentions', discover)
    result = research.lookup(ops, SimpleNamespace(targets=[SimpleNamespace(file_id=uid, draft_id=None,
        text=None, start=None, end=None)], purpose='Review every authority.', research_id=None))
    saved = record(result['research_id'])
    assert len(calls) == 3 and any(needle in part for part in calls)
    occurrence = saved['authorities'][0]['occurrences'][0]
    assert occurrence['source_location']['start'] == text.index(needle)
    assert occurrence['source_location']['page'] == 3
    assert occurrence['source_location']['block_ids']
    assert saved['targets'][0]['full_target']


def test_hallucinated_reference_or_context_is_not_recorded_as_source_fact(ops, monkeypatch):
    monkeypatch.setattr(research, '_discover_mentions', discovery_rows([
        {'kind': 'statute', 'citation_as_written': '99 U.S.C. § 999', 'context': 'Invented', 'proposition': None},
        {'kind': 'case', 'citation_as_written': 'Smith', 'context': 'Smith held an invented proposition.', 'proposition': 'Invented'},
    ]))
    result = research.lookup(ops, lookup_args('The user mentions Smith.'))
    assert result['authorities'] == []
    assert not result['coverage']['complete']
    assert len(result['coverage']['failed_chunks']) == 2


def test_same_chat_research_and_linked_assessments_survive_new_request(ops, monkeypatch):
    initial = research.lookup(ops, lookup_args('A research question with no asserted authority.'))
    analysis_id = ops.save('analysis', {'kind': 'analysis', 'text': 'A generated interpretation.'})
    db.session.commit()
    with pytest.raises(ValueError, match='not linked'):
        research.resolve_artifact(ops, analysis_id)
    assessed = research.attach_assessment(ops, [initial['research_id']], analysis_id, 'A generated interpretation.')[0]
    db.session.commit()
    data = json.loads(ops.row.data_json)
    ops.row.status = 'completed'; ops.row.active_draft_id = None
    db.session.commit()
    data.pop('run_id', None); data['query'] = 'Continue the earlier legal research.'
    next_row = workflow.submit(ops.row.user_id, data)
    next_ops = step_ops(next_row)
    assert research.resolve_artifact(next_ops, analysis_id)['text'] == 'A generated interpretation.'
    assert research.resolve_artifact(next_ops, assessed)['parent_research_id'] == initial['research_id']
    assert [item['research_id'] for item in research.catalogue(next_row)] == [assessed]
    for user_id, draft_id in [(2, 1), (1, 2)]:
        unauthorized = SimpleNamespace(row=SimpleNamespace(user_id=user_id, chat_thread_id=draft_id))
        with pytest.raises(ValueError, match='unavailable'):
            research.resolve_artifact(unauthorized, assessed)


@pytest.mark.parametrize('text,query', [
    ('§ 778.110\nHourly rate employee.', '§ 778.110 Hourly rate employee'),
    ('hours worked in excess \nof 40', 'Hours worked in excess of 40'),
    ('A\t \r\nB\u00a0C', 'a b c'),
    ('“Employee’s” hourly—rate', '"Employee\'s" hourly-rate'),
    ('hourly‐rate and non‑discretionary pay', 'hourly-rate and non-discretionary pay'),
    ('compen\u00adsation', 'compensation'),
    ('Straße rate', 'STRASSE RATE'),
])
def test_source_phrase_matches_formatting_at_original_positions(text, query):
    prefix = 'Unrelated introduction. '
    body = prefix + text + ' Trailing material.'
    assert research._find_source_phrase(body, query, 0) == (len(prefix), len(prefix) + len(text.rstrip('.')))


@pytest.mark.parametrize('text,query', [
    ('§ 778.109', '§ 778.110'),
    ('hourly rate employee', 'hourly rate employer'),
    ('hourly rate employee', 'employee hourly rate'),
    ('non-discretionary', 'nondiscretionary'),
    ('hourly rate', 'houry rate'),
    ('anything', ' \t\n\u00ad'),
    ('anything', ''),
])
def test_source_phrase_does_not_guess_words_numbers_or_empty_queries(text, query):
    assert research._find_source_phrase(text, query, 0) is None


def test_tolerant_read_preserves_original_passages_locations_and_cache(ops, monkeypatch):
    rid, sid = add_web_source(ops, monkeypatch)
    prefix = 'Introductory material. ' * 700
    heading = '§ 778.110\nHourly\u00a0rate employee.'
    body = prefix + heading + '\n' + 'Further explanation. ' * 700
    calls = []
    def fetch(*args, **kwargs):
        calls.append(1)
        return {'status': 'read', 'text': body, 'sha256': 'original-hash',
                'blocks': [{'start': 0, 'end': len(prefix), 'page': 1},
                           {'start': len(prefix), 'end': len(body), 'page': 2}]}
    monkeypatch.setattr(sources, 'fetch_source', fetch)
    first = research.read(ops, read_args(rid, sid, query='§ 778.110 Hourly rate employee'))
    assert first['match_found']
    assert first['start'] == len(prefix) - 1000
    assert first['end'] == first['start'] + research.PAGE_CHARACTERS
    assert first['text'] == body[first['start']:first['end']]
    assert heading in first['text']
    saved = record(first['passage_artifact_id'])
    assert saved['source_sha256'] == 'original-hash'
    assert saved['locations'] == [
        {'start': first['start'], 'end': len(prefix), 'page': 1},
        {'start': len(prefix), 'end': first['end'], 'page': 2}]
    second = research.read(ops, read_args(first['research_id'], sid, query='hourly rate employee'))
    assert second['source_artifact_id'] == first['source_artifact_id'] and calls == [1]
    missed = research.read(ops, read_args(second['research_id'], sid,
        query='hourly rate employee', start=len(prefix) + len(heading)))
    assert missed['match_found'] is False and 'passage_artifact_id' not in missed
    empty = research.read(ops, read_args(missed['research_id'], sid, query=' \t\u00ad'))
    assert empty['match_found'] is False and 'passage_artifact_id' not in empty
    page = research.read(ops, read_args(empty['research_id'], sid, start=first['next_start']))
    assert page['text'] == body[page['start']:page['end']]
    assert calls == [1]


def test_source_read_errors_and_query_misses_are_saved_observations(ops, monkeypatch):
    rid, sid = add_web_source(ops, monkeypatch)
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: {'status': 'unavailable', 'error': 'Official page requires JavaScript.'})
    failed = research.read(ops, read_args(rid, sid))
    assert failed['error'] and 'JavaScript' in failed['read_error']
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: {'status': 'read', 'text': 'Some readable source text.'})
    missed = research.read(ops, read_args(failed['research_id'], sid, query='not in this text'))
    assert missed['match_found'] is False and 'passage_artifact_id' not in missed
    assert record(missed['research_id'])['sources'][0]['read_errors']


def test_unlocatable_provider_citation_remains_a_lead_without_an_invented_occurrence(ops, monkeypatch):
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([case_result(citation='123 US 456')], None))
    result = research.lookup(ops, lookup_args('The wrapped citation 123\nU.S. 456 is in this paragraph.'))
    authority = record(result['research_id'])['authorities'][0]
    assert authority['occurrences'] == []
    assert authority['provider_outcomes'][0]['citation'] == '123 US 456'
    assert authority['candidates'][0]['case_name'] == 'Smith v. Jones'


def test_standalone_search_can_start_without_any_invented_case(ops, monkeypatch):
    initial = research.lookup(ops, lookup_args('Find authorities addressing the legal research question.'))
    assert initial['authority_count'] == 0
    monkeypatch.setattr(sources, 'search_sources', lambda *a, **kw: {'results': [web_result()], 'error': None})
    found = research.search(ops, SimpleNamespace(research_id=initial['research_id'], authority_ids=[],
        query='public legal question', provider='web'))
    assert found['source_count'] == 1 and found['authority_count'] == 0
    with pytest.raises(ValueError, match='Authority IDs'):
        research.search(ops, SimpleNamespace(research_id=found['research_id'], authority_ids=['invented'],
            query='public legal question', provider='web'))


def test_empty_end_of_source_is_not_marked_as_a_read_passage(ops, monkeypatch):
    rid, sid = add_web_source(ops, monkeypatch)
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: {'status': 'read', 'text': 'Short source.'})
    result = research.read(ops, read_args(rid, sid, start=len('Short source.')))
    assert result['end_of_source'] and 'passage_artifact_id' not in result
    saved = record(result['research_id'])
    assert saved['sources'][0]['passage_artifact_ids'] == []


def test_tools_run_through_controller_and_analysis_links_a_new_snapshot(agent, monkeypatch):
    from backend.ai import base
    from tests.test_agent_controller import operation, last_result
    monkeypatch.setattr(research, '_discover_mentions', lambda *a, **kw: [])
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **kw: ([case_result()], None))
    operation(agent, monkeypatch, 'lookup_authorities', {
        'targets': [{'file_id': None, 'draft_id': None, 'text': CITATION, 'start': None, 'end': None}],
        'purpose': 'Research the cited decision.', 'research_id': None})
    initial = last_result(agent)
    assert initial['kind'] == 'authority_research' and initial['authority_count'] == 1
    monkeypatch.setattr(base, 'chat_completion', lambda *a, **kw: 'The database identifies a candidate; substantive support is not assessed.')
    operation(agent, monkeypatch, 'analyze_material', {'instructions': 'Assess identity observations only.',
        'material_ids': [initial['research_id']]})
    analyzed = last_result(agent)
    assert len(analyzed['research_ids']) == 1
    updated = record(analyzed['research_ids'][0])
    assert updated['parent_research_id'] == initial['research_id']
    assert updated['assessments'][0]['artifact_id'] == analyzed['artifact_id']
