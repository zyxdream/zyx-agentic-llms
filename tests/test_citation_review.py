"""Audit accounting uses real SQL state and mocked discovery/provider boundaries."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import agent_tasks as tasks
from backend.ai import agent_tools
from backend.database import db
from backend.models import AgentArtifact, AgentStep
from backend.domains.legal import authority_research as research, authority_sources as sources, citation_review as review
from backend.util import workspace_service as service
from tests.test_agent_controller import agent, operation, last_result, state, tick, choose
from tests.test_authority_research import ops, lookup_args, target, record, case_result, read_args
from tests.test_document_preparation import app


REAVES='649 F.3d 862'
OTHER='450 F.3d 1132'


def discover(text, purpose, **options):
    window=options['window'];found=[]
    for citation in [REAVES,OTHER]:
        positions=[];start=0
        while (position:=text.find(citation,start))>=0:
            positions.append(position);start=position+len(citation)
        for position in positions:
            original=window['start']+position
            unit=next(u for u in window['units'] if u['start']<=original<u['end'])
            if not window['core_start']<=original<window['core_end']:continue
            found.append({'kind':'case','citation_as_written':citation,'case_name':None,
                'context':text,'proposition':'The claim surrounding this source occurrence.',
                'unit_id':unit['id'],'occurrence_index':sum(1 for p in positions
                    if unit['start']<=window['start']+p<=original)})
    return found


def install(monkeypatch, raw=None):
    monkeypatch.setattr(research,'_discover_mentions',discover)
    monkeypatch.setattr(sources,'lookup_citations',lambda *a,**kw:(raw or [],None))


def audit(ops, monkeypatch, text=REAVES, *, parent=None, start=None, end=None, mode='audit'):
    install(monkeypatch)
    return research.lookup(ops,SimpleNamespace(targets=[target(text,start=start,end=end)],
        purpose='Review citations in the selected text.',research_id=parent,mode=mode))


def outcomes(items, *, evidence=None):
    return [{'review_item_id':item['review_item_id'],
        'status':'assessed' if evidence and not item.get('location_uncertain') else 'unresolved',
        'finding':'Source supports this recorded finding.' if evidence else 'Opinion/application not checked.',
        'evidence_ids':[evidence] if evidence else []} for item in items]


def args(results):
    return agent_tools.arguments('complete_task',{'response':'Review results.',
        'candidate_id':None,'citation_review_results':results})


def operation_audit(agent,monkeypatch,text=REAVES+'\n\n'+OTHER):
    install(monkeypatch)
    operation(agent,monkeypatch,'lookup_authorities',{'targets':[{
        'text':text,'file_id':None,'draft_id':None,'start':None,'end':None}],
        'mode':'audit','purpose':'Review each citation and its support.','research_id':None})
    result=last_result(agent)
    assert len(result['review_items'])==text.count(REAVES)+text.count(OTHER)
    return result


@pytest.mark.parametrize('assess_reaves',[False,True])
@pytest.mark.parametrize('with_candidate',[False,True])
def test_missing_reaves_blocks_publication_then_allows_assessed_or_unresolved(agent,monkeypatch,assess_reaves,with_candidate):
    if with_candidate:
        from backend.util import document_drafts
        monkeypatch.setattr(document_drafts,'write_document',lambda **kw: document_drafts.MarkdownDraftResult(
            title='Review',markdown='# Authored review\n\nKeep this document unchanged.',update_court_format=False))
        operation(agent,monkeypatch,'create_draft',{'instructions':'Prepare the review.','material_ids':[]})
    found=operation_audit(agent,monkeypatch)
    before=copy.deepcopy(state(agent))
    submitted=outcomes([i for i in found['review_items'] if i['citation']!=REAVES])
    operation(agent,monkeypatch,'complete_task',{'response':'Finished review.',
        'candidate_id':'working_draft' if with_candidate else None,'citation_review_results':submitted})
    error=last_result(agent)
    assert error['error'] and REAVES in error['message'] and 'missing_review_items' in error['message']
    assert agent.status=='running' and state(agent)['draft']==before['draft']
    assert state(agent)['chat_history']==before['chat_history']
    db.session.expire_all()
    items=review.for_run(agent)['review_items']
    submitted=outcomes(items)
    if assess_reaves:
        entry=next(r for r in submitted if next(i for i in items if i['review_item_id']==r['review_item_id'])['citation']==REAVES)
        entry.update(status='assessed',finding='Case identity was looked up; this finding records that limited check.',
                     evidence_ids=[found['research_id']])
    operation(agent,monkeypatch,'complete_task',{'response':'Finished review.',
        'candidate_id':'working_draft' if with_candidate else None,'citation_review_results':submitted})
    assert agent.status=='completed'
    if with_candidate:
        assert state(agent)['draft']['markdown']=='# Authored review\n\nKeep this document unchanged.'
    else:
        assert state(agent)['draft']==before['draft']
    text=state(agent)['chat_history'][-1][1]
    assert 'Citation review coverage' in text and REAVES in text and 'Opinion/application not checked.' in text
    meta=state(agent)['chat_history_meta'][-1]['citation_review']
    assert len(meta['results'])==2
    assert last_result(agent)['citation_review']['results']==meta['results']


def test_original_coomer_record_replay_catches_reaves_omission_without_rewriting_baseline(agent,monkeypatch):
    path=Path(__file__).resolve().parents[1]/'artifacts/model-benchmark-openai-20261115-152018/results/evidence.json'
    if not path.exists():pytest.skip('Private frozen benchmark is available only in the local evaluation workspace.')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    evidence=json.loads(path.read_text())
    original=next(r for r in evidence['runs'] if r['id']=='wf_6009bd5b-6f78-4609-87d9-0c466d07ba77')
    legacy=next(a['content'] for a in reversed(original['artifacts']) if a.get('kind')=='authority_research')
    found=operation_audit(agent,monkeypatch)
    step=tasks.steps(agent).filter_by(kind='tool').order_by(None).order_by(AgentStep.sequence.desc()).first()
    uid='original-coomer-audit-replay'
    db.session.add(AgentArtifact(id=uid,run_id=agent.id,step_id=step.id,kind='authority_research',
        content_json=json.dumps(legacy),created_at=service.now()))
    db.session.commit()
    expected=review.for_run(agent)['review_items']
    reaves=[i for i in expected if REAVES in i['citation']]
    assert reaves
    incomplete=outcomes([i for i in expected if i not in reaves])
    operation(agent,monkeypatch,'complete_task',{'response':'Ready.',
        'candidate_id':None,'citation_review_results':incomplete})
    assert last_result(agent)['error'] and REAVES in last_result(agent)['message']
    assert not state(agent)['chat_history']
    operation(agent,monkeypatch,'complete_task',{'response':'Review with outstanding checks.',
        'candidate_id':None,'citation_review_results':outcomes(expected)})
    assert agent.status=='completed' and REAVES in state(agent)['chat_history'][-1][1]
    assert hashlib.sha256(path.read_bytes()).hexdigest()==digest


def test_overlapping_ranges_deduplicate_occurrences_but_not_distinct_uses(ops,monkeypatch):
    body=REAVES+' first claim.\n\n'+REAVES+' different claim.'
    first=audit(ops,monkeypatch,body)
    original=db.session.get(AgentArtifact,first['research_id']).content_json
    ids=[i['review_item_id'] for i in first['review_items']]
    second=audit(ops,monkeypatch,body,parent=first['research_id'],start=0,end=len(REAVES)+2)
    items=review.for_run(ops.row)['review_items']
    assert len(items)==2 and {i['review_item_id'] for i in items}==set(ids)
    with pytest.raises(ValueError,match='unaccounted'):
        review.preflight(ops.row,args(outcomes(items[:1])).citation_review_results)
    assert db.session.get(AgentArtifact,first['research_id']).content_json==original


def test_overview_and_exploratory_search_do_not_expand_audit_scope(ops,monkeypatch):
    first=audit(ops,monkeypatch,REAVES)
    audit(ops,monkeypatch,OTHER,parent=first['research_id'],mode='overview')
    items=review.for_run(ops.row)['review_items']
    assert [i['citation'] for i in items]==[REAVES]
    assert len(review.preflight(ops.row,args(outcomes(items)).citation_review_results)['results'])==1


def test_unlocated_provider_reference_remains_unresolved_and_discovery_gap_visible(ops,monkeypatch):
    monkeypatch.setattr(sources,'lookup_citations',lambda *a,**kw:([case_result(citation=REAVES)],None))
    monkeypatch.setattr(research,'_discover_mentions',lambda *a,**kw:[])
    found=research.lookup(ops,lookup_args(REAVES,mode='audit'))
    assert len(found['review_items'])==1
    item=found['review_items'][0]
    assert item['location_uncertain'] and found['review_limitations']
    with pytest.raises(ValueError,match='unconfirmed source location'):
        review.preflight(ops.row,args([{'review_item_id':item['review_item_id'],'status':'assessed',
            'finding':'An identity lead exists.','evidence_ids':[found['research_id']]}]).citation_review_results)
    value=review.preflight(ops.row,args(outcomes([item])).citation_review_results)
    assert 'discovery is incomplete' in review.render(value) and 'exact location unconfirmed' in review.render(value)


def test_identification_failure_discloses_incompleteness_even_with_no_items(ops,monkeypatch):
    monkeypatch.setattr(research,'_discover_mentions',lambda *a,**kw:(_ for _ in ()).throw(ValueError('Cannot locate an occurrence.')))
    found=research.lookup(ops,lookup_args('Some selected source text.',mode='audit'))
    assert found['review_items']==[] and found['review_limitations']
    value=review.preflight(ops.row,[])
    assert 'discovery is incomplete' in review.render(value) and 'not proof that none exist' in review.render(value)


@pytest.mark.parametrize('problem',['duplicate','unknown','missing_evidence','foreign_evidence','generated_evidence','blank_finding'])
def test_invalid_outcomes_never_publish(agent,monkeypatch,problem):
    found=operation_audit(agent,monkeypatch,REAVES)
    values=outcomes(found['review_items'])
    if problem=='duplicate':values.append(copy.deepcopy(values[0]))
    elif problem=='unknown':values[0]['review_item_id']='not-in-scope'
    elif problem=='blank_finding':values[0]['finding']=' \t '
    elif problem=='missing_evidence':values[0]['status']='assessed'
    else:
        values[0]['status']='assessed';values[0]['evidence_ids']=['not-authorized']
        if problem=='generated_evidence':
            operation(agent,monkeypatch,'read_sources',{'artifact_id':'current_draft','ranges':[],'start':0,'end':None})
            step=tasks.steps(agent).filter_by(kind='tool').order_by(None).order_by(AgentStep.sequence.desc()).first()
            db.session.add(AgentArtifact(id='generated-evidence',run_id=agent.id,step_id=step.id,
                kind='analysis',content_json='{"text":"Generated assertion."}',created_at=service.now()))
            db.session.commit();values[0]['evidence_ids']=['generated-evidence']
    operation(agent,monkeypatch,'complete_task',{'response':'Done.',
        'candidate_id':None,'citation_review_results':values})
    assert last_result(agent)['error'] and not state(agent)['chat_history']


def test_an_opinion_read_does_not_satisfy_review_completion(agent,monkeypatch):
    install(monkeypatch,[case_result(citation=REAVES)])
    operation(agent,monkeypatch,'lookup_authorities',{'targets':[{'text':REAVES,'file_id':None,
        'draft_id':None,'start':None,'end':None}],'mode':'audit','purpose':'Review use.','research_id':None})
    found=last_result(agent)
    monkeypatch.setattr(sources,'fetch_source',lambda *a,**kw:{'status':'read','text':'Actual opinion body.',
        'opinion_id':52,'blocks':[]})
    operation(agent,monkeypatch,'read_authority',{'research_id':found['research_id'],
        'source_id':found['sources'][0]['id'],'opinion_id':52,'query':None,'start':0})
    operation(agent,monkeypatch,'complete_task',{'response':'Read the opinion.','candidate_id':None})
    assert last_result(agent)['error'] and 'unaccounted' in last_result(agent)['message']


def test_ordinary_legacy_completion_has_no_new_table_or_obligation(agent,monkeypatch):
    operation(agent,monkeypatch,'complete_task',{'response':'Ordinary answer.','candidate_id':None})
    assert agent.status=='completed' and state(agent)['chat_history'][-1][1]=='Ordinary answer.'
    assert 'citation_review' not in state(agent)['chat_history_meta'][-1]


def test_cancelled_audit_cannot_publish_after_outcomes_are_prepared(agent,monkeypatch):
    found=operation_audit(agent,monkeypatch)
    choose(monkeypatch,'complete_task',{'response':'Review.', 'candidate_id':None,
        'citation_review_results':outcomes(found['review_items'])})
    tick(agent)
    service.stop(agent,cancelled=True)
    tick(agent)
    assert agent.status=='cancelled' and not state(agent)['chat_history']


def test_table_escapes_source_text_and_does_not_invent_verification(ops,monkeypatch):
    found=audit(ops,monkeypatch)
    rows=outcomes(found['review_items']);rows[0]['finding']='Unchecked | [link](https://example.test)\nnew line'
    table=review.render(review.preflight(ops.row,args(rows).citation_review_results))
    assert r'\|' in table and r'\[link\]' in table
    assert 'not that the citation passed every check' in table


def test_retry_keeps_audit_items_and_completion_publishes_once(agent,monkeypatch):
    found=operation_audit(agent,monkeypatch)
    values=outcomes(found['review_items'])
    expected=[i['review_item_id'] for i in review.for_run(agent)['review_items']]
    choose(monkeypatch,'complete_task',{'response':'Review with unresolved work.',
        'candidate_id':None,'citation_review_results':values})
    tick(agent)
    pending=tasks.steps(agent).filter_by(kind='tool',name='complete_task').order_by(None).order_by(AgentStep.sequence.desc()).first()
    pending.state='running'
    service.stop(agent,'Worker interrupted before publication.')
    db.session.expire_all()
    assert [i['review_item_id'] for i in review.for_run(agent)['review_items']]==expected
    service.retry(agent);tasks.dispatch(agent)
    tick(agent)
    assert agent.status=='completed' and len(state(agent)['chat_history'])==1
    tick(agent)
    assert len(state(agent)['chat_history'])==1


def test_later_window_resolves_provider_lead_seen_only_in_neighboring_context(ops,monkeypatch):
    original=research._iter_lookup_windows
    monkeypatch.setattr(research,'_iter_lookup_windows',lambda text,blocks,start: original(text,blocks,start,max_chars=40,context_chars=20))
    install(monkeypatch)
    monkeypatch.setattr(sources,'lookup_citations',lambda text,**kw: ([case_result(citation=REAVES)] if REAVES in text else [],None))
    body='x'*35+'\n\n'+REAVES+' supported.\n\n'+'y'*70
    found=research.lookup(ops,lookup_args(body,mode='audit'))
    assert len(found['review_items'])==1
    assert not found['review_items'][0]['location_uncertain']
    assert not found['review_limitations']


def test_repeating_audit_cannot_erase_an_already_identified_use(ops,monkeypatch):
    first=audit(ops,monkeypatch,REAVES)
    from tests.test_authority_research import step_ops
    later=step_ops(ops.row)
    monkeypatch.setattr(research,'_discover_mentions',lambda *a,**kw:[])
    second=research.lookup(later,lookup_args(REAVES,mode='audit'))
    assert second['review_items'][0]['review_item_id']==first['review_items'][0]['review_item_id']
    required=review.for_run(ops.row)['review_items']
    assert [i['review_item_id'] for i in required]==[first['review_items'][0]['review_item_id']]


def test_repeating_audit_keeps_unlocated_lead_until_its_location_is_confirmed(ops,monkeypatch):
    from tests.test_authority_research import step_ops
    monkeypatch.setattr(sources,'lookup_citations',lambda *a,**kw:([case_result(citation=REAVES)],None))
    monkeypatch.setattr(research,'_discover_mentions',lambda *a,**kw:[])
    first=research.lookup(ops,lookup_args(REAVES,mode='audit'))
    lead=first['review_items'][0]
    assert lead['location_uncertain']
    monkeypatch.setattr(sources,'lookup_citations',lambda *a,**kw:([],None))
    second=research.lookup(step_ops(ops.row),lookup_args(REAVES,mode='audit'))
    assert [i['review_item_id'] for i in second['review_items']]==[lead['review_item_id']]
    assert second['review_limitations']
    monkeypatch.setattr(research,'_discover_mentions',discover)
    third=research.lookup(step_ops(ops.row),lookup_args(REAVES,mode='audit'))
    assert len(third['review_items'])==1
    assert not third['review_items'][0]['location_uncertain']
    assert not third['review_limitations']


def test_failed_location_of_known_discovered_reference_is_an_unresolved_item(ops,monkeypatch):
    monkeypatch.setattr(sources,'lookup_citations',lambda *a,**kw:([],None))
    monkeypatch.setattr(research,'_discover_mentions',lambda *a,**kw:[{
        'kind':'case','citation_as_written':'Reaves','case_name':'Reaves',
        'context':'Reaves supports the claim.','proposition':'Attributed support for the claim.',
        'unit_id':'not-a-supplied-unit','occurrence_index':1}])
    found=research.lookup(ops,lookup_args('Reaves supports the claim.',mode='audit'))
    assert len(found['review_items'])==1
    item=found['review_items'][0]
    assert item['citation']=='Reaves' and item['location_uncertain']
    assert item['proposition']=='Attributed support for the claim.'
    assert found['review_limitations']
    with pytest.raises(ValueError,match='unaccounted'):
        review.preflight(ops.row,[])
    accepted=review.preflight(ops.row,args(outcomes([item])).citation_review_results)
    assert accepted['results'][0]['status']=='unresolved'


def test_completion_reports_simultaneous_errors(agent, monkeypatch):
    found = operation_audit(agent, monkeypatch)
    items = found['review_items']
    rows = [{'review_item_id': items[0]['review_item_id'], 'status':'assessed',
             'finding':'A finding.', 'evidence_ids':['unavailable-evidence']},
            {'review_item_id':'misspelled', 'status':'assessed', 'finding':'Another finding.', 'evidence_ids':[]}]
    before = copy.deepcopy(state(agent))
    operation(agent, monkeypatch, 'complete_task', {'response':'Done', 'candidate_id':None,
              'citation_review_results':rows})
    error = last_result(agent)
    assert error['code'] == 'citation_review_invalid'
    assert {'invalid_evidence','unknown_review_item','missing_evidence'} <= {i['code'] for i in error['issues']}
    assert error['missing_review_items']
    assert state(agent) == before and agent.status == 'running'


def test_current_checklist_and_completion_enums(agent, monkeypatch):
    found = operation_audit(agent, monkeypatch)
    options = review.completion_options(agent)
    definitions = {t['name']:t for t in agent_tools.definitions(agent)}
    props = definitions['complete_task']['parameters']['$defs']['CitationReviewResult']['properties']
    assert set(props['review_item_id']['enum']) == {i['review_item_id'] for i in options['review_items']}
    assert found['research_id'] in props['evidence_ids']['items']['enum']
    operation(agent, monkeypatch, 'list_documents', {})
    listed = last_result(agent)['citation_review']
    assert listed['review_item_count'] == len(options['review_items'])
    assert all(i['allowed_statuses'] for i in listed['review_items'])
    plain = {t['name']:t for t in agent_tools.definitions()}
    assert 'enum' not in plain['complete_task']['parameters']['$defs']['CitationReviewResult']['properties']['review_item_id']


def test_no_audit_completion_schema_has_no_empty_enum(agent):
    schema = next(t['parameters'] for t in agent_tools.definitions(agent) if t['name']=='complete_task')
    assert schema['properties']['citation_review_results']['maxItems'] == 0
    assert 'enum' not in schema['$defs']['CitationReviewResult']['properties']['review_item_id']


def test_uncertain_location_and_other_errors_return_together(ops, monkeypatch):
    found = audit(ops, monkeypatch)
    item = review.for_run(ops.row)['review_items'][0]
    monkeypatch.setattr(review, 'for_run', lambda row: {'review_items':[{**item,'location_uncertain':True}], 'review_scope_ids':['scope'], 'review_limitations':[]})
    with pytest.raises(review.CompletionValidationError) as caught:
        review.preflight(ops.row, [{'review_item_id':item['review_item_id'],'status':'assessed','finding':'Checked','evidence_ids':['bad']},
                                 {'review_item_id':item['review_item_id'],'status':'unresolved','finding':'Unchecked','evidence_ids':[]}])
    assert {'unconfirmed_location','invalid_evidence','duplicate_review_item'} <= {i['code'] for i in caught.value.details['issues']}


def test_recorded_coomer_first_completion_reports_all_known_problems(agent, monkeypatch):
    path = Path(__file__).resolve().parents[1]/'artifacts/model-benchmark-openai-v2-20261117-083206/results/evidence.json'
    if not path.exists():
        pytest.skip('Private recorded replay is available only locally.')
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    original = next(r for r in json.loads(path.read_text())['runs'] if r['id']=='wf_ea960759-ff35-4b30-b856-566756feacda')
    steps = {s['sequence']:s for s in original['steps']}
    items = steps[5]['result']['review_items']
    submitted = json.loads(steps[41]['arguments']['raw'])['citation_review_results']
    available = {a['id'] for a in original['artifacts']}
    monkeypatch.setattr(review, 'for_run', lambda row: {'review_items':items,'review_scope_ids':['scope'],'review_limitations':[]})
    def owned(row, uid):
        if uid not in available:
            raise ValueError('Research artifact is unavailable in this chat.')
        return SimpleNamespace(kind='authority_source',run_id=agent.id)
    monkeypatch.setattr(research, '_owned_row', owned)
    with pytest.raises(review.CompletionValidationError) as caught:
        review.preflight(agent, submitted)
    codes = [i['code'] for i in caught.value.details['issues']]
    assert codes.count('unknown_review_item') == 1
    assert codes.count('unconfirmed_location') == 2
    assert codes.count('invalid_evidence') == 4
    assert len(caught.value.details['missing_review_items']) == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_enum_limit_fallback_preserves_all_options(agent, monkeypatch):
    options = {'review_items':[{'review_item_id':f'citation_{i}'} for i in range(1001)],
               'evidence_options':[]}
    monkeypatch.setattr(review, 'completion_options', lambda row: options)
    schema = next(t['parameters'] for t in agent_tools.definitions(agent) if t['name']=='complete_task')
    field = schema['$defs']['CitationReviewResult']['properties']['review_item_id']
    assert 'enum' not in field and 'provider limits' in field['description']
    assert len(options['review_items']) == 1001


def test_completion_schemas_are_isolated_and_refresh_between_requests(monkeypatch):
    def options(row):
        return {'review_items':[{'review_item_id':'citation_'+row.id}],
                'evidence_options':[{'artifact_id':'evidence_'+row.id}]}
    monkeypatch.setattr(review, 'completion_options', options)
    def schema(uid):
        return next(t['parameters'] for t in agent_tools.definitions(SimpleNamespace(id=uid)) if t['name']=='complete_task')
    a, b = schema('A'), schema('B')
    fields = lambda s:s['$defs']['CitationReviewResult']['properties']
    assert fields(a)['review_item_id']['enum']==['citation_A']
    assert fields(b)['review_item_id']['enum']==['citation_B']
    fields(b)['evidence_ids']['items']['enum'].append('mutated')
    assert fields(schema('B'))['evidence_ids']['items']['enum']==['evidence_B']
    assert fields(a)['evidence_ids']['items']['enum']==['evidence_A']


def test_malformed_completion_returns_field_errors(agent, monkeypatch):
    operation(agent, monkeypatch, 'complete_task', {'response':'Done','candidate_id':None,
              'citation_review_results':'not a list'})
    error=last_result(agent)
    assert error['error'] and error['issues'][0]['path']==['citation_review_results']
    assert not state(agent)['chat_history']
