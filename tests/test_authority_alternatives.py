"""Ordered authority phrase alternatives retain exact passages and offline provenance."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.ai import agent_tools
from backend.domains.legal import authority_research as research
from backend.domains.legal import authority_sources as sources
from tests.test_authority_research import add_web_source, ops, read_args, record
from tests.test_agent_controller import agent
from tests.test_document_preparation import app


def arguments(research_id, source_id, *, query=None, alternatives=(), start=0):
    return agent_tools.arguments('read_authority', {
        'research_id': research_id, 'source_id': source_id, 'opinion_id': None,
        'query': query, 'alternative_queries': list(alternatives), 'start': start})


def fetch_text(monkeypatch, body, **metadata):
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs.get('opinion_id'))
        return {'status': 'read', 'text': body, 'sha256': 'original-source-hash', **metadata}

    monkeypatch.setattr(sources, 'fetch_source', fetch)
    return calls


def observe_phrase_searches(monkeypatch):
    find = research._find_source_phrase
    calls = []

    def locate(body, query, start):
        calls.append((query, start))
        return find(body, query, start)

    monkeypatch.setattr(research, '_find_source_phrase', locate)
    return calls


def test_legacy_read_arguments_default_alternatives_without_changing_saved_call():
    saved = {'research_id': 'research', 'source_id': 'source',
             'opinion_id': None, 'query': 'Original phrase', 'start': 0}
    original = copy.deepcopy(saved)
    parsed = agent_tools.arguments('read_authority', saved)
    assert parsed.alternative_queries == []
    assert saved == original


@pytest.mark.parametrize('through_arguments', [True, False], ids=['schema', 'direct-reader'])
def test_page_reads_reject_alternatives_before_source_access(ops, monkeypatch, through_arguments):
    rid, sid = add_web_source(ops, monkeypatch)
    saved = {'research_id': rid, 'source_id': sid, 'opinion_id': None,
             'query': None, 'alternative_queries': ['some phrase'], 'start': 0}
    before = record(rid)
    if through_arguments:
        with pytest.raises(ValueError):
            agent_tools.arguments('read_authority', saved)
    else:
        with pytest.raises(ValueError):
            research.read(ops, SimpleNamespace(**saved))
    assert record(rid) == before


def test_original_phrase_has_priority_over_earlier_alternative_matches(ops, monkeypatch):
    rid, sid = add_web_source(ops, monkeypatch)
    body = 'An alternative phrase appears first.\n' + '.' * 1500 + 'The ORIGINAL\nphrase governs.'
    fetches = fetch_text(monkeypatch, body)
    searches = observe_phrase_searches(monkeypatch)
    result = research.read(ops, arguments(rid, sid, query='Original phrase',
        alternatives=['alternative phrase', 'governs']))
    assert result['query'] == result['matched_query'] == 'Original phrase'
    assert result['original_query_found'] is True and result['match_found'] is True
    position = body.index('ORIGINAL\nphrase')
    assert (result['match_start'], result['match_end']) == (position, position + len('ORIGINAL\nphrase'))
    assert result['text'] == body[result['start']:result['end']]
    assert searches == [('Original phrase', 0)] and fetches == [None]
    source = record(result['research_id'])['sources'][0]
    assert source['passage_artifact_ids'] == [result['passage_artifact_id']]
    assert len(source['cached_sources']) == 1


def test_ordered_alternatives_share_start_and_return_one_passage_then_allow_navigation(ops, monkeypatch):
    rid, sid = add_web_source(ops, monkeypatch)
    prefix = 'Irreparable harm before the requested offset.\n'
    heading = 'Irreparable\nharm'
    later = 'IRREPARABLE \n  harm'
    body = (prefix + 'An irreparable general concern.\n' + '.' * 1500 + heading
            + '\nContext. ' * 12 + later + '\nFurther reasoning. ' * 1100)
    start = len(prefix)
    primary = 'risk of irreparable injury'
    alternatives = ['a phrase absent from this source', 'irreparable harm', 'irreparable']
    fetches = fetch_text(monkeypatch, body,
        blocks=[{'start': 0, 'end': len(body), 'page': 7}])
    searches = observe_phrase_searches(monkeypatch)
    first = research.read(ops, arguments(rid, sid, query=primary,
        alternatives=alternatives, start=start))
    position = body.index(heading, start)
    assert first['query'] == primary and first['original_query_found'] is False
    assert first['matched_query'] == 'irreparable harm' and first['match_found'] is True
    assert (first['match_start'], first['match_end']) == (position, position + len(heading))
    assert first['text'] == body[first['start']:first['end']]
    assert searches == [(primary, start), (alternatives[0], start), (alternatives[1], start)]
    assert fetches == [None]
    source = record(first['research_id'])['sources'][0]
    assert source['passage_artifact_ids'] == [first['passage_artifact_id']]
    assert len(source['cached_sources']) == 1
    saved = record(first['passage_artifact_id'])
    for field in ('query', 'original_query_found', 'matched_query', 'match_start', 'match_end'):
        assert saved[field] == first[field]
    assert saved['locations'] == [{'start': first['start'], 'end': first['end'], 'page': 7}]
    assert saved['source_sha256'] == 'original-source-hash'

    second_position = body.index(later)
    assert first['match_end'] < second_position < first['next_start']
    second = research.read(ops, arguments(first['research_id'], sid,
        query=first['matched_query'], start=first['match_end']))
    assert second['original_query_found'] is True
    assert second['matched_query'] == second['query'] == 'irreparable harm'
    assert (second['match_start'], second['match_end']) == (second_position, second_position + len(later))
    assert body[second['match_start']:second['match_end']] == later
    assert second['source_artifact_id'] == first['source_artifact_id']

    page = research.read(ops, arguments(second['research_id'], sid, start=first['next_start']))
    assert page['text'] == body[first['next_start']:page['end']]
    assert page['start'] == first['next_start']
    assert page['original_query_found'] is None and page['matched_query'] is None
    assert page['match_start'] is None and page['match_end'] is None
    assert page['source_artifact_id'] == first['source_artifact_id']
    assert fetches == [None]


def test_all_phrases_missing_produces_no_passage_and_preserves_original_query(ops, monkeypatch):
    rid, sid = add_web_source(ops, monkeypatch)
    body = 'Earlier primary phrase.\nSome different source text.'
    start = len('Earlier primary phrase.\n')
    fetches = fetch_text(monkeypatch, body)
    searches = observe_phrase_searches(monkeypatch)
    query, alternatives = 'primary phrase', ['first absent phrase', 'second absent phrase']
    result = research.read(ops, arguments(rid, sid, query=query,
        alternatives=alternatives, start=start))
    assert result['query'] == query and result['match_found'] is False
    assert result['original_query_found'] is False and result['matched_query'] is None
    assert result['match_start'] is None and result['match_end'] is None
    assert 'passage_artifact_id' not in result and 'text' not in result
    assert searches == [(phrase, start) for phrase in [query, *alternatives]]
    assert fetches == [None]
    source = record(result['research_id'])['sources'][0]
    assert source['passage_artifact_ids'] == [] and not source.get('reads')


def test_legacy_plain_read_has_no_phrase_match_metadata(ops, monkeypatch):
    rid, sid = add_web_source(ops, monkeypatch)
    body = 'Original source page.'
    fetches = fetch_text(monkeypatch, body)
    searches = observe_phrase_searches(monkeypatch)
    result = research.read(ops, read_args(rid, sid))
    assert result['text'] == body and result['query'] is None
    assert result['match_found'] is True
    assert result['original_query_found'] is None and result['matched_query'] is None
    assert result['match_start'] is None and result['match_end'] is None
    saved = record(result['passage_artifact_id'])
    for field in ('query', 'original_query_found', 'matched_query'):
        assert saved[field] is None
    assert searches == [] and fetches == [None]


@pytest.mark.parametrize('outcome', ['unavailable', 'metadata', 'exhausted'])
def test_unread_outcomes_do_not_claim_any_phrase_was_searched(ops, monkeypatch, outcome):
    rid, sid = add_web_source(ops, monkeypatch)
    body = 'Complete source.'
    fetched = {
        'unavailable': {'status': 'unavailable', 'error': 'Source is unavailable.'},
        'metadata': {'status': 'metadata', 'opinions': [{'id': 7, 'type': 'lead-opinion'}]},
        'exhausted': {'status': 'read', 'text': body},
    }[outcome]
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **kw: fetched)
    searches = observe_phrase_searches(monkeypatch)
    result = research.read(ops, arguments(rid, sid, query='original phrase',
        alternatives=['another phrase'], start=len(body) if outcome == 'exhausted' else 0))
    assert result['original_query_found'] is None and result['matched_query'] is None
    assert result['match_start'] is None and result['match_end'] is None
    assert 'passage_artifact_id' not in result and 'match_found' not in result
    assert searches == []
    source = record(result['research_id'])['sources'][0]
    assert source['passage_artifact_ids'] == [] and not source.get('reads')
    if outcome == 'unavailable':
        assert result['error'] is True
    elif outcome == 'metadata':
        assert result['opinions'] == fetched['opinions']
    else:
        assert result['end_of_source'] is True and result['next_start'] is None


def test_alternatives_cannot_select_a_source_outside_the_research_snapshot(ops, monkeypatch):
    rid, sid = add_web_source(ops, monkeypatch)
    with pytest.raises(ValueError, match='Source ID must come from this research record'):
        research.read(ops, arguments(rid, sid + '-unselected', query='original', alternatives=['alternative']))


@pytest.mark.parametrize('field', ['user_id', 'chat_thread_id'])
def test_alternatives_preserve_research_ownership_checks(ops, monkeypatch, field):
    rid, sid = add_web_source(ops, monkeypatch)
    identity = {'user_id': ops.row.user_id, 'chat_thread_id': ops.row.chat_thread_id}
    identity[field] = identity[field] + 1 if field == 'user_id' else str(identity[field]) + '-other-chat'
    other = SimpleNamespace(**{**vars(ops), 'row': SimpleNamespace(**identity)})
    with pytest.raises(ValueError, match='Research artifact is unavailable in this chat'):
        research.read(other, arguments(rid, sid, query='original', alternatives=['alternative']))


def test_soundview_saved_source_replays_through_reader_without_changing_baseline(ops, monkeypatch):
    baseline = (Path(__file__).resolve().parents[1] / 'artifacts'
                / 'model-benchmark-openai-20261115-152018' / 'results' / 'evidence.json')
    if not baseline.is_file():
        pytest.skip('Private frozen model-benchmark evidence is not available.')
    frozen_bytes = baseline.read_bytes()
    digest = hashlib.sha256(frozen_bytes).hexdigest()
    assert digest == 'dcac18e053b0c0d70790a4f48e822d12c6d59c6d2005aa5b8ac83ecd22c79444'
    evidence = json.loads(frozen_bytes)
    run = next(item for item in evidence['runs']
               if item['id'] == 'wf_3118ad04-c75b-4859-9580-a4a127cb2d87')
    saved = next(item['content'] for item in run['artifacts']
                 if item['kind'] == research.SOURCE_KIND
                 and item['content'].get('role') == 'source_cache'
                 and item['content'].get('source_id') == 'c63dea02-09d1-4db0-9a16-a7ffafe5438a')
    original = copy.deepcopy(saved)
    body = saved['text']
    assert len(body) == 166_555
    assert hashlib.sha256(body.encode()).hexdigest() == '78fa7924cdfdc89aaec8ce9018b4090e12cdbad8c6ab94c462412b391fea5419'
    rid, sid = add_web_source(ops, monkeypatch, text='Read the saved Soundview source.')
    fetches = []

    def fetch(*args, **kwargs):
        fetches.append(kwargs.get('opinion_id'))
        return saved

    monkeypatch.setattr(sources, 'fetch_source', fetch)
    searches = observe_phrase_searches(monkeypatch)
    primary = 'risk of irreparable injury'
    result = research.read(ops, arguments(rid, sid, query=primary,
        alternatives=['irreparable harm', 'irreparable']))
    assert result['query'] == primary and result['original_query_found'] is False
    assert result['matched_query'] == 'irreparable harm' and result['match_found'] is True
    assert (result['match_start'], result['match_end']) == (123_753, 123_769)
    assert result['text'] == body[result['start']:result['end']]
    assert searches == [(primary, 0), ('irreparable harm', 0)] and fetches == [None]
    source = record(result['research_id'])['sources'][0]
    assert source['passage_artifact_ids'] == [result['passage_artifact_id']]
    assert len(source['cached_sources']) == 1
    passage = record(result['passage_artifact_id'])
    for field in ('query', 'original_query_found', 'matched_query', 'match_start', 'match_end'):
        assert passage[field] == result[field]
    assert passage['source_sha256'] == original['sha256']
    assert saved == original
    assert baseline.read_bytes() == frozen_bytes
    assert hashlib.sha256(baseline.read_bytes()).hexdigest() == digest
