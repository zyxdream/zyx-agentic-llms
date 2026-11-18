"""Separate review conclusions share source evidence and the existing accounting fields.

Scripted model responses test delivery/publication only, not legal judgment quality.
"""
import json

import pytest

from backend import agent_tasks as tasks
from backend.ai import base
from backend.database import db
from backend.domains.legal import authority_research as research, authority_sources as sources, citation_review as review
from backend.domains.legal.instructions import SYSTEM
from backend.models import AgentArtifact
from backend.util import document_drafts as drafts, workspace_service as service
from tests.test_agent_controller import agent, operation, last_result, state
from tests.test_citation_review import audit, args
from tests.test_authority_research import ops, record
from tests.test_document_preparation import app


@pytest.mark.parametrize('scenario,finding', [
    ('matching quotation', 'Wording reproduced accurately and support checked in context. Pinpoint not checked.'),
    ('missing pagination', 'Wording and context assessed. Published page not verified from the retrieved sources.'),
    ('out of context', 'Quoted words occur, but the surrounding limitation does not support the broader claim. Pinpoint not checked.'),
    ('paraphrase', 'No direct quotation to compare. Attributed meaning assessed; pinpoint not checked.'),
    ('alternative match', 'Original phrase not located; alternative found related discussion. Quotation remains unverified.'),
    ('partial coverage', 'Only the selected passage was assessed; other discussion and pinpoint remain unchecked.'),
])
def test_one_existing_item_accepts_limited_supported_finding_without_automatic_verdict(ops, monkeypatch, scenario, finding):
    found = audit(ops, monkeypatch)
    item = found['review_items'][0]
    assert not item['location_uncertain']
    evidence = ops.save(research.SOURCE_KIND, {'kind':research.SOURCE_KIND, 'role':'passage',
        'text':'An accurate phrase. This rule is limited to the stated circumstances.',
        'match_found':True, 'original_query_found':scenario != 'alternative match',
        'matched_query':'accurate phrase', 'match_start':3, 'match_end':18,
        'page_evidence':[], 'matched_published_pages':None})
    outcome = {'review_item_id':item['review_item_id'], 'status':'assessed',
        'finding':finding, 'evidence_ids':[evidence]}
    result = review.preflight(ops.row, args([outcome]).citation_review_results)
    assert result['results'] == [outcome]
    assert result['review_items'][0]['review_item_id'] == item['review_item_id']
    assert len(result['review_items']) == 1
    assert 'verified' not in record(evidence)
    # Supplemental pagination adds evidence to the same requested citation use.
    page = ops.save(research.SOURCE_KIND, {'kind':research.SOURCE_KIND, 'role':'passage',
        'text':'The same relevant discussion.', 'page_evidence':[{'page':'867'}]})
    supplemental = {**outcome, 'finding':finding+' Supplemental page evidence is now available.',
                    'evidence_ids':[evidence,page]}
    updated = review.preflight(ops.row, args([supplemental]).citation_review_results)
    assert len(updated['review_items']) == 1
    assert updated['results'] == [supplemental]
    assert result['results'] == [outcome]


@pytest.mark.parametrize('tool', ['analyze_material','create_draft','revise_draft','verify_draft'])
def test_shared_guidance_and_saved_location_note_reach_material_operations(agent, monkeypatch, tool):
    from backend.domains.legal.instructions import AUTHORITY_LOCATION_NOTE
    operation(agent, monkeypatch, 'lookup_authorities', {'targets':[{'file_id':None,
        'draft_id':None,'text':'A general discussion.','start':None,'end':None}],
        'mode':'overview','purpose':'Read context.','research_id':None})
    # Use a saved passage just as read_authority does; selection must not rewrite it.
    step = tasks.steps(agent).filter_by(kind='tool').first()
    uid = 'source-review-'+tool
    content = {'kind':research.SOURCE_KIND,'role':'passage',
        'text':'This rule applies only when the stated condition is met.',
        'query':'unlocated original phrase','original_query_found':False,
        'matched_query':'stated condition','match_start':38,'match_end':54,
        'page_evidence':[], 'matched_published_pages':None,
        'location_note':AUTHORITY_LOCATION_NOTE}
    db.session.add(AgentArtifact(id=uid,run_id=agent.id,step_id=step.id,kind=research.SOURCE_KIND,
        content_json=json.dumps(content),created_at=service.now()))
    db.session.commit()
    original = db.session.get(AgentArtifact,uid).content_json
    db.session.expire_all()
    calls = []
    def check(prompt, options):
        assert drafts.SOURCE_LOCATION_GUIDANCE in options['sys_prompt']
        assert 'Assess quotation wording and support in context separately' in options['sys_prompt']
        assert 'For paraphrases' in options['sys_prompt']
        assert 'not checked' in options['sys_prompt']
        assert 'not verified from the retrieved sources' in options['sys_prompt']
        assert 'Prioritize material wording/context checks' in options['sys_prompt']
        assert 'stated condition' in prompt and AUTHORITY_LOCATION_NOTE in prompt
        calls.append(1)
    def writer(prompt, schema, **options):
        if schema is tasks.EditorWriteCheck:
            return schema(decision='not_forbidden',reason='Requested edit.',supporting_wording='')
        check(prompt, options)
        assert json.loads(prompt)['materials'][0] == {**content,'artifact_id':uid}
        if schema is drafts.MarkdownDraftResult:
            return schema(title='Review',markdown='Supported content; page not verified.',update_court_format=False)
        return schema(mode='patch', title=None, markdown=None,
            replacements=[{'old_text':'Original facts.','new_text':'Revised facts.'}],update_court_format=False)
    def analyst(prompt, **options):
        check(prompt,options)
        return 'Content reviewed; reporter page not verified from the retrieved sources.'
    monkeypatch.setattr(base,'chat_completion_parse',writer)
    monkeypatch.setattr(base,'chat_completion',analyst)
    values = {'instructions':'Assess wording and use in context.','material_ids':[uid]}
    if tool in {'revise_draft','verify_draft'}: values['target']='working_draft'
    if tool == 'revise_draft': values['revision_mode']='general_revision'
    if tool == 'verify_draft': values['check_citations']=False
    operation(agent,monkeypatch,tool,values)
    assert not last_result(agent).get('error'),last_result(agent)
    assert calls == [1]
    assert db.session.get(AgentArtifact,uid).content_json == original
    assert drafts.SOURCE_LOCATION_GUIDANCE in tasks.SYSTEM
    assert 'A supported content finding may be assessed while' in SYSTEM


def test_unverified_page_does_not_prevent_publication_of_supported_content_finding(agent, monkeypatch):
    from tests.test_citation_review import operation_audit, outcomes
    found=operation_audit(agent,monkeypatch)
    results=outcomes(found['review_items'],evidence=found['research_id'])
    for result in results:
        result['finding']='The recorded finding concerns source identity only. Quotation/context not checked; pinpoint not verified.'
    operation(agent,monkeypatch,'complete_task',{'response':'Limited review; remaining checks disclosed.',
        'candidate_id':None,'citation_review_results':results})
    assert agent.status == 'completed'
    assert last_result(agent)['citation_review']['results'] == results
    assert 'pinpoint not verified' in state(agent)['chat_history'][-1][1]
