"""A saved external-source cache survives selection of another research snapshot."""
import copy
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from backend.database import db
from backend.models import AgentArtifact
from backend.domains.legal import authority_research as research, authority_sources as sources
from backend.util import workspace_service as service
from tests.test_agent_controller import agent
from tests.test_document_preparation import app
from tests.test_authority_research import ops, record, read_args, step_ops, lookup_args, case_result, add_web_source
from tests.test_page_metadata import setup_read, fetched, resolve, provider


def test_sibling_snapshot_reuses_text_offsets_and_page_evidence_after_reload(ops, monkeypatch):
    value=fetched();value.update(resolve(value,provider()[1]))
    args,calls=setup_read(ops,monkeypatch,value=value)
    base=args.research_id
    first=research.read(ops,args)
    # A newer snapshot can still derive from history without the completed read.
    sibling=research._persist(ops,record(base),base)
    preserved={uid:db.session.get(AgentArtifact,uid).content_json
               for uid in (base,first['research_id'],first['source_artifact_id'],sibling)}
    db.session.commit();db.session.expire_all()
    args.research_id=sibling
    second=research.read(ops,args)
    assert calls==['body']
    assert second['source_artifact_id']==first['source_artifact_id']
    assert second['passage_artifact_id']!=first['passage_artifact_id']
    for key in ('text','match_start','match_end','start','end','next_start','locations','page_evidence','publication_source'):
        assert second[key]==first[key]
    src=record(second['research_id'])['sources'][0]
    assert src['cache_artifact_id']==first['source_artifact_id']
    assert src['cached_sources']==[{'artifact_id':first['source_artifact_id'],
        'opinion_id':167625,'requested_opinion_id':None}]
    assert all(db.session.get(AgentArtifact,uid).content_json==body for uid,body in preserved.items())
    assert len([a for a in AgentArtifact.query.filter_by(kind=research.SOURCE_KIND)
                if json.loads(a.content_json).get('role')=='source_cache'])==1


def test_same_chat_next_request_can_reuse_cache_without_copying_it(ops,monkeypatch):
    args,calls=setup_read(ops,monkeypatch)
    base=args.research_id;first=research.read(ops,args)
    data=json.loads(ops.row.data_json)
    ops.row.status='completed';ops.row.active_draft_id=None
    db.session.commit()
    data.pop('run_id',None);data['query']='Continue checking the same opinion.'
    next_ops=step_ops(service.submit(ops.row.user_id,data))
    args.research_id=base
    second=research.read(next_ops,args)
    assert calls==['body'] and second['source_artifact_id']==first['source_artifact_id']
    assert db.session.get(AgentArtifact,second['passage_artifact_id']).run_id==next_ops.row.id
    assert db.session.get(AgentArtifact,first['source_artifact_id']).run_id==ops.row.id


def test_newest_cache_enrichment_wins_over_newer_snapshot_with_older_pointer(ops,monkeypatch):
    args,calls=setup_read(ops,monkeypatch)
    base=args.research_id;first=research.read(ops,args)
    args.research_id=first['research_id'];args.resolve_page_metadata=True
    enriched=research.read(ops,args)
    assert enriched['page_evidence']
    late_old=research._persist(ops,record(first['research_id']),first['research_id'])
    args.resolve_page_metadata=False;args.research_id=base
    reused=research.read(ops,args)
    assert reused['source_artifact_id']==enriched['source_artifact_id']
    assert reused['page_evidence']==enriched['page_evidence']
    assert calls==['body','clusters/167625/']
    # A pointer already present in the selected snapshot retains its normal meaning.
    args.research_id=late_old
    explicit_old=research.read(ops,args)
    assert explicit_old['source_artifact_id']==first['source_artifact_id']
    assert explicit_old['page_evidence']==[]
    assert calls==['body','clusters/167625/']


@pytest.mark.parametrize('first_opinion,second_opinion',[(None,167625),(167625,None)])
def test_sole_opinion_reuses_exact_selection_alias(ops,monkeypatch,first_opinion,second_opinion):
    args,calls=setup_read(ops,monkeypatch)
    base=args.research_id;args.opinion_id=first_opinion
    first=research.read(ops,args)
    args.research_id=base;args.opinion_id=second_opinion
    second=research.read(ops,args)
    assert calls==['body'] and second['source_artifact_id']==first['source_artifact_id']


@pytest.mark.parametrize('requested',[None,999,167626])
def test_fallback_does_not_choose_another_or_ambiguous_opinion(ops,monkeypatch,requested):
    args,calls=setup_read(ops,monkeypatch)
    snap=record(args.research_id);snap['sources'][0]['opinions']=[{'id':167625},{'id':167626}]
    base=research._persist(ops,snap,args.research_id)
    args.research_id=base;args.opinion_id=167625
    research.read(ops,args)
    def fetch(source,*,opinion_id,**kwargs):
        calls.append(opinion_id)
        if opinion_id is None:return {'status':'metadata','opinions':[{'id':167625},{'id':167626}]}
        if opinion_id==999:return {'status':'invalid_selection','requested_opinion_id':999,
            'opinions':[{'id':167625},{'id':167626}],'error':'Choose a supplied opinion.'}
        return {**fetched(),'opinion_id':167626,'text':'Other opinion text.'}
    monkeypatch.setattr(sources,'fetch_source',fetch)
    args.research_id=base;args.opinion_id=requested;args.query=None
    result=research.read(ops,args)
    assert calls==['body',requested]
    if requested==167626:assert result['text']=='Other opinion text.'
    else:assert 'passage_artifact_id' not in result


def test_web_pdf_source_reuses_only_its_saved_body(ops,monkeypatch):
    base,sid=add_web_source(ops,monkeypatch)
    calls=[]
    def fetch(*a,**k):
        calls.append(1)
        return {'status':'read','text':'Full original PDF text.','opinion_id':None,
            'blocks':[{'start':0,'end':23,'page':7,'location':'PDF page 7'}]}
    monkeypatch.setattr(sources,'fetch_source',fetch)
    first=research.read(ops,read_args(base,sid))
    second=research.read(ops,read_args(base,sid))
    assert calls==[1] and second['source_artifact_id']==first['source_artifact_id']
    assert second['locations']==first['locations'] and not second['page_evidence']


@pytest.mark.parametrize('corrupt',['role','source_id','opinion_id','cluster_id','blocked','empty','identity'])
def test_mismatched_or_unusable_cache_is_not_reused(ops,monkeypatch,corrupt):
    args,calls=setup_read(ops,monkeypatch)
    base=args.research_id;first=research.read(ops,args)
    row=db.session.get(AgentArtifact,first['source_artifact_id']);value=json.loads(row.content_json)
    if corrupt=='role':value['role']='passage'
    if corrupt=='source_id':value['source_id']='another-source'
    if corrupt=='opinion_id':value['opinion_id']=999
    if corrupt=='cluster_id':value['cluster_id']=999
    if corrupt=='blocked':value.update(content_type='text/html',title='Request access',text='Please complete the CAPTCHA.')
    if corrupt=='empty':value['text']=''
    if corrupt=='identity':
        peer=db.session.get(AgentArtifact,first['research_id']);recorded=json.loads(peer.content_json)
        recorded['sources'][0]['identity_key']='courtlistener:999';peer.content_json=json.dumps(recorded)
    row.content_json=json.dumps(value);db.session.commit()
    args.research_id=base
    result=research.read(ops,args)
    assert calls==['body','body']
    assert result['source_artifact_id']!=first['source_artifact_id']


def clone_workflow(row,**changes):
    values={column.name:getattr(row,column.name) for column in row.__table__.columns}
    values.update(id='cache-test-'+str(uuid.uuid4()),active_chat_thread_id=None,status='completed',**changes)
    cloned=service.Workflow(**values);db.session.add(cloned);db.session.commit();return cloned


@pytest.mark.parametrize('scope',['other_user','other_chat','stale_snapshot'])
@pytest.mark.parametrize('move_reference',[False,True])
def test_candidate_authorization_and_snapshot_validation_remain_required(ops,monkeypatch,scope,move_reference):
    args,calls=setup_read(ops,monkeypatch)
    base=args.research_id;first=research.read(ops,args)
    changes={}
    if scope=='other_user':changes['user_id']=2
    if scope=='other_chat':changes['chat_thread_id']=2
    if scope=='stale_snapshot':
        data=json.loads(ops.row.data_json);data['document_snapshot']['files'][0]['source_hash']='changed'
        changes['data_json']=json.dumps(data)
    foreign=clone_workflow(ops.row,**changes)
    db.session.get(AgentArtifact,first['source_artifact_id']).run_id=foreign.id
    if move_reference:db.session.get(AgentArtifact,first['research_id']).run_id=foreign.id
    db.session.commit();args.research_id=base
    result=research.read(ops,args)
    assert calls==['body','body'] and result['source_artifact_id']!=first['source_artifact_id']


def test_frozen_pure_snapshot_replay_reuses_recorded_page_evidence(ops,monkeypatch):
    path=Path(__file__).resolve().parents[1]/'artifacts/model-benchmark-openai-latest-review-word-count-20261118-103618/results/evidence.json'
    if not path.exists():pytest.skip('Frozen private benchmark is local evaluation material.')
    digest=hashlib.sha256(path.read_bytes()).hexdigest();e=json.loads(path.read_text())
    artifacts={a['id']:a['content'] for r in e['runs'] for a in r['artifacts']}
    current=copy.deepcopy(artifacts['b8d20412-4007-4e95-ab77-c9e410b0ce6c'])
    cached=copy.deepcopy(artifacts['e7a09004-f91f-45c6-a53b-56b0e0e697f7'])
    previous=copy.deepcopy(artifacts['7dcb18ad-7686-4434-b230-27cbfc3c2eb4'])
    cache_id=ops.save(research.SOURCE_KIND,cached)
    src=next(s for s in previous['sources'] if s['id']==cached['source_id'])
    src['cached_sources']=[{'artifact_id':cache_id,'requested_opinion_id':167625,'opinion_id':167625}]
    ops.save(research.RESEARCH_KIND,previous)
    current_id=ops.save(research.RESEARCH_KIND,current)
    result=research.read(ops,read_args(current_id,cached['source_id'],opinion_id=167625,query='Rule 405(b)'))
    assert result['source_artifact_id']==cache_id
    assert result['matched_published_pages']==['1138']
    assert result['page_evidence'] and result['publication_source']==cached['publication_source']
    assert result['text']==cached['text'][result['start']:result['end']]
    assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
