"""Offline publication association and immutable cache enrichment, without model verdicts."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
import requests

from backend.ai import agent_tools
from backend.database import db
from backend.domains.legal import authority_research as research, authority_sources as sources, published_pages as pages
from backend.models import AgentArtifact
from tests.test_authority_research import ops, lookup_args, read_args, case_result, record
from tests.test_document_preparation import app
from tests.test_agent_controller import agent

FIXTURES = Path(__file__).parent / 'fixtures/authority-pinpoints'
CITATION = '450 F.3d 1132'


def provider():
    html = (FIXTURES / 'world-wide-cap.html').read_text()
    info = json.loads((FIXTURES / 'world-wide-provenance.json').read_text())
    assert hashlib.sha256(html.encode()).hexdigest() == info['html_sha256']
    cluster = json.loads((FIXTURES / 'world-wide-cluster.json').read_text())
    return html, cluster, info


def fetched():
    html, cluster, _ = provider()
    parsed = pages.inspect_html(html, [CITATION, '2006 WL 1617318'], field='html_with_citations')
    return {'status': 'read', **parsed, 'cluster_id': cluster['id'], 'opinion_id': 167625,
        'sha256': hashlib.sha256(html.encode()).hexdigest(),
        'retrieval_url': sources.API_BASE + 'opinions/167625/',
        'blocks': [{'start': 0, 'end': len(parsed['text']), 'location': 'CourtListener opinion 167625'}]}


def resolve(value, cluster):
    return pages.resolve_cap_publication(value, cluster,
        metadata_url=sources.API_BASE + 'clusters/167625/', metadata_sha256='metadata-response-digest')


def setup_read(ops, monkeypatch, *, metadata=False, value=None):
    _, cluster, _ = provider()
    result = case_result(cluster_id=167625, citation=CITATION)
    result['clusters'][0]['sub_opinions'] = cluster['sub_opinions']
    if metadata:
        result['clusters'][0].update(cluster)
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **k: ([result], None))
    found = research.lookup(ops, lookup_args(CITATION))
    calls = []
    def fetch(*a, **k):
        calls.append('body')
        return copy.deepcopy(value if value is not None else fetched())
    def api(method, path, **options):
        calls.append(path)
        assert method == 'GET' and path == 'clusters/167625/'
        return copy.deepcopy(cluster), 'metadata-response-digest'
    monkeypatch.setattr(sources, 'fetch_source', fetch)
    monkeypatch.setattr(sources, '_api_json', api)
    args = read_args(found['research_id'], found['sources'][0]['id'], query='district court')
    return args, calls


def test_world_wide_exact_publication_not_citation_order():
    value = fetched()
    html, cluster, _ = provider()
    assert value['text'] == sources._html_text(html)[1]
    assert value['unassociated_page_ranges'] and not value['published_page_ranges']
    assert all('citation' not in p for p in value['unassociated_page_ranges'])
    original = copy.deepcopy(value)
    for citations in [cluster['citations'], list(reversed(cluster['citations']))]:
        result = resolve(value, {**cluster, 'citations': citations})
        assert result['pagination_resolution']['status'] == 'resolved'
        assert {p['citation'] for p in result['published_page_ranges']} == {CITATION}
        assert {p['page'] for p in result['published_page_ranges']} >= {'1138', '1139'}
        assert result['publication_source']['pagination_identifier'] == '1'
        assert result['publication_source']['sha256'] == 'metadata-response-digest'
        for old, new in zip(value['unassociated_page_ranges'], result['published_page_ranges']):
            assert {k:v for k,v in new.items() if k != 'citation'} == old
    assert value == original


@pytest.mark.parametrize('change', ['cluster', 'opinion', 'locator', 'page', 'reporter', 'ambiguous', 'stream', 'header', 'boundaries', 'malformed'])
def test_unsupported_metadata_remains_unverified(change):
    value = fetched()
    _, cluster, _ = provider()
    if change == 'cluster': cluster['id'] = 999
    if change == 'opinion': cluster['sub_opinions'] = [999]
    if change == 'locator': cluster['filepath_json_harvard'] = ''
    if change == 'page': cluster['citations'][0]['page'] = '1133'
    if change == 'reporter': cluster['citations'][0]['reporter'] = 'F.2d'
    if change == 'ambiguous': cluster['citations'].append({'volume':450, 'reporter':'F3d', 'page':'1132'})
    if change == 'stream': value['pagination_streams'] = ['2']
    if change == 'header': value['pagination_headers'] = ['999 F.2d 111']
    if change == 'boundaries': value['unassociated_page_ranges'] = []
    if change == 'malformed': cluster['citations'] = 42
    result = resolve(value, cluster)
    assert result['pagination_resolution']['status'] == 'unverified'
    assert not result['published_page_ranges'] and result['publication_source'] is None


def test_duplicate_structured_citation_is_not_a_distinct_publication():
    _, cluster, _ = provider()
    cluster['citations'].append(copy.deepcopy(cluster['citations'][0]))
    assert resolve(fetched(), cluster)['pagination_resolution']['status'] == 'resolved'


def test_default_then_explicit_enrichment_replaces_aliases_and_survives_reload(ops, monkeypatch):
    args, calls = setup_read(ops, monkeypatch)
    first = research.read(ops, args)
    assert calls == ['body'] and first['page_evidence'] == []
    old_cache_id = first['source_artifact_id']
    old_cache = db.session.get(AgentArtifact, old_cache_id).content_json
    old_snapshot = db.session.get(AgentArtifact, first['research_id']).content_json
    # Historical snapshots may contain a second selection alias for the same body.
    snapshot = copy.deepcopy(record(first['research_id']))
    src = snapshot['sources'][0]
    src['cached_sources'].append({'artifact_id':old_cache_id, 'requested_opinion_id':167625, 'opinion_id':167625})
    aliased = ops.save(research.RESEARCH_KIND, snapshot)
    args.research_id = aliased
    args.resolve_page_metadata = True
    second = research.read(ops, args)
    assert calls == ['body', 'clusters/167625/']
    new_id = second['source_artifact_id']
    assert new_id != old_cache_id and second['page_evidence']
    for key in ('text', 'match_start', 'match_end', 'start', 'end', 'next_start', 'locations'):
        assert second[key] == first[key]
    cache = record(new_id)
    assert cache['parent_cache_artifact_id'] == old_cache_id
    for key in ('text', 'blocks', 'sha256'):
        assert cache[key] == json.loads(old_cache)[key]
    assert {c['artifact_id'] for c in record(second['research_id'])['sources'][0]['cached_sources']} == {new_id}
    assert record(second['research_id'])['sources'][0]['cache_artifact_id'] == new_id
    db.session.commit()
    db.session.expire_all()
    for flag in (False, True):
        args.research_id = second['research_id']
        args.resolve_page_metadata = flag
        second = research.read(ops, args)
        assert second['source_artifact_id'] == new_id
        assert record(second['passage_artifact_id'])['publication_source'] == second['publication_source']
        assert second['page_evidence'][0]['publication_source'] == second['publication_source']
    assert calls == ['body', 'clusters/167625/']
    assert db.session.get(AgentArtifact, old_cache_id).content_json == old_cache
    assert db.session.get(AgentArtifact, first['research_id']).content_json == old_snapshot
    assert record(aliased) == snapshot


def test_existing_metadata_resolves_without_extra_request(ops, monkeypatch):
    args, calls = setup_read(ops, monkeypatch, metadata=True)
    result = research.read(ops, args)
    assert calls == ['body'] and result['page_evidence']
    assert result['publication_source']['value'] == provider()[1]['filepath_json_harvard']


@pytest.mark.parametrize('transient', [False, True])
def test_metadata_failure_preserves_reading_and_controls_retries(ops, monkeypatch, transient):
    args, calls = setup_read(ops, monkeypatch)
    def api(*a, **k):
        calls.append('metadata')
        if transient: raise requests.Timeout('mock timeout')
        return {'id':167625, 'sub_opinions':[167625]}, 'missing-locator-digest'
    monkeypatch.setattr(sources, '_api_json', api)
    args.resolve_page_metadata = True
    result = research.read(ops, args)
    assert not result.get('error') and result['text'] and not result['page_evidence']
    assert result['pagination_resolution']['status'] == ('temporary_failure' if transient else 'unverified')
    assert 'does not prevent quotation' in result['location_note']
    for flag in (False, True):
        args.research_id = result['research_id']
        args.resolve_page_metadata = flag
        result = research.read(ops, args)
        assert result['text'] and not result.get('error')
        assert calls.count('metadata') == (2 if transient and flag else 1)
    assert calls.count('body') == 1


@pytest.mark.parametrize('kind', ['legacy', 'web', 'miss', 'exhausted', 'mapped'])
def test_no_page_only_request_when_not_needed(ops, monkeypatch, kind):
    value = fetched()
    if kind == 'legacy':
        for key in ('unassociated_page_ranges', 'pagination_streams'): value.pop(key)
    if kind == 'mapped': value.update(resolve(value, provider()[1]))
    args, calls = setup_read(ops, monkeypatch, value=value)
    if kind == 'web':
        snapshot = record(args.research_id)
        snapshot['sources'][0]['type'] = 'web'
        args.research_id = ops.save(research.RESEARCH_KIND, snapshot)
    if kind == 'miss': args.query = 'this phrase is absent'
    if kind == 'exhausted': args.query = None; args.start = len(value['text'])
    args.resolve_page_metadata = True
    result = research.read(ops, args)
    assert calls == ['body']
    if kind in ('miss', 'exhausted'):
        args.research_id = result['research_id']
        result = research.read(ops, args)
        assert calls == ['body']


def test_new_schema_and_legacy_normalization_do_not_rewrite_input():
    raw = {'research_id':'research', 'source_id':'source', 'opinion_id':None, 'query':None, 'start':0}
    original = copy.deepcopy(raw)
    assert agent_tools.arguments('read_authority', raw).resolve_page_metadata is False
    assert raw == original
    schema = agent_tools.ReadAuthority.model_json_schema()
    assert 'resolve_page_metadata' in schema['required']
    assert schema['properties']['resolve_page_metadata']['type'] == 'boolean'


def test_fetch_preserves_marked_html_when_plain_text_has_no_pages(monkeypatch):
    html, cluster, _ = provider()
    payload = {'id':167625, 'type':'010combined', 'plain_text':'unmarked plaintext', 'html_with_citations':html}
    monkeypatch.setattr(sources, '_api_json', lambda *a, **k: (payload, 'body-digest'))
    src = {'type':'courtlistener', 'cluster_id':167625, 'citations':[CITATION, '2006 WL 1617318'],
           'opinions':[{'id':167625}], 'title':'World Wide'}
    result = sources.fetch_source(src, opinion_id=167625)
    assert result['status'] == 'read' and result['text'] != payload['plain_text']
    assert result['unassociated_page_ranges'] and result['published_page_ranges'] == []


def test_metadata_cancellation_is_not_downgraded_to_a_pagination_failure(monkeypatch):
    from backend.util.workspace_service import WorkflowStopped
    def stopped(*a, **k):
        raise WorkflowStopped('cancelled')
    monkeypatch.setattr(sources, '_api_json', stopped)
    with pytest.raises(WorkflowStopped):
        sources.resolve_page_metadata(fetched(), {'type':'courtlistener','cluster_id':167625}, allow_request=True)


@pytest.mark.parametrize('tool', ['analyze_material','create_draft','revise_draft','verify_draft'])
def test_resolved_publication_reaches_next_controller_and_selected_materials(agent, monkeypatch, tool):
    from backend.ai import base
    from backend.util import document_drafts as drafts
    from tests.test_agent_controller import operation, last_result, call, tick
    _, cluster, _ = provider()
    raw = case_result(cluster_id=167625, citation=CITATION)
    raw['clusters'][0]['sub_opinions'] = cluster['sub_opinions']
    monkeypatch.setattr(research, '_discover_mentions', lambda *a, **k: [])
    monkeypatch.setattr(sources,'lookup_citations',lambda *a, **k: ([raw],None))
    operation(agent,monkeypatch,'lookup_authorities',{'targets':[{'file_id':None,'draft_id':None,
        'text':CITATION,'start':None,'end':None}],'mode':'overview','purpose':'Review the discussion.','research_id':None})
    found=last_result(agent)
    monkeypatch.setattr(sources,'fetch_source',lambda *a,**k: fetched())
    monkeypatch.setattr(sources,'_api_json',lambda *a,**k: (cluster,'metadata-digest'))
    operation(agent,monkeypatch,'read_authority',{'research_id':found['research_id'],
        'source_id':found['sources'][0]['id'],'opinion_id':None,'query':'district court','start':0,
        'resolve_page_metadata':True})
    observed=last_result(agent)
    assert observed['publication_source'] and observed['page_evidence']
    values={'instructions':'Assess wording and support; separately report what is known about the pinpoint.',
            'material_ids':[observed['passage_artifact_id']]}
    if tool in {'revise_draft','verify_draft'}: values['target']='working_draft'
    if tool=='revise_draft': values['revision_mode']='general_revision'
    if tool=='verify_draft': values['check_citations']=False
    def controller(request):
        outputs=[json.loads(x['output']) for x in request['input'] if x.get('type')=='function_call_output']
        assert outputs[-1]['publication_source']==observed['publication_source']
        assert outputs[-1]['page_evidence']==observed['page_evidence']
        return call(tool, values)
    calls=[]
    def check(materials):
        assert materials[0]['publication_source']==observed['publication_source']
        assert materials[0]['page_evidence']==observed['page_evidence']
        assert materials[0]['text']==observed['text']
        calls.append(1)
    def writer(prompt,schema,**options):
        check(json.loads(prompt)['materials'])
        if schema is drafts.MarkdownDraftResult:
            return schema(title='Review',markdown='Discussion assessed.',update_court_format=False)
        return schema(mode='patch',title=None,markdown=None,
            replacements=[{'old_text':'Original facts.','new_text':'Reviewed facts.'}],update_court_format=False)
    def analyst(prompt,**options):
        marker='\nSupporting materials:\n' if tool=='verify_draft' else '\nMaterials (source evidence and prior generated work are labeled):\n'
        check(json.loads(prompt.split(marker)[1]))
        return 'Discussion assessed.'
    monkeypatch.setattr(base,'responses_completion',controller)
    monkeypatch.setattr(base,'chat_completion_parse',writer)
    monkeypatch.setattr(base,'chat_completion',analyst)
    db.session.expire_all()
    tick(agent);tick(agent)
    assert not last_result(agent).get('error'),last_result(agent)
    assert calls==[1]
