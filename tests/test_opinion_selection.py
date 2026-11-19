"""Opinion choices and recorded Perrin recovery, with offline provider boundaries."""
import copy
import json

import pytest

from backend import agent_tasks
from backend.ai import agent_tools, base
from backend.database import db
from backend.models import AgentArtifact
from backend.domains.legal import authority_research as research, authority_sources as sources
from tests.test_authority_sources import api, public_web, Response, _pdf
from tests.test_authority_research import ops, lookup_args, read_args, case_result, record
from tests.test_agent_controller import agent, operation, last_result, call, tick
from tests.test_document_preparation import app

CASE_ID, OPINION_ID = 8948136, 8938823
URL = sources.API_BASE + f'opinions/{OPINION_ID}/'
CITATION = '784 F.2d 1040'
# Synthetic body: tests exercise selection and information delivery, not case law.
BODY = 'Test opinion: character is an essential element. Further test text.'


def perrin():
    value = case_result(cluster_id=CASE_ID, name='Perrin v. Anderson', citation=CITATION)
    value['clusters'][0]['sub_opinions'] = [URL]
    return value


def case_source():
    return {'type':'courtlistener', 'cluster_id':CASE_ID, 'opinions':[{'url':URL}]}


def inventory(*ids):
    return Response({'results':[{'id':i, 'type':'020lead' if n==0 else '040dissent'}
                                for n,i in enumerate(ids)], 'next':None})


def opinion():
    return Response({'id':OPINION_ID, 'cluster':CASE_ID, 'plain_text':BODY})


@pytest.mark.parametrize('value', [OPINION_ID, str(OPINION_ID), URL,
    {'id':OPINION_ID}, {'opinion_id':OPINION_ID}, {'url':URL},
    {'url':URL.replace('/v4/','/v3/')}, {'url':f'/api/rest/v4/opinions/{OPINION_ID}/'}])
def test_normalize_existing_reference_formats_without_mutating_input(value):
    original = copy.deepcopy(value)
    result = sources.opinion_choices([value])[0]
    assert result['opinion_id'] == result['id'] == OPINION_ID
    assert result['url'] == URL and value == original


@pytest.mark.parametrize('url', [
    f'https://www.courtlistener.com/opinion/{CASE_ID}/perrin-v-anderson/',
    f'https://other.example/api/rest/v4/opinions/{OPINION_ID}/',
    f'https://user:password@www.courtlistener.com/api/rest/v4/opinions/{OPINION_ID}/',
    'https://www.courtlistener.com/api/rest/v4/clusters/8948136/', 'not-an-opinion',
])
def test_case_permalinks_and_untrusted_urls_are_not_opinion_ids(url):
    assert sources.opinion_choices([{'url':url}]) == []


def test_duplicate_metadata_preserves_choice_and_fills_missing_details():
    values=[{'url':URL}, {'id':OPINION_ID, 'type':'020lead', 'author_str':'Named author'},
            {'opinion_id':OPINION_ID, 'type':'040dissent'}]
    original=copy.deepcopy(values)
    result=sources.opinion_choices(values)
    assert len(result)==1 and result[0]['type']=='020lead' and result[0]['author']=='Named author'
    assert values==original


def test_single_opinion_auto_selection_waits_for_complete_inventory(api):
    queue,seen=api
    queue.extend([Response({'results':[{'id':OPINION_ID}],
        'next':sources.API_BASE+'opinions/?cursor=second'}),
        Response({'results':[{'id':OPINION_ID+1, 'type':'040dissent'}], 'next':None})])
    result=sources.fetch_source(case_source(), opinion_id=None)
    assert result['status']=='metadata'
    assert [v['opinion_id'] for v in result['opinions']]==[OPINION_ID,OPINION_ID+1]
    assert len(seen)==2 and all(url.endswith('/opinions/') for _,url,_ in seen)


def test_single_opinion_null_selects_actual_opinion_without_llm_id(api):
    queue,seen=api;queue.extend([inventory(OPINION_ID),opinion()])
    result=sources.fetch_source(case_source(), opinion_id=None)
    assert result['status']=='read' and result['opinion_id']==OPINION_ID
    assert result['text']==BODY
    assert [url for _,url,_ in seen]==[sources.API_BASE+'opinions/',URL]


@pytest.mark.parametrize('wrong', [CASE_ID,0,-1])
def test_invalid_choice_returns_all_choices_without_reading_or_redirecting(api,wrong):
    queue,seen=api;queue.append(inventory(OPINION_ID))
    result=sources.fetch_source(case_source(), opinion_id=wrong)
    assert result['status']=='invalid_selection' and result['error_code']=='invalid_opinion_id'
    assert result['requested_opinion_id']==wrong
    assert result['opinions'][0]['opinion_id']==OPINION_ID
    assert result['text']=='' and len(seen)==1
    assert 'automatic selection' in result['error']


def test_url_only_choice_can_be_selected_without_another_inventory_call(api):
    api[0].append(opinion())
    result=sources.fetch_source(case_source(), opinion_id=OPINION_ID)
    assert result['status']=='read' and result['opinion_id']==OPINION_ID
    assert len(api[1])==1 and api[1][0][1]==URL


def test_actual_inventory_failure_stays_a_source_failure(api):
    api[0].append(Response(status=429))
    result=sources.fetch_source(case_source(), opinion_id=CASE_ID)
    assert result['status']=='unavailable' and result['error_code']=='source_unavailable'
    assert '429' in result['error'] and 'opinions' not in result


@pytest.mark.parametrize('body,content_type', [
    (b'<html><body>Actual webpage text.</body></html>','text/html'),
    (_pdf('Actual PDF source text.'),'application/pdf')])
def test_web_and_pdf_sources_do_not_use_opinion_inventory(public_web,monkeypatch,body,content_type):
    public_web[0].append(Response(body,headers={'Content-Type':content_type}))
    monkeypatch.setattr(sources,'_api_json',lambda *a,**k:pytest.fail('Web/PDF used CourtListener'))
    result=sources.fetch_source({'type':'web','url':'https://court.example/source'},opinion_id=None)
    assert result['status']=='read' and result['opinion_id'] is None
    assert 'Actual' in result['text']


def test_lookup_and_search_return_explicit_numeric_opinion_choices(ops,monkeypatch):
    monkeypatch.setattr(sources,'lookup_citations',lambda *a,**k:([perrin()],None))
    found=research.lookup(ops,lookup_args(CITATION))
    choice=found['sources'][0]['opinions'][0]
    assert choice['opinion_id']==OPINION_ID and choice['url']==URL
    row=sources._case_result({'cluster_id':CASE_ID,'caseName':'Perrin','opinions':[{'url':URL}]})
    assert row['opinions'][0]['opinion_id']==OPINION_ID


def test_invalid_selection_saves_choices_without_read_errors_or_passages_then_recovers(ops,api,monkeypatch):
    monkeypatch.setattr(sources,'lookup_citations',lambda *a,**k:([perrin()],None))
    initial=research.lookup(ops,lookup_args(CITATION));sid=initial['sources'][0]['id']
    db.session.commit();original=db.session.get(AgentArtifact,initial['research_id']).content_json
    monkeypatch.setattr(sources,'fetch_source',REAL_FETCH)
    api[0].extend([inventory(OPINION_ID),opinion()])
    bad=research.read(ops,read_args(initial['research_id'],sid,opinion_id=CASE_ID,query='character'))
    assert bad['error_code']=='invalid_opinion_id' and bad['opinions'][0]['opinion_id']==OPINION_ID
    assert bad['research_id']!=initial['research_id'] and bad['source_id']==sid
    assert 'passage_artifact_id' not in bad and bad['match_start'] is None
    saved=record(bad['research_id']);source=saved['sources'][0]
    assert not source.get('read_errors') and not source.get('passage_artifact_ids') and not source.get('cached_sources')
    assert not saved['authorities'][0]['checking'].get('opinion_read')
    db.session.commit();db.session.expire_all()
    good=research.read(ops,read_args(bad['research_id'],sid,opinion_id=OPINION_ID,query='character'))
    assert good['opinion_id']==OPINION_ID and good['match_found']
    assert record(good['passage_artifact_id'])['opinion_id']==OPINION_ID
    assert db.session.get(AgentArtifact,initial['research_id']).content_json==original
    assert len(api[1])==2  # Invalid choice fetched metadata only; retry fetched the body.
    again=research.read(ops,read_args(good['research_id'],sid,opinion_id=OPINION_ID,query='element'))
    assert again['source_artifact_id']==good['source_artifact_id'] and len(api[1])==2


REAL_FETCH=sources.fetch_source


def test_historical_url_only_record_is_normalized_on_return_without_rewriting(ops,monkeypatch):
    monkeypatch.setattr(sources,'lookup_citations',lambda *a,**k:([perrin()],None))
    initial=research.lookup(ops,lookup_args(CITATION));source=record(initial['research_id'])['sources'][0]
    source['opinions']=[{'url':URL}];original=copy.deepcopy(source)
    ref=research._source_reference(source)
    assert ref['opinions'][0]['opinion_id']==OPINION_ID and source==original


def test_controller_receives_invalid_choice_options_and_can_retry(agent,api,monkeypatch):
    monkeypatch.setattr(research,'_discover_mentions',lambda *a,**k:[])
    monkeypatch.setattr(sources,'lookup_citations',lambda *a,**k:([perrin()],None))
    operation(agent,monkeypatch,'lookup_authorities',{'targets':[{'file_id':None,'draft_id':None,
        'text':CITATION,'start':None,'end':None}], 'mode':'overview','purpose':'Read the opinion.','research_id':None})
    initial=last_result(agent);sid=initial['sources'][0]['id']
    assert initial['sources'][0]['opinions'][0]['opinion_id']==OPINION_ID
    api[0].extend([inventory(OPINION_ID),opinion()])
    operation(agent,monkeypatch,'read_authority',{'research_id':initial['research_id'],'source_id':sid,
        'opinion_id':CASE_ID,'query':'character is an essential element','alternative_queries':[],'start':0})
    bad=last_result(agent);assert bad['error_code']=='invalid_opinion_id'
    db.session.expire_all()
    def decide(request):
        outputs=[json.loads(x['output']) for x in request['input'] if x.get('type')=='function_call_output']
        observation=outputs[-1]
        assert observation['error_code']=='invalid_opinion_id'
        assert observation['opinions'][0]['opinion_id']==OPINION_ID
        assert observation['research_id']==bad['research_id']
        return call('read_authority',{'research_id':observation['research_id'],'source_id':observation['source_id'],
            'opinion_id':observation['opinions'][0]['opinion_id'],'query':'character is an essential element',
            'alternative_queries':[],'start':0})
    monkeypatch.setattr(base,'responses_completion',decide)
    assert tick(agent)=={'continued':True} and tick(agent)=={'continued':True}
    result=last_result(agent)
    assert not result.get('error') and result['opinion_id']==OPINION_ID and result['text']==BODY


def test_tool_and_controller_guidance_describe_null_and_explicit_selection():
    definition=next(x for x in agent_tools.definitions() if x['name']=='read_authority')
    description=definition['parameters']['properties']['opinion_id']['description']
    assert 'Null initially' in description and 'never cluster_id' in description
    assert 'opinion_id=null' in agent_tasks.system_instructions()
    assert 'After invalid_opinion_id, retry its choices' in agent_tasks.system_instructions()
