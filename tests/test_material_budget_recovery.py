"""Lineage chaining, duplicate-material resolution, and recoverable budget errors."""
import copy
from types import SimpleNamespace

import pytest

from backend import agent_tasks as tasks
from backend.ai import agent_tools, base, token_budget
from backend.ai.token_budget import ContextBudgetExceeded, estimate_text_tokens
from backend.domains.legal import authority_research as research
from backend.domains.legal import authority_sources as sources
from backend.util import document_drafts as drafts
from tests.test_agent_controller import agent, choose, last_result, operation, tick
from tests.test_authority_research import lookup_args, ops, read_args, record, web_result
from tests.test_document_preparation import app
from tests.test_drafting_materials import source_material


# ---------------------------------------------------------------------------
# Lineage chaining
# ---------------------------------------------------------------------------

def test_reads_from_one_snapshot_continue_one_cumulative_descendant(ops, monkeypatch):
    base, source_id = add_web_source(ops, monkeypatch, 'Read the official rule.')
    monkeypatch.setattr(sources, 'fetch_source', lambda source, **kw: {
        'status': 'read', 'text': 'Alpha rule. Beta rule. Gamma rule.',
        'content_type': 'text/html', 'title': 'Rule'})
    first = research.read(ops, read_args(base, source_id, query='Alpha'))
    second = research.read(ops, read_args(base, source_id, query='Beta'))
    assert record(second['research_id'])['parent_research_id'] == first['research_id']
    assert research.lineage_head(ops, base, source_id=source_id) == second['research_id']
    saved = record(second['research_id'])
    passages = saved['sources'][0]['passage_artifact_ids']
    assert first['passage_artifact_id'] in passages
    assert second['passage_artifact_id'] in passages
    # The second read extended the first instead of branching from the parent.
    assert [row.id for row, _ in research._research_children(ops, base)] == [first['research_id']]


def test_search_extends_the_richest_descendant_instead_of_branching(ops, monkeypatch):
    initial = research.lookup(ops, lookup_args('Research a public rule.'))
    monkeypatch.setattr(sources, 'search_sources', lambda *a, **kw: {
        'results': [web_result('https://official.example/one')], 'error': None})
    first = research.search(ops, SimpleNamespace(research_id=initial['research_id'],
        authority_ids=[], query='First rule', provider='web'))
    monkeypatch.setattr(sources, 'search_sources', lambda *a, **kw: {
        'results': [web_result('https://official.example/two')], 'error': None})
    second = research.search(ops, SimpleNamespace(research_id=initial['research_id'],
        authority_ids=[], query='Second rule', provider='web'))
    assert record(second['research_id'])['parent_research_id'] == first['research_id']


def test_lineage_never_supplies_a_source_the_requested_snapshot_lacks(ops, monkeypatch):
    base, _ = add_web_source(ops, monkeypatch, 'Read the official rule.')
    monkeypatch.setattr(sources, 'search_sources', lambda *a, **kw: {
        'results': [web_result('https://official.example/later')], 'error': None})
    later = research.search(ops, SimpleNamespace(research_id=base, authority_ids=[],
        query='Later rule', provider='web'))
    with pytest.raises(ValueError, match='Source ID must come from'):
        research.read(ops, read_args(base, later['sources'][0]['id']))


# ---------------------------------------------------------------------------
# Material resolution
# ---------------------------------------------------------------------------

def add_web_source(ops, monkeypatch, text):
    initial = research.lookup(ops, lookup_args(text))
    monkeypatch.setattr(sources, 'search_sources', lambda *a, **kw: {
        'results': [web_result()], 'error': None})
    found = research.search(ops, SimpleNamespace(research_id=initial['research_id'],
        authority_ids=[item['id'] for item in initial['authorities']],
        query='Smith Jones official opinion', provider='web'))
    return found['research_id'], found['sources'][0]['id']


def research_material(uid, *, parent=None, reads=0, sources_count=0, purpose=None,
                      passages=()):
    sources = [{'id': f'source-{uid}-{index}', 'passage_artifact_ids': [],
                'reads': []} for index in range(sources_count)]
    if sources:
        sources[0]['passage_artifact_ids'] = list(passages)
        sources[0]['reads'] = [{'artifact_id': passage} for passage in passages][:reads]
    value = {'artifact_id': uid, 'kind': 'authority_research', 'parent_research_id': parent,
             'sources': sources, 'authorities': [], 'updated_at': '2026-01-01T00:00:00'}
    if purpose:
        value['use_in_this_operation'] = purpose
    return value


def passage_material(uid):
    return {'artifact_id': uid, 'kind': 'authority_source', 'role': 'passage', 'text': f'Passage {uid}.'}


def resolver(items):
    artifacts = {item['artifact_id']: item for item in items}
    ops = object.__new__(agent_tools.Operations)

    def artifact(uid, kind=None):
        if uid not in artifacts:
            raise ValueError('Artifact is unavailable.')
        return copy.deepcopy(artifacts[uid])
    ops.artifact = artifact
    return ops


def test_resolution_collapses_ancestors_and_keeps_distinct_passages():
    materials = [research_material('root', sources_count=1, passages=['old']),
                 research_material('child', parent='root', reads=1, sources_count=1, passages=['one']),
                 research_material('rich', parent='root', reads=2, sources_count=2, passages=['two', 'three'])]
    artifacts = [*materials, passage_material('old'), passage_material('one'),
                 passage_material('two'), passage_material('three')]
    resolved, summary = resolver(artifacts).resolve_material_set(copy.deepcopy(materials))
    assert [item['artifact_id'] for item in resolved] == ['rich', 'old', 'one']
    assert summary['deduplicated'][0]['kept'] == 'rich'
    assert {'old', 'one'} <= set(summary['passages_added'])


def test_resolution_collapses_sibling_branches_to_the_richest_and_merges_purposes():
    materials = [research_material('left', parent='root', reads=1, sources_count=1,
                                   passages=['left-passage'], purpose='Use the left branch.'),
                 research_material('right', parent='root', reads=2, sources_count=2,
                                   passages=['right-passage'], purpose='Use the right branch.')]
    artifacts = [research_material('root'), *materials,
                 passage_material('left-passage'), passage_material('right-passage')]
    resolved, summary = resolver(artifacts).resolve_material_set(copy.deepcopy(materials))
    assert [item['artifact_id'] for item in resolved] == ['right', 'left-passage']
    assert resolved[0]['use_in_this_operation'] == 'Use the right branch.\nUse the left branch.'
    assert summary['passages_added'] == ['left-passage']


def test_resolution_keeps_separate_research_roots_and_removes_duplicate_ids():
    materials = [research_material('first', sources_count=1),
                 research_material('first', sources_count=1),
                 research_material('second', sources_count=1)]
    resolved, summary = resolver(materials).resolve_material_set(copy.deepcopy(materials))
    assert [item['artifact_id'] for item in resolved] == ['first', 'second']
    assert summary['deduplicated'] == []
    assert summary['requested'] == ['first', 'first', 'second']


def test_resolution_does_not_add_an_already_selected_passage_twice():
    materials = [research_material('root', sources_count=1, passages=['shared']),
                 research_material('child', parent='root', reads=1, sources_count=1,
                                   passages=['other']),
                 passage_material('shared')]
    resolved, summary = resolver(materials).resolve_material_set(copy.deepcopy(materials))
    assert [item['artifact_id'] for item in resolved] == ['child', 'shared']
    assert summary['deduplicated'][0]['passages_added'] == ['shared']
    assert summary['passages_added'] == []


def test_resolution_is_a_noop_without_research_snapshots():
    materials = [{'artifact_id': 'draft', 'kind': 'draft'}]
    resolved, summary = resolver(materials).resolve_material_set(copy.deepcopy(materials))
    assert resolved == materials
    assert summary['supplied'] == ['draft'] and not summary['deduplicated']


def test_failed_benchmark_topology_now_resolves_to_one_snapshot_plus_passages():
    def snapshot(uid, *, reads, count, passages, parent=None):
        sources = [{'id': f'{uid}-{index}', 'title': 'Smith v. Jones official opinion',
                    'snippet': 'Retrieved source metadata for a cited authority. ' * 8,
                    'passage_artifact_ids': [], 'reads': []} for index in range(count)]
        for index, passage in enumerate(passages):
            sources[index]['passage_artifact_ids'] = [passage]
            sources[index]['reads'] = [{'artifact_id': passage}]
        return {'artifact_id': uid, 'kind': 'authority_research', 'parent_research_id': parent,
                'sources': sources, 'authorities': [], 'updated_at': '2026-01-01T00:00:00'}

    parent = snapshot('parent', reads=0, count=146, passages=[])
    siblings = [snapshot(name, reads=1, count=146, passages=[f'{name}-passage'], parent='parent')
                for name in ('a', 'b', 'c')]
    richer = snapshot('richer', reads=2, count=169, passages=['richer-passage'], parent='parent')
    small = [snapshot('small-' + name, reads=0, count=4, passages=[]) for name in ('x', 'y')]
    materials = [parent, *siblings, richer, *small]
    passages = [passage_material(f'{name}-passage') for name in ('a', 'b', 'c', 'richer')]
    resolved, summary = resolver([*materials, *passages]).resolve_material_set(copy.deepcopy(materials))
    assert [item['kind'] for item in resolved].count('authority_research') == 3
    assert {item['artifact_id'] for item in resolved} >= {'richer', 'small-x', 'small-y'}
    assert {entry['kept'] for entry in summary['deduplicated']} == {'richer'}
    assert estimate_text_tokens(resolved) * 3 < estimate_text_tokens(materials)


# ---------------------------------------------------------------------------
# Budget errors
# ---------------------------------------------------------------------------

def test_preflight_budget_error_carries_the_measured_request():
    request = {'model': 'gpt-5.4-mini', 'input': [{'role': 'user', 'content': [
        {'type': 'input_text', 'text': 'material ' * 300_000}]}]}
    with pytest.raises(ContextBudgetExceeded) as raised:
        token_budget.validate_text_request(request)
    assert raised.value.tokens > raised.value.budget
    assert raised.value.budget == token_budget.usable_input_budget(request)
    assert estimate_text_tokens('material') > 0


def test_draft_budget_failure_becomes_a_recoverable_observation(agent, monkeypatch):
    def fail(**kwargs):
        raise ContextBudgetExceeded('Too much material.', tokens=1_271_273, budget=919_952)
    monkeypatch.setattr(drafts, 'write_document', fail)
    operation(agent, monkeypatch, 'create_draft', {'instructions': 'Draft.', 'material_ids': []})
    result = last_result(agent)
    assert result['error'] is True and result['error_code'] == 'context_budget'
    assert result['request_tokens'] == 1_271_273 and result['input_allowance'] == 919_952
    assert result['material_tokens'] == 0 and result['materials'] == []
    assert agent.status == 'running'


def test_analysis_budget_failure_names_each_material(agent, monkeypatch):
    material = source_material(agent, monkeypatch)

    def fail(*args, **kwargs):
        raise ContextBudgetExceeded('Too much.')
    monkeypatch.setattr(base, 'chat_completion', fail)
    operation(agent, monkeypatch, 'analyze_material',
              {'instructions': 'Analyze.', 'material_ids': [material]})
    result = last_result(agent)
    assert result['error_code'] == 'context_budget'
    assert result['materials'][0]['artifact_id'] == material
    assert result['materials'][0]['tokens'] > 0
    assert result['by_kind'] == {'source': result['materials'][0]['tokens']}
    assert 'Repeating the same material_ids will fail again' in result['message']


def test_verification_budget_failure_is_recoverable_too(agent, monkeypatch):
    material = source_material(agent, monkeypatch)

    def fail(*args, **kwargs):
        raise ContextBudgetExceeded('Too much.')
    monkeypatch.setattr(base, 'chat_completion', fail)
    operation(agent, monkeypatch, 'verify_draft', {
        'target': 'current_draft', 'instructions': 'Check.', 'check_citations': False,
        'material_ids': [material]})
    result = last_result(agent)
    assert result['error_code'] == 'context_budget'
    assert result['material_tokens'] > 0


def test_irreducible_draft_budget_failure_still_stops_the_run(agent, monkeypatch):
    def fail(**kwargs):
        raise ContextBudgetExceeded('The draft alone is too large.', can_reduce_evidence=False)
    monkeypatch.setattr(drafts, 'write_document', fail)
    choose(monkeypatch, 'create_draft', {'instructions': 'Draft.', 'material_ids': []})
    assert tick(agent) == {'continued': True}
    assert tick(agent) == {'stopped': True}
    assert agent.status in ('cancelled', 'failed')


def test_publication_budget_failure_is_still_fatal(agent, monkeypatch):
    def fail(*args, **kwargs):
        raise ContextBudgetExceeded('Too large.')
    monkeypatch.setattr(tasks, 'publish', fail)
    choose(monkeypatch, 'complete_task', {'response': 'Done', 'candidate_id': None})
    assert tick(agent) == {'continued': True}
    assert tick(agent) == {'stopped': True}
    assert agent.status in ('cancelled', 'failed')
