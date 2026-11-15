"""General drafting handoff and controller loops; all model responses are mocked."""
import copy
import json

import pytest

from backend import agent_tasks as tasks
from backend.ai import agent_tools, base
from backend.database import db
from backend.models import AgentArtifact, AgentStep
from backend.util import document_drafts as drafts, source_context, workspace_service as service
from tests.test_agent_controller import agent, call, last_result, operation, state, tick
from tests.test_document_preparation import app


TOOLS = ['analyze_material', 'create_draft', 'revise_draft', 'verify_draft']


def arguments(name, **values):
    result = {'instructions': 'Follow the user request.', 'material_ids': ['current_draft']}
    if name in {'revise_draft', 'verify_draft'}:
        result['target'] = 'current_draft'
    if name == 'revise_draft':
        result['revision_mode'] = 'general_revision'
    if name == 'verify_draft':
        result['check_citations'] = False
    return {**result, **values}


@pytest.mark.parametrize('name', TOOLS)
def test_material_purpose_schema_and_legacy_calls_remain_compatible(name):
    legacy = arguments(name)
    original = copy.deepcopy(legacy)
    assert agent_tools.arguments(name, legacy).material_uses == [] and legacy == original
    tools = {item['name']: item for item in agent_tools.definitions()}
    schema = tools[name]['parameters']
    assert 'material_uses' in schema['required']
    definition = schema['$defs']['MaterialUse']
    assert set(definition['required']) == {'artifact_id', 'purpose'}
    assert definition['additionalProperties'] is False
    assert 'enum' not in definition['properties']['purpose']
    result = agent_tools.arguments(name, arguments(name, material_uses=[{
        'artifact_id':'current_draft', 'purpose':'Use the cadence and headings; facts come from the user.'}]))
    assert result.material_uses[0].purpose.startswith('Use the cadence')


@pytest.mark.parametrize('name', TOOLS)
@pytest.mark.parametrize('uses', [
    [{'artifact_id':'unselected', 'purpose':'Use facts.'}],
    [{'artifact_id':'current_draft', 'purpose':'   '}],
    [{'artifact_id':'current_draft', 'purpose':'Valid'}, {'artifact_id':'current_draft', 'purpose':''}],
    [{'artifact_id':'current_draft', 'purpose':42}],
    [{'artifact_id':42, 'purpose':'Use facts.'}],
    {'artifact_id':'current_draft', 'purpose':'Use facts.'},
])
def test_invalid_material_uses_are_rejected(name, uses):
    with pytest.raises(ValueError):
        agent_tools.arguments(name, arguments(name, material_uses=uses))


@pytest.mark.parametrize('name', TOOLS)
def test_repeated_material_purposes_preserve_order_text_and_inputs(name):
    style = ' Use the Answer excerpts for style only. '
    facts = 'Use the Cedar notes for matter facts.'
    values = arguments(name, material_ids=['bundle', 'other'], material_uses=[
        {'artifact_id':'bundle', 'purpose':style},
        {'artifact_id':'other', 'purpose':'Preserve qualifications.'},
        {'artifact_id':'bundle', 'purpose':facts},
        {'artifact_id':'bundle', 'purpose':style},
        {'artifact_id':'other', 'purpose':style},
    ])
    original = copy.deepcopy(values)
    result = agent_tools.arguments(name, values)
    assert [use.model_dump() for use in result.material_uses] == [
        {'artifact_id':'bundle', 'purpose':style + '\n' + facts},
        {'artifact_id':'other', 'purpose':'Preserve qualifications.\n' + style},
    ]
    assert values == original
    assert agent_tools.arguments(name, result.model_dump()).model_dump() == result.model_dump()


def source_material(agent, monkeypatch):
    snapshot = json.loads(agent.data_json)['document_snapshot']
    uid = snapshot['files'][0]['file_id']
    body = source_context.document(snapshot, uid)['manifest']['text']
    operation(agent, monkeypatch, 'read_sources', {'artifact_id':None,
        'ranges':[{'file_id':uid, 'start':0, 'end':len(body)}], 'start':0, 'end':None})
    return last_result(agent)['artifact_id']


def permitted(schema):
    return schema(decision='not_forbidden', reason='User permits drafting.', supporting_wording='')


@pytest.mark.parametrize('name', TOOLS)
@pytest.mark.parametrize('repeat_uses', [False, True])
def test_purposes_reach_operation_and_saved_metadata_without_changing_sources(agent, monkeypatch, name, repeat_uses):
    style = source_material(agent, monkeypatch)
    facts = source_material(agent, monkeypatch)
    uses = [{'artifact_id':style, 'purpose':'Use as a style and structure example only.'},
            {'artifact_id':facts, 'purpose':'Use the quoted facts, preserving source qualifications.'}]
    expected = copy.deepcopy(uses)
    if repeat_uses:
        extra = 'The example supplies no facts about the new matter.'
        uses.extend([{'artifact_id':style, 'purpose':extra}, copy.deepcopy(uses[0])])
        expected[0]['purpose'] += '\n' + extra
    values = arguments(name, material_ids=[style, facts], material_uses=uses)
    original_values = copy.deepcopy(values)
    original = {uid:db.session.get(AgentArtifact, uid).content_json for uid in (style, facts)}
    calls = []
    def parse(prompt, schema, **options):
        calls.append((json.loads(prompt)['materials'], options))
        if schema is drafts.MarkdownDraftResult:
            return schema(title='New document', markdown='# New document\n\nRequested text.', update_court_format=False)
        assert schema is drafts.MarkdownRevisionResult
        return schema(mode='patch', title=None, markdown=None,
            replacements=[{'old_text':'Original facts.','new_text':'Revised facts.','replace_all':False}], update_court_format=False)
    def text(prompt, **options):
        marker = '\nSupporting materials:\n' if name == 'verify_draft' else '\nMaterials (source evidence and prior generated work are labeled):\n'
        calls.append((json.loads(prompt.split(marker)[1]), options))
        return 'Requested findings.'
    monkeypatch.setattr(base, 'chat_completion_parse', parse)
    monkeypatch.setattr(base, 'chat_completion', text)
    operation(agent, monkeypatch, name, values)
    result = last_result(agent)
    assert not result.get('error'), result
    saved = json.loads(db.session.get(AgentArtifact, result['artifact_id']).content_json)
    assert saved['material_uses'] == expected
    assert values == original_values
    step = tasks.steps(agent).filter_by(kind='tool', name=name).order_by(None).order_by(AgentStep.sequence.desc()).first()
    assert json.loads(json.loads(step.arguments_json)['raw']) == original_values
    assert len(calls) == 1
    supplied, options = calls[0]
    assert [(item['artifact_id'], item['use_in_this_operation']) for item in supplied] == [(u['artifact_id'],u['purpose']) for u in expected]
    assert 'cannot override either' in options['sys_prompt']
    assert all(db.session.get(AgentArtifact, uid).content_json == raw for uid,raw in original.items())


def test_purpose_is_per_operation_and_legacy_use_stays_unspecified(agent, monkeypatch):
    uid = source_material(agent, monkeypatch)
    seen = []
    def analyze(prompt, **options):
        seen.append(json.loads(prompt.split('\nMaterials (source evidence and prior generated work are labeled):\n')[1]))
        return 'Findings.'
    monkeypatch.setattr(base,'chat_completion',analyze)
    for purpose in ['Style example only.', 'Check factual support.', None]:
        values = {'instructions':'Analyze.', 'material_ids':[uid]}
        if purpose:
            values['material_uses']=[{'artifact_id':uid,'purpose':purpose}]
        operation(agent,monkeypatch,'analyze_material',values)
    assert seen[0][0]['use_in_this_operation']=='Style example only.'
    assert seen[1][0]['use_in_this_operation']=='Check factual support.'
    assert 'use_in_this_operation' not in seen[2][0]
    assert json.loads(db.session.get(AgentArtifact,uid).content_json).get('use_in_this_operation') is None


def test_working_draft_material_purpose_pins_resolved_candidate(agent, monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setattr(drafts,'write_document',lambda **kw:drafts.MarkdownDraftResult(
            title='Draft',markdown='Saved draft.',update_court_format=False))
        operation(agent,monkeypatch,'create_draft',{'instructions':'Create.','material_ids':[]})
    candidate=last_result(agent)['artifact_id']
    prompts=[]
    monkeypatch.setattr(base,'chat_completion',lambda prompt,**kw:prompts.append(prompt) or 'Findings.')
    operation(agent,monkeypatch,'verify_draft',{'target':'working_draft','check_citations':False,
        'instructions':'Check current draft.','material_ids':['working_draft'],
        'material_uses':[{'artifact_id':'working_draft','purpose':'Compare the current wording.'}]})
    result=last_result(agent)
    assert result['target']==candidate
    record=json.loads(db.session.get(AgentArtifact,result['artifact_id']).content_json)
    assert record['material_uses']==[{'artifact_id':candidate,'purpose':'Compare the current wording.'}]
    assert candidate in prompts[0]


@pytest.mark.parametrize('assignment, result_body', [
    ('Use the example tone, but write a new letter about the facts I supplied.', '# Letter\n\nRequested new facts.'),
    ('Combine the supplied reports into a concise executive brief.', '# Executive brief\n\nCombined findings.'),
    ('Give responses to the source statements in a clear table.', '| Source | Response |\n| --- | --- |\n| Statement | Response |'),
    ('Write an explanation suitable for readers without technical knowledge.', '# Explanation\n\nPlain language.'),
])
def test_general_controller_inspects_result_then_completes_without_required_reviewer(agent, monkeypatch, assignment, result_body):
    data=json.loads(agent.data_json);data['query']=assignment;agent.data_json=service.encode(data);db.session.commit()
    material=source_material(agent,monkeypatch)
    uses=[{'artifact_id':material,'purpose':'Use this material according to the original user request.'}]
    chosen=[];writer_calls=[]
    def decide(model_request):
        original=json.loads(model_request['input'][1]['content'])
        assert original['assignment']==assignment
        chosen.append(model_request)
        if len(chosen)==1:
            return call('create_draft',{'instructions':assignment,'material_ids':[material],'material_uses':uses})
        if len(chosen)==2:
            return call('read_sources',{'artifact_id':'working_draft','ranges':[],'start':0,'end':None})
        observations=[json.loads(item['output']) for item in model_request['input'] if item.get('type')=='function_call_output']
        assert json.loads(observations[-1]['text'])['draft']['markdown']==result_body
        return call('complete_task',{'response':'Prepared as requested.','candidate_id':'working_draft'})
    def write(prompt,schema,**options):
        if schema is tasks.EditorWriteCheck:return permitted(schema)
        assert schema is drafts.MarkdownDraftResult
        payload=json.loads(prompt);writer_calls.append(payload)
        assert payload['assignment']==assignment and payload['instructions']==assignment
        assert payload['materials'][0]['use_in_this_operation']==uses[0]['purpose']
        return schema(title='Requested document',markdown=result_body,update_court_format=False)
    monkeypatch.setattr(base,'responses_completion',decide)
    monkeypatch.setattr(base,'chat_completion_parse',write)
    for _ in range(6):result=tick(agent)
    assert result=={'completed':True} and state(agent)['draft']['markdown']==result_body
    assert len(writer_calls)==1 and tasks.steps(agent).filter_by(name='verify_draft').count()==0
    assert 'assess the actual current result' in chosen[0]['input'][0]['content']


def test_revision_rejects_uncanonicalized_target_before_provider(monkeypatch):
    monkeypatch.setattr(base,'chat_completion_parse',lambda *a,**kw:pytest.fail('Invalid revision target reached provider'))
    with pytest.raises(ValueError,match='canonical Markdown'):
        drafts.write_document(assignment='Edit.',instructions='Edit.',conversation={},materials=[],
            target={'source_format':'uploaded','text':'Raw upload'},answers=[],model='mock',reasoning_effort='medium')


def test_recorded_duplicate_purpose_call_completes_without_validation_retry(agent, monkeypatch):
    # The live Cedar call selected one read_sources artifact containing both
    # factual and style excerpts, then supplied one purpose entry for each use.
    material = source_material(agent, monkeypatch)
    original_source = db.session.get(AgentArtifact, material).content_json
    purposes = [
        'Source of Cedar-specific facts, limits, dates, stated policy, incomplete review, and absent procedural information.',
        'The read range from Uber/Ottomotto Answer is a style-only reference for firm organized prose and concise descriptive headings; do not import its facts, parties, legal positions, or circumstances.',
    ]
    values = {
        'instructions':'Draft a preliminary internal position statement for Cedar using the notes for facts and the Answer for style.',
        'material_ids':[material],
        'material_uses':[{'artifact_id':material, 'purpose':purpose} for purpose in purposes],
    }
    original_values = copy.deepcopy(values)
    chosen, writer_calls = [], []
    def decide(request):
        chosen.append(request)
        if len(chosen) == 1:
            return call('create_draft', values)
        observations = [json.loads(item['output']) for item in request['input'] if item.get('type') == 'function_call_output']
        assert not observations[-1].get('error'), observations[-1]
        if len(chosen) == 2:
            assert observations[-1]['kind'] == 'draft'
            return call('read_sources', {'artifact_id':'working_draft', 'ranges':[], 'start':0, 'end':None})
        assert len(chosen) == 3
        return call('complete_task', {'response':'Prepared the statement.', 'candidate_id':'working_draft'})
    def write(prompt, schema, **options):
        if schema is tasks.EditorWriteCheck:
            return permitted(schema)
        assert schema is drafts.MarkdownDraftResult
        writer_calls.append(json.loads(prompt))
        supplied = writer_calls[-1]['materials']
        assert len(supplied) == 1
        assert supplied[0]['use_in_this_operation'] == '\n'.join(purposes)
        return schema(title='Cedar statement', markdown='# Cedar\n\nThe review remains incomplete.', update_court_format=False)
    monkeypatch.setattr(base, 'responses_completion', decide)
    monkeypatch.setattr(base, 'chat_completion_parse', write)
    for _ in range(6):
        result = tick(agent)
    assert result == {'completed':True}
    assert len(chosen) == 3 and len(writer_calls) == 1
    steps = tasks.steps(agent).filter_by(name='create_draft').all()
    assert len(steps) == 1
    assert json.loads(json.loads(steps[0].arguments_json)['raw']) == original_values == values
    saved = db.session.get(AgentArtifact, json.loads(steps[0].result_json)['artifact_id'])
    assert json.loads(saved.content_json)['material_uses'] == [{'artifact_id':material, 'purpose':'\n'.join(purposes)}]
    assert db.session.get(AgentArtifact, material).content_json == original_source
