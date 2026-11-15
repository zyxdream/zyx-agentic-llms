"""Real SQL controller runs; model and broker calls are boundary doubles."""
import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from backend import agent_tasks as tasks, workspace_runs as routes
from backend.workspace import state as everything
from backend.ai import agent_tools, base
from backend.ai.token_budget import ContextBudgetExceeded
from backend.database import db
from backend.models import AgentArtifact, AgentStep, TextEntry
from backend.util import workspace_service as service, document_preparation as prep, conversation_context
from backend.util import document_drafts
from tests.test_document_preparation import app, admit, receive, run


@pytest.fixture
def agent(app, monkeypatch):
    app.register_blueprint(routes.bp)
    for endpoint, fn in list(app.view_functions.items()):
        if endpoint.startswith('workspace_services.'):
            app.view_functions[endpoint] = fn.__wrapped__
    monkeypatch.setattr(routes, 'current_user', SimpleNamespace(id=1))
    monkeypatch.setattr(tasks.advance, 'apply_async', lambda **kw: None)
    monkeypatch.setattr(tasks.advance, 'update_state', lambda **kw: None)
    monkeypatch.setattr(conversation_context, 'for_request', lambda *a, **kw: {'text': 'Conversation context'})
    monkeypatch.setattr(everything, 'finalize_llm_response_for_display', lambda text, *a, **kw: (text, {}, {}))
    monkeypatch.setattr(everything, '_enqueue_brief_update', lambda *a, **kw: None)
    entry = receive(admit('agent'))
    assert run(entry)['ok']
    state = everything.create_empty_state()
    state['draft'] = {'title': 'Original', 'sections': [{'section_id': '1', 'section_name': 'Facts', 'section_content': 'Original facts.'}]}
    db.session.add(TextEntry(draftId=1, textName='state', content=json.dumps(state)))
    db.session.commit()
    snapshot = prep.build_snapshot(1, 'draft-1', {'promptAttachments': [{'file_id': entry['document_id']}]})
    row = service.submit(1, {'conversation_id': 'draft-1', 'query': 'Compare the source, draft a summary, and check its support.',
        'document_snapshot': snapshot, 'assistant_mode': 'fast'})
    tasks.dispatch(row)
    # Existing controller tests permit ordinary candidate publication without
    # calling a live provider. Dedicated guard tests override this decision.
    monkeypatch.setattr(base, 'chat_completion_parse', lambda prompt, response_model, **kw: response_model(
        decision='not_forbidden', reason='No explicit editor restriction.', supporting_wording=''))
    # Forced draft normalization is a separate existing provider operation.
    # Keep controller staging/publication regressions offline as well.
    monkeypatch.setattr(document_drafts, 'resolve_court_settings', lambda *a, **kw: {'structural': {}, 'layout': {}})
    return row


def tick(row):
    tasks.advance.push_request(id=row.expected_task_id)
    try:
        result = tasks.advance.run(row.id)
    finally:
        tasks.advance.pop_request()
    db.session.expire_all()
    return result


def call(name, arguments, *, call_id='call_1'):
    return {'status': 'completed', 'output': [
        {'id': 'rs_test', 'type': 'reasoning', 'summary': [], 'encrypted_content': 'encrypted-state'},
        {'type': 'function_call', 'call_id': call_id, 'name': name, 'arguments': json.dumps(arguments)}]}


def choose(monkeypatch, name, arguments):
    monkeypatch.setattr(base, 'responses_completion', lambda request: call(name, arguments))


def operation(row, monkeypatch, name, arguments):
    choose(monkeypatch, name, arguments)
    assert tick(row) == {'continued': True}
    return tick(row)


def state(row):
    from backend.util.workspace_store import load
    return load(row.draft_id)


def last_result(row):
    item = tasks.steps(row).filter_by(kind='tool').order_by(None).order_by(AgentStep.sequence.desc()).first()
    return json.loads(item.result_json)


def test_result_driven_sequence_preserves_reasoning_and_publishes_once(agent, monkeypatch):
    uid = json.loads(agent.data_json)['document_snapshot']['files'][0]['file_id']
    requests = []
    def model(request):
        requests.append(request)
        outputs = [json.loads(item['output']) for item in request['input'] if item.get('type') == 'function_call_output']
        if not outputs:
            return call('search_sources', {'file_ids': [uid], 'queries': ['contract middle event']})
        if 'matches' in outputs[-1]:
            ranges = outputs[-1]['matches'][0]['ranges']
            return call('read_sources', {'artifact_id': None, 'ranges': [{'file_id': uid, **ranges[0]}], 'start': 0, 'end': None})
        # Reading the source changes the next action and final text.
        assert 'middle event' in outputs[-1]['text']
        return call('complete_task', {'response': 'The source concerns the middle event (agent.txt).', 'candidate_id': None})
    monkeypatch.setattr(base, 'responses_completion', model)
    for _ in range(6):
        result = tick(agent)
    assert result == {'completed': True}
    assert agent.status == 'completed'
    saved = state(agent)
    assert saved['draft']['title'] == 'Original'
    assert len(saved['chat_history']) == 1
    assert saved['chat_history_meta'][0]['workflow_completed']
    assert saved['chat_history_meta'][0]['agent_run']
    assert saved['chat_history_meta'][0]['document_scope']['version'] == 2
    assert json.loads(agent.payload_json)['agent_sources'][0]['file_id'] == uid
    assert requests[0]['model'] == 'gpt-6-luna'
    assert requests[-1]['parallel_tool_calls'] is False
    assert requests[-1]['store'] is False
    assert any(item.get('encrypted_content') == 'encrypted-state' for item in requests[-1]['input'])
    assert tick(agent) == {'stale': True}
    assert len(state(agent)['chat_history']) == 1
    assert tasks.steps(agent).filter_by(state='running').count() == 0


def test_real_drafting_operation_stages_candidate_without_chat_writes(agent, monkeypatch):
    result = document_drafts.MarkdownDraftResult(title='Candidate', markdown='# Candidate\n\nSupported new facts.', update_court_format=False)
    monkeypatch.setattr(document_drafts, 'write_document', lambda **kw: result)
    monkeypatch.setattr(everything, '_enqueue_brief_update', lambda *a, **kw: pytest.fail('Operation published a brief'))
    before = state(agent)
    operation(agent, monkeypatch, 'create_draft', {'instructions': 'Draft a short factual summary.', 'material_ids': []})
    assert state(agent) == before
    candidate = last_result(agent)['artifact_id']
    assert last_result(agent)['message'] == 'Draft candidate saved; editor unchanged.'
    saved = AgentArtifact.query.filter_by(id=candidate, kind='draft').one()
    assert json.loads(saved.content_json)['draft']['markdown'] == result.markdown
    assert json.loads(saved.content_json)['draft']['revision_id'] == candidate
    assert tasks.progress(agent)['steps'][0]['result']['message'] == 'Draft candidate saved; editor unchanged.'
    monkeypatch.setattr(everything, '_enqueue_brief_update', lambda *a, **kw: None)
    operation(agent, monkeypatch, 'complete_task', {'response': 'Prepared the summary.', 'candidate_id': candidate})
    assert state(agent)['draft']['title'] == 'Candidate'
    assert state(agent)['draft_before']['title'] == 'Original'
    assert tasks.steps(agent).filter_by(name='verify_draft').count() == 0


def test_document_versions_and_saved_material_ids_remain_distinct(agent, monkeypatch):
    accepted = json.loads(agent.data_json)['document_snapshot']['files'][0]
    file_id, prepared_id = accepted['file_id'], accepted['artifact_id']
    operation(agent, monkeypatch, 'list_documents', {})
    document = last_result(agent)['documents'][0]
    assert document['file_id'] == file_id
    assert document['prepared_artifact_id'] == prepared_id
    assert 'artifact_id' not in document
    initial_input = json.loads(json.loads(agent.payload_json)['agent_context']['input'][1]['content'])
    assert initial_input['documents'][0]['prepared_artifact_id'] == prepared_id
    assert 'artifact_id' not in initial_input['documents'][0]
    assert json.loads(agent.data_json)['document_snapshot']['files'][0] == accepted

    operation(agent, monkeypatch, 'search_sources', {'file_ids': [file_id], 'queries': ['middle event']})
    match = last_result(agent)['matches'][0]
    assert match['prepared_artifact_id'] == prepared_id and 'artifact_id' not in match
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': None,
        'ranges': [{'file_id': file_id, **match['ranges'][0]}], 'start': 0, 'end': None})
    source = last_result(agent)
    assert source['artifact_id'] not in (file_id, prepared_id)
    assert source['references'][0]['prepared_artifact_id'] == prepared_id
    assert 'artifact_id' not in source['references'][0]

    prompts = []
    def analyze(prompt, **kwargs):
        prompts.append(prompt)
        return 'Source and draft compared.'
    monkeypatch.setattr(base, 'chat_completion', analyze)
    operation(agent, monkeypatch, 'analyze_material', {'instructions': 'Compare the source and draft.',
        'material_ids': [source['artifact_id'], 'current_draft']})
    materials = json.loads(prompts[0].split('Materials (source evidence and prior generated work are labeled):\n', 1)[1])
    assert [(item['artifact_id'], item['kind']) for item in materials] == [
        (source['artifact_id'], 'source'), ('current_draft', 'draft')]
    assert materials[0]['references'][0]['file_id'] == file_id
    operation(agent, monkeypatch, 'verify_draft', {'instructions': 'Check the source identity and facts.',
        'material_ids': [source['artifact_id']], 'target': 'current_draft', 'check_citations': False})
    verification_materials = json.loads(prompts[-1].split('\nSupporting materials:\n', 1)[1])
    assert verification_materials[0]['artifact_id'] == source['artifact_id']
    assert verification_materials[0]['kind'] == 'source'
    assert verification_materials[0]['references'][0]['prepared_artifact_id'] == prepared_id


@pytest.mark.parametrize('identifier', ['file_id', 'artifact_id'])
def test_document_ids_are_not_silently_converted_into_materials(agent, monkeypatch, identifier):
    entry = json.loads(agent.data_json)['document_snapshot']['files'][0]
    monkeypatch.setattr(base, 'chat_completion', lambda *a, **kw: pytest.fail('Invalid material reached the LLM'))
    before = state(agent)
    operation(agent, monkeypatch, 'analyze_material', {'instructions': 'Analyze this source.',
        'material_ids': [entry[identifier]]})
    error = last_result(agent)
    assert error['error'] is True
    assert all(term in error['message'] for term in ('file_id', 'prepared_artifact_id', 'read_sources'))
    assert agent.status == 'running' and state(agent) == before
    assert tasks.progress(agent)['steps'][-1]['status'] == 'ready'
    assert tasks.progress(agent)['steps'][-1]['result']['error'] is True


@pytest.mark.parametrize('target_kind', ['current_draft', 'candidate'])
def test_preserve_original_stays_explicitly_unsupported_for_workspace_targets(agent, monkeypatch, target_kind):
    target = 'current_draft'
    if target_kind == 'candidate':
        candidate = document_drafts.MarkdownDraftResult(title='Candidate', markdown='Candidate facts.', update_court_format=False)
        monkeypatch.setattr(document_drafts, 'write_document', lambda **kw: candidate)
        operation(agent, monkeypatch, 'create_draft', {'instructions': 'Create a candidate.', 'material_ids': []})
        target = last_result(agent)['artifact_id']
    before = state(agent)
    monkeypatch.setattr(document_drafts, 'write_document', lambda **kw: pytest.fail('Unsupported mode reached drafting'))
    operation(agent, monkeypatch, 'revise_draft', {'instructions': 'Make targeted edits.', 'material_ids': [],
        'target': target, 'revision_mode': 'preserve_original'})
    assert last_result(agent)['error'] is True
    assert 'general_revision' in last_result(agent)['message']
    assert 'uploaded-draft' in last_result(agent)['message']
    assert state(agent) == before




def test_legacy_artifact_paging_preserves_saved_text_and_offsets(agent, monkeypatch):
    entry = json.loads(agent.data_json)['document_snapshot']['files'][0]
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': None,
        'ranges': [{'file_id': entry['file_id'], 'start': 0, 'end': 30}], 'start': 0, 'end': None})
    saved = db.session.get(AgentArtifact, last_result(agent)['artifact_id'])
    legacy = json.loads(saved.content_json)
    legacy['references'][0]['artifact_id'] = legacy['references'][0].pop('prepared_artifact_id')
    saved.content_json = service.encode(legacy)
    db.session.commit()
    expected = saved.content_json
    split = len(expected) // 2
    for start, end in [(0, split), (split, len(expected))]:
        operation(agent, monkeypatch, 'read_sources', {'artifact_id': saved.id, 'ranges': [], 'start': start, 'end': end})
        assert last_result(agent)['text'] == expected[start:end]
        assert last_result(agent)['total_characters'] == len(expected)
    assert db.session.get(AgentArtifact, saved.id).content_json == expected
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': 'current_draft', 'ranges': [], 'start': 0, 'end': None})
    assert last_result(agent)['text'] == service.encode({'kind': 'draft', 'draft': state(agent)['draft']})


def test_analysis_and_verification_are_saved_and_do_not_edit(agent, monkeypatch):
    before = state(agent)
    calls = []
    def complete(prompt, **kwargs):
        calls.append({'prompt': prompt, **kwargs})
        return 'One assertion needs source support.'
    monkeypatch.setattr(base, 'chat_completion', complete)
    operation(agent, monkeypatch, 'analyze_material', {'instructions': 'Compare the facts.', 'material_ids': ['current_draft']})
    analysis = last_result(agent)['artifact_id']
    assert 'Return analysis findings only.' in calls[0]['sys_prompt']
    assert 'The application saves your response as an analysis artifact.' in calls[0]['sys_prompt']
    saved_analysis = json.loads(db.session.get(AgentArtifact, analysis).content_json)
    assert saved_analysis['text'] == 'One assertion needs source support.'
    assert saved_analysis['material_ids'] == ['current_draft']
    operation(agent, monkeypatch, 'verify_draft', {'instructions': 'Check source support.', 'target': 'current_draft',
        'material_ids': [analysis], 'check_citations': False})
    result = last_result(agent)
    verification = result['artifact_id']
    report = json.loads(db.session.get(AgentArtifact, verification).content_json)
    summary = tasks.progress(agent)['steps'][-1]['result']
    assert result['target'] == report['target'] == summary['target'] == 'current_draft'
    assert report['instructions'] == 'Check source support.'
    assert report['check_citations'] is False and report['error'] is False
    assert 'checks_passed' not in report
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': verification, 'ranges': [], 'start': 0, 'end': None})
    assert json.loads(last_result(agent)['text']) == report
    assert 'needs source support' in report['text']
    assert state(agent) == before


def test_legacy_verification_report_reads_without_metadata_backfill(agent, monkeypatch):
    monkeypatch.setattr(base, 'chat_completion', lambda *a, **kw: 'Original saved findings.')
    operation(agent, monkeypatch, 'verify_draft', {'instructions': 'Check the draft.',
        'target': 'current_draft', 'material_ids': [], 'check_citations': False})
    saved = db.session.get(AgentArtifact, last_result(agent)['artifact_id'])
    # Earlier reports saved their target but did not include these scope/error
    # fields. Reading them must not rewrite their body or change paging offsets.
    saved.content_json = service.encode({'kind': 'verification', 'text': 'Original saved findings.',
        'target': 'current_draft', 'material_ids': []})
    db.session.commit()
    original = saved.content_json
    split = len(original) // 2
    for start, end in ((0, split), (split, len(original))):
        operation(agent, monkeypatch, 'read_sources', {'artifact_id': saved.id,
            'ranges': [], 'start': start, 'end': end})
        assert last_result(agent)['text'] == original[start:end]
        assert last_result(agent)['total_characters'] == len(original)
    assert db.session.get(AgentArtifact, saved.id).content_json == original


@pytest.mark.parametrize('compacted', [False, True])
def test_verification_receives_original_assignment_and_saved_answers(agent, monkeypatch, compacted):
    original = 'Verify the title exactly "Background." Keep the editor unchanged.'
    data = json.loads(agent.data_json)
    data['query'] = original
    agent.data_json = service.encode(data)
    db.session.commit()
    clarifications = [
        {'question': 'Keep the final period?', 'answer': 'Yes—use "Background." exactly.'},
        {'question': 'Which exhibit should I use?', 'answer': 'Use Exhibit B.\nKeep its qualifications.'},
    ]
    for item in clarifications:
        operation(agent, monkeypatch, 'request_user_input', {'question': item['question'], 'options': []})
        tasks.answer_question(agent, {'answer': item['answer']})
    # A failed question call is a saved error observation, not a user answer.
    operation(agent, monkeypatch, 'request_user_input', {'options': []})
    assert last_result(agent)['error'] is True
    through = tasks.steps(agent).order_by(None).order_by(AgentStep.sequence.desc()).first().sequence
    instructions = 'Check that the title is Background without a final period.'
    choose(monkeypatch, 'verify_draft', {'instructions': instructions, 'target': 'current_draft',
        'material_ids': ['current_draft'], 'check_citations': False})
    assert tick(agent) == {'continued': True}
    if compacted:
        # Verification must reread authoritative SQL answers, not rely on this
        # intentionally incomplete paraphrase of the controller's earlier work.
        payload = json.loads(agent.payload_json)
        payload['agent_context']['working_context'] = {
            'summary': 'Use the title Background without punctuation.', 'through_sequence': through}
        agent.payload_json = service.encode(payload)
        db.session.commit()
    saved_steps = [(s.id, s.arguments_json, s.result_json, s.summary_json)
                   for s in tasks.steps(agent).filter_by(state='ready').all()]
    before = state(agent)
    prompts = []

    def verify(prompt, **kwargs):
        prompts.append(prompt)
        return 'The original assignment and answers require the final period.'

    monkeypatch.setattr(base, 'chat_completion', verify)
    monkeypatch.setattr(base, 'chat_completion_parse', lambda *a, **kw: pytest.fail('No extra requirements extraction'))
    assert tick(agent) == {'continued': True}

    assert len(prompts) == 1
    prompt = prompts[0]
    assert prompt.startswith('Original assignment:\n' + original)
    saved_answers = json.loads(prompt.split('\nUser clarifications (agent questions provide context):\n', 1)[1].split('\nRequested checks:\n', 1)[0])
    assert saved_answers == clarifications
    assert prompt.split('\nRequested checks:\n', 1)[1].split('\nDraft:\n', 1)[0] == instructions
    checked_draft = json.loads(prompt.split('\nDraft:\n', 1)[1].split('\nSupporting materials:\n', 1)[0])
    assert checked_draft == before['draft']
    materials = json.loads(prompt.split('\nSupporting materials:\n', 1)[1])
    assert materials == [{'artifact_id': 'current_draft', 'kind': 'draft', 'draft': before['draft']}]
    assert state(agent) == before
    for uid, arguments, result, summary in saved_steps:
        saved = db.session.get(AgentStep, uid)
        assert (saved.arguments_json, saved.result_json, saved.summary_json) == (arguments, result, summary)
    report = json.loads(db.session.get(AgentArtifact, last_result(agent)['artifact_id']).content_json)
    assert report['target'] == 'current_draft'
    assert 'original assignment and answers' in report['text']


def test_verification_without_clarifications_uses_one_existing_provider_call(agent, monkeypatch):
    original = json.loads(agent.data_json)['query']
    prompts = []
    findings = 'The supplied draft needs source support.'

    def verify(prompt, **kwargs):
        prompts.append((prompt, kwargs['sys_prompt']))
        return findings

    monkeypatch.setattr(base, 'chat_completion', verify)

    assert operation(agent, monkeypatch, 'verify_draft', {'instructions': 'Check source support.',
        'target': 'current_draft', 'material_ids': [], 'check_citations': False}) == {'continued': True}

    assert len(prompts) == 1
    prompt, system_prompt = prompts[0]
    assert prompt.startswith('Original assignment:\n' + original)
    saved_answers = json.loads(prompt.split('\nUser clarifications (agent questions provide context):\n', 1)[1].split('\nRequested checks:\n', 1)[0])
    assert saved_answers == []
    # One provider call produces findings; application storage supplies the
    # artifact identity. Do not add another call or rewrite the returned prose.
    assert 'The application saves this response as a report artifact.' in system_prompt
    assert 'Do not describe save/publication status' in system_prompt
    result = last_result(agent)
    report = json.loads(db.session.get(AgentArtifact, result['artifact_id']).content_json)
    assert report['text'] == findings
    assert result['target'] == report['target'] == 'current_draft'
    assert tasks.steps(agent).filter_by(name='verify_draft').count() == 1


@pytest.mark.parametrize('audit,execution_error', [
    ({'error': True, 'error_message': 'Citation provider unavailable.'}, True),
    ({'error_message': 'Citation provider unavailable.'}, True),
    ({'authorities': [{'citation_as_written': 'Example v. Example', 'uncertainty': 'Unresolved in sources searched.'}]}, False),
])
def test_authority_lookup_precedes_verification_and_preserves_partial_findings(agent, monkeypatch, audit, execution_error):
    before = state(agent)
    observation = {}
    def lookup(ops, args):
        assert args.targets[0].draft_id == 'current_draft'
        assert args.mode == 'overview'
        uid = ops.save('authority_research', {'kind': 'authority_research',
            'purpose': args.purpose, 'authorities': audit.get('authorities', []), 'sources': [],
            'targets': [], 'references': [], 'coverage': {'partial': execution_error}})
        observation.update(copy.deepcopy(audit), research_id=uid, artifact_id=uid)
        return observation
    def report(prompt, **kwargs):
        assert observation, 'The report ran before authority lookup.'
        assert 'authority_research' in prompt
        return 'The substantive assertion needs source support.'
    monkeypatch.setattr(agent_tools.Operations, 'lookup_authorities', lookup)
    monkeypatch.setattr(base, 'chat_completion', report)

    assert operation(agent, monkeypatch, 'verify_draft', {'instructions': 'Check source support and citations.',
        'target': 'current_draft', 'material_ids': [], 'check_citations': True}) == {'continued': True}

    result = last_result(agent)
    saved = db.session.get(AgentArtifact, result['artifact_id'])
    saved_content = saved.content_json
    report = json.loads(saved_content)
    assert 'The substantive assertion needs source support.' in report['text']
    assert service.encode(observation) in report['text']
    assert report['research_id'] == observation['research_id']
    assert report['instructions'] == 'Check source support and citations.'
    assert report['check_citations'] is True and report['error'] is execution_error
    assert result['target'] == report['target'] == tasks.progress(agent)['steps'][-1]['result']['target'] == 'current_draft'
    assert 'checks_passed' not in report
    assert result.get('error', False) is execution_error
    assert tasks.progress(agent)['steps'][-1]['result'].get('error', False) is execution_error
    assert tasks.steps(agent).filter_by(kind='tool').one().state == 'ready'
    assert agent.status == 'running'
    assert state(agent) == before
    if execution_error:
        assert 'authority lookup' in result['message'].lower() and 'unavailable' in result['message'].lower()

    # The completed error observation goes back to the LLM; it is not an
    # automatic retry or a failure of the whole assignment.
    def decide(request):
        observations = [json.loads(item['output']) for item in request['input']
                        if item.get('type') == 'function_call_output']
        assert observations[-1] == result
        return call('complete_task', {'response': 'Saved findings and their limitations.', 'candidate_id': None})
    monkeypatch.setattr(base, 'responses_completion', decide)
    assert tick(agent) == {'continued': True}
    assert tick(agent) == {'completed': True}
    assert db.session.get(AgentArtifact, saved.id).content_json == saved_content


@pytest.mark.parametrize('options', [[], ['Use exhibit A', 'Use exhibit B']])
def test_question_survives_reload_and_resumes_same_call(agent, monkeypatch, app, options):
    operation(agent, monkeypatch, 'request_user_input', {'question': 'Which exhibit should I use?', 'options': options})
    response = app.test_client().get(f'/api/workspace/runs/{agent.id}')
    assert response.json['value']['confirmation']['kind'] == 'agent_question'
    assert agent.status == 'awaiting_user_confirmation'
    waiting = tasks.steps(agent).filter_by(state='waiting').one()
    call_id = waiting.call_id
    response = app.test_client().post(f'/api/workspace/runs/{agent.id}/confirm', json={'decision': {'answer': 'Use exhibit B'}})
    assert response.status_code == 200
    db.session.refresh(waiting)
    assert waiting.call_id == call_id and waiting.state == 'ready'
    assert json.loads(waiting.result_json)['answer'] == 'Use exhibit B'
    assert agent.status == 'running' and not state(agent)['chat_history']


@pytest.mark.parametrize('name,args', [
    ('search_sources', {'file_ids': ['outside'], 'queries': ['deadline']}),
    ('read_sources', {'artifact_id': 'other-assignment', 'ranges': [], 'start': 0, 'end': None}),
    ('complete_task', {'response': 'Done', 'candidate_id': 'outside'}),
    ('revise_draft', {'instructions': 'Edit', 'material_ids': [], 'target': 'current_draft', 'revision_mode': 'invented'}),
    ('unknown', {}),
])
def test_invalid_tool_calls_never_publish(agent, monkeypatch, name, args):
    before = state(agent)
    operation(agent, monkeypatch, name, args)
    assert agent.status in ('running', 'failed')
    assert state(agent) == before
    if agent.status == 'running':
        assert last_result(agent)['error'] is True


@pytest.mark.parametrize('failure', ['cancel', 'draft_changed', 'expired', 'context', 'timeout'])
def test_inflight_guards_prevent_candidate_publication(agent, monkeypatch, failure):
    before = state(agent)
    def provider(request):
        if failure == 'cancel':
            service.stop(agent, cancelled=True)
        elif failure == 'draft_changed':
            from backend.util import workspace_store
            current = workspace_store.load(agent.draft_id)
            edited = dict(current['draft'], title='User edit')
            workspace_store.update_draft(agent.draft_id, edited, expected_version=current['draft_version'])
            db.session.commit()
        elif failure == 'expired':
            agent.deadline = service.now() - timedelta(seconds=1); db.session.commit()
        elif failure == 'context':
            raise ContextBudgetExceeded('Context exhausted')
        else:
            raise SoftTimeLimitExceeded()
        return call('complete_task', {'response': 'Done', 'candidate_id': None})
    monkeypatch.setattr(base, 'responses_completion', provider)
    tick(agent)
    if agent.status == 'running':
        tick(agent)
    assert agent.status in ('cancelled', 'failed')
    assert state(agent)['chat_history'] == before['chat_history']
    assert state(agent)['draft']['title'] == ('User edit' if failure == 'draft_changed' else 'Original')


@pytest.mark.parametrize('interrupted_state', ['failed', 'running'])
def test_retry_reuses_completed_results_and_reruns_only_interrupted_decision(agent, monkeypatch, interrupted_state):
    operation(agent, monkeypatch, 'list_documents', {})
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': 'current_draft', 'ranges': [], 'start': 0, 'end': None})
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': 'invalid-id', 'ranges': [], 'start': 0, 'end': None})
    assert last_result(agent)['error'] is True
    completed = [(step.id, step.result_json, step.summary_json)
                 for step in tasks.steps(agent).filter_by(state='ready').all()]
    artifacts = [(saved.id, saved.content_json) for saved in AgentArtifact.query.all()]
    choose(monkeypatch, 'analyze_material', {'instructions': 'Analyze', 'material_ids': []})
    tick(agent)
    pending = tasks.steps(agent).filter_by(state='queued').one()
    identity = (pending.id, pending.sequence, pending.call_id, pending.arguments_json)
    pending.state = 'running'
    service.stop(agent, 'Worker stopped')
    assert pending.state == 'failed'
    assert json.loads(pending.summary_json)['message'] == 'Worker stopped'
    # Older saved requests can still have a running step after workflow failure.
    pending.state = interrupted_state
    db.session.commit()
    service.retry(agent)
    tasks.dispatch(agent)
    for uid, result, summary in completed:
        step = db.session.get(AgentStep, uid)
        assert (step.state, step.result_json, step.summary_json) == ('ready', result, summary)
    assert pending.state == 'queued'
    assert pending.result_json is None and pending.summary_json is None
    assert (pending.id, pending.sequence, pending.call_id, pending.arguments_json) == identity
    assert [(saved.id, saved.content_json) for saved in AgentArtifact.query.all()] == artifacts
    monkeypatch.setattr(base, 'chat_completion', lambda *a, **kw: 'Analysis completed')
    tick(agent)
    assert AgentArtifact.query.filter_by(run_id=agent.id, step_id=pending.id).count() == 1


@pytest.mark.parametrize('failure', ['timeout', 'provider_exception', 'worker_loss', 'lease_expiry'])
def test_interrupted_tool_records_failure_without_changing_completed_work(agent, monkeypatch, failure):
    operation(agent, monkeypatch, 'list_documents', {})
    ready = [(step.id, step.result_json, step.summary_json) for step in tasks.steps(agent).filter_by(state='ready').all()]
    choose(monkeypatch, 'analyze_material', {'instructions': 'Analyze', 'material_ids': []})
    tick(agent)
    pending = tasks.steps(agent).filter_by(state='queued').one()
    # A later requested call has not executed and must not appear completed.
    queued = tasks.add_step(agent, 'tool', 'list_documents', call_id='later_call', arguments={'raw': '{}'})
    db.session.commit()
    old_task_id = agent.expected_task_id
    old_token = 'lost-worker'
    if failure in ('worker_loss', 'lease_expiry'):
        pending.state = 'running'
        agent.lease_token, agent.lease_until = old_token, service.now() + timedelta(minutes=1)
        if failure == 'lease_expiry':
            agent.lease_until = service.now() - timedelta(seconds=1)
        db.session.commit()
        if failure == 'worker_loss':
            assert service.fail_task(old_task_id, 'Worker process was lost.') is True
        else:
            service.check_failure(agent)
    else:
        def interrupted(*a, **kw):
            if failure == 'timeout':
                raise SoftTimeLimitExceeded()
            raise RuntimeError('Provider connection interrupted.')
        monkeypatch.setattr(base, 'chat_completion', interrupted)
        if failure == 'provider_exception':
            with pytest.raises(RuntimeError, match='Provider connection interrupted'):
                tick(agent)
        else:
            assert tick(agent) == {'stopped': True}

    db.session.expire_all()
    assert agent.status == 'failed'
    assert pending.state == 'failed' and pending.result_json is None
    assert json.loads(pending.summary_json) == {'message': agent.error_message}
    assert tasks.progress(agent)['steps'][-2]['status'] == 'failed'
    assert tasks.steps(agent).filter_by(state='running').count() == 0
    assert queued.state == 'queued' and queued.result_json is None
    assert AgentArtifact.query.filter_by(run_id=agent.id).count() == 0
    for uid, result, summary in ready:
        step = db.session.get(AgentStep, uid)
        assert (step.state, step.result_json, step.summary_json) == ('ready', result, summary)

    service.retry(agent)
    tasks.dispatch(agent)
    new_task_id = agent.expected_task_id
    assert new_task_id != old_task_id
    assert service.fail_task(old_task_id, 'Late failure from the old attempt.') is False
    tasks.stop_owned_workflow(agent.id, old_token, 'Late worker failure.')
    assert agent.status == 'running' and agent.expected_task_id == new_task_id
    assert pending.state == 'queued' and pending.summary_json is None


def test_expired_question_records_failure_and_retry_waits_on_the_same_question(agent, monkeypatch):
    operation(agent, monkeypatch, 'request_user_input', {'question': 'Which exhibit?', 'options': []})
    question = tasks.steps(agent).filter_by(state='waiting').one()
    identity = (question.id, question.call_id, question.arguments_json)
    agent.deadline = service.now() - timedelta(seconds=1)
    db.session.commit()

    service.check_failure(agent)

    assert agent.status == 'failed' and question.state == 'failed'
    assert question.result_json is None
    assert json.loads(question.summary_json)['message'] == agent.error_message
    service.retry(agent)
    tasks.dispatch(agent)
    monkeypatch.setattr(base, 'responses_completion', lambda *a, **kw: pytest.fail('Reuse the saved question'))
    assert tick(agent) == {'awaiting_confirmation': True}
    assert question.state == 'waiting' and question.summary_json is None
    assert (question.id, question.call_id, question.arguments_json) == identity


@pytest.mark.parametrize('step_state,action', [('queued', 'cancel'), ('running', 'cancel'), ('waiting', 'confirm')])
def test_cancel_records_unfinished_step_state_and_returns_actual_outcome(agent, monkeypatch, app, step_state, action):
    operation(agent, monkeypatch, 'list_documents', {})
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': 'current_draft', 'ranges': [], 'start': 0, 'end': None})
    ready = [(step.id, step.result_json, step.summary_json) for step in tasks.steps(agent).filter_by(state='ready').all()]
    artifacts = {saved.id: saved.content_json for saved in AgentArtifact.query.all()}
    if step_state == 'waiting':
        operation(agent, monkeypatch, 'request_user_input', {'question': 'Which exhibit?', 'options': []})
    else:
        choose(monkeypatch, 'analyze_material', {'instructions': 'Analyze', 'material_ids': []})
        tick(agent)
    pending = tasks.steps(agent).filter(AgentStep.state != 'ready').one()
    identity = (pending.id, pending.sequence, pending.call_id, pending.arguments_json)
    old_task_id, old_token = agent.expected_task_id, 'cancelled-worker'
    pending.state = step_state
    agent.lease_token, agent.lease_until = old_token, service.now() + timedelta(minutes=1)
    db.session.commit()
    monkeypatch.setattr(__import__('celery').current_app.control, 'revoke', lambda *a, **kw: None)

    response = app.test_client().post(f'/api/workspace/runs/{agent.id}/{action}',
        json={'decision': {'cancelled': True}} if action == 'confirm' else {})

    assert response.status_code == 200
    assert response.json == {'run_id': agent.id, 'status': 'cancelled', 'cancelled': True}
    assert agent.status == 'cancelled' and pending.state == 'cancelled'
    assert pending.result_json is None and json.loads(pending.summary_json)['message']
    assert tasks.steps(agent).filter(AgentStep.state.in_(['queued', 'running', 'waiting'])).count() == 0
    for uid, result, summary in ready:
        step = db.session.get(AgentStep, uid)
        assert (step.state, step.result_json, step.summary_json) == ('ready', result, summary)
    assert {saved.id: saved.content_json for saved in AgentArtifact.query.all()} == artifacts

    response = app.test_client().post(f'/api/workspace/runs/{agent.id}/retry')
    assert response.status_code == 200 and response.json['run_id'] == agent.id
    assert agent.status == 'running' and agent.expected_task_id != old_task_id
    assert pending.state == 'queued' and pending.result_json is None and pending.summary_json is None
    assert (pending.id, pending.sequence, pending.call_id, pending.arguments_json) == identity
    assert {saved.id: saved.content_json for saved in AgentArtifact.query.all()} == artifacts
    assert service.fail_task(old_task_id, 'Late cancellation failure.') is False
    tasks.stop_owned_workflow(agent.id, old_token, 'Late worker failure.')
    assert agent.status == 'running'
    monkeypatch.setattr(base, 'responses_completion', lambda *a, **kw: pytest.fail('Resume the saved tool call'))
    monkeypatch.setattr(base, 'chat_completion', lambda *a, **kw: 'Analysis completed')
    assert tick(agent) == ({'awaiting_confirmation': True} if step_state == 'waiting' else {'continued': True})
    assert pending.state == ('waiting' if step_state == 'waiting' else 'ready')
    for uid, result, summary in ready:
        step = db.session.get(AgentStep, uid)
        assert (step.state, step.result_json, step.summary_json) == ('ready', result, summary)
    for uid, content in artifacts.items():
        assert db.session.get(AgentArtifact, uid).content_json == content


@pytest.mark.parametrize('changed', ['draft', 'source', 'active_request'])
def test_cancelled_agent_retry_keeps_existing_scope_and_draft_guards(agent, changed):
    service.stop(agent, cancelled=True)
    if changed == 'draft':
        from backend.util import workspace_store
        current = workspace_store.load(agent.draft_id)
        edited = dict(current['draft'], title='User edit')
        workspace_store.update_draft(agent.draft_id, edited, expected_version=current['draft_version'])
        db.session.commit()
    elif changed == 'source':
        from backend.models import DocumentPreparation
        uid = json.loads(agent.data_json)['document_snapshot']['files'][0]['file_id']
        db.session.get(DocumentPreparation, uid).generation += 1
        db.session.commit()
    else:
        service.submit(agent.user_id, json.loads(agent.data_json))
    exception = prep.PreparationError if changed == 'source' else ValueError
    with pytest.raises(exception, match={'draft': 'draft changed', 'source': 'document changed', 'active_request': 'Another request'}[changed]):
        service.retry(agent)
    db.session.rollback()
    assert agent.status == 'cancelled'




@pytest.mark.parametrize('action', ['cancel', 'confirm'])
def test_cancel_after_failure_returns_failed_without_overwriting_the_reason(agent, monkeypatch, app, action):
    service.stop(agent, 'The worker failed before cancellation arrived.')
    saved_result = agent.result_json
    monkeypatch.setattr(__import__('celery').current_app.control, 'revoke', lambda *a, **kw: pytest.fail('No active execution'))

    response = app.test_client().post(f'/api/workspace/runs/{agent.id}/{action}',
        json={'decision': {'cancelled': True}} if action == 'confirm' else {})

    assert response.status_code == 200
    assert response.json == {'run_id': agent.id, 'status': 'failed', 'cancelled': False}
    assert agent.result_json == saved_result
    assert agent.error_message == 'The worker failed before cancellation arrived.'


def test_progress_and_artifact_route_omit_model_reasoning_and_enforce_owner(agent, monkeypatch, app):
    monkeypatch.setattr(base, 'chat_completion', lambda *a, **kw: 'Findings')
    operation(agent, monkeypatch, 'analyze_material', {'instructions': 'Analyze', 'material_ids': []})
    progress = app.test_client().get(f'/api/workspace/runs/{agent.id}').json
    assert 'encrypted-state' not in json.dumps(progress)
    assert progress['info']['agent']['steps'][0]['operation'] == 'analyze_material'
    uid = last_result(agent)['artifact_id']
    assert app.test_client().get(f'/api/workspace/runs/{agent.id}/artifacts/{uid}').json['content']['text'] == 'Findings'
    monkeypatch.setattr(routes, 'current_user', SimpleNamespace(id=2))
    assert app.test_client().get(f'/api/workspace/runs/{agent.id}/artifacts/{uid}').status_code == 404


def test_tool_history_context_preflight_runs_before_provider(monkeypatch):
    from backend.ai import token_budget
    monkeypatch.setitem(token_budget.CONTEXT_LIMITS, 'test-model', 10)
    monkeypatch.setattr(base, 'get_openai_client', lambda: pytest.fail('Oversized tool history reached provider'))
    with pytest.raises(ContextBudgetExceeded):
        base.responses_completion({'model': 'test-model', 'input': [
            {'type': 'function_call_output', 'call_id': 'c', 'output': 'large output'}], 'tools': agent_tools.definitions()})


def test_verification_finding_can_drive_a_real_candidate_revision(agent, monkeypatch):
    original = copy.deepcopy(state(agent)['draft'])
    first = document_drafts.MarkdownDraftResult(title='Original', markdown='Original facts.', update_court_format=False)
    monkeypatch.setattr(document_drafts, 'write_document', lambda **kw: first)
    operation(agent, monkeypatch, 'create_draft', {'instructions': 'Create candidate A.', 'material_ids': []})
    candidate_a = last_result(agent)['artifact_id']
    verification_calls = []
    monkeypatch.setattr(base, 'chat_completion', lambda prompt, **kw: verification_calls.append(prompt) or 'The opening sentence needs the qualifier alleged.')
    operation(agent, monkeypatch, 'verify_draft', {'target': candidate_a, 'instructions': 'Check qualifications.',
        'material_ids': [], 'check_citations': False})
    findings = last_result(agent)['artifact_id']
    saved_findings = db.session.get(AgentArtifact, findings).content_json
    revised = document_drafts.MarkdownDraftResult(title='Original', markdown='The alleged facts.', update_court_format=False)
    monkeypatch.setattr(document_drafts, 'write_document', lambda **kw: revised)
    operation(agent, monkeypatch, 'revise_draft', {'target': candidate_a, 'instructions': 'Apply the saved verification finding.',
        'material_ids': [findings], 'revision_mode': 'general_revision'})
    candidate_b = last_result(agent)['artifact_id']
    assert candidate_b != candidate_a
    assert db.session.get(AgentArtifact, findings).content_json == saved_findings
    assert json.loads(saved_findings)['target'] == candidate_a
    assert state(agent)['draft'] == original

    def finish(request):
        observations = [json.loads(item['output']) for item in request['input']
                        if item.get('type') == 'function_call_output']
        reports = [item for item in observations if item.get('kind') == 'verification']
        assert len(reports) == 1
        assert reports[0]['artifact_id'] == findings and reports[0]['target'] == candidate_a
        # The controller may finish with B after using A's findings. No
        # mandatory verification step or automatic relabeling of A's report.
        return call('complete_task', {'response': 'Added the requested qualifier.', 'candidate_id': candidate_b})

    monkeypatch.setattr(base, 'responses_completion', finish)
    assert tick(agent) == {'continued': True}
    assert tick(agent) == {'completed': True}
    assert tasks.steps(agent).filter_by(name='verify_draft').count() == 1
    assert len(verification_calls) == 1
    assert db.session.get(AgentArtifact, findings).content_json == saved_findings
    assert 'alleged' in json.dumps(state(agent)['draft'])
    assert state(agent)['draft_before'] == original


def test_publication_marker_recovers_worker_loss_without_duplicate_answer(agent, monkeypatch):
    operation(agent, monkeypatch, 'complete_task', {'response': 'Analysis finished.', 'candidate_id': None})
    # Simulate death after the atomic state/step commit but before bookkeeping.
    agent.status, agent.active_draft_id = 'running', agent.draft_id
    agent.lease_until = service.now() - timedelta(seconds=1)
    db.session.commit()
    assert service.check_failure(agent).status == 'completed'
    assert len(state(agent)['chat_history']) == 1


@pytest.mark.parametrize('action', ['cancel', 'confirm'])
@pytest.mark.parametrize('crash_gap', [False, True])
def test_cancel_after_publication_returns_completed_without_rerun_or_revocation(agent, monkeypatch, app, action, crash_gap):
    candidate_result = document_drafts.MarkdownDraftResult(title='Published candidate', markdown='Final supported facts.', update_court_format=False)
    monkeypatch.setattr(document_drafts, 'write_document', lambda **kw: candidate_result)
    operation(agent, monkeypatch, 'create_draft', {'instructions': 'Draft the summary.', 'material_ids': []})
    candidate = last_result(agent)['artifact_id']
    operation(agent, monkeypatch, 'complete_task', {'response': 'Analysis finished.', 'candidate_id': candidate})
    published = copy.deepcopy(state(agent))
    assert published['draft']['title'] == 'Published candidate'
    assert published['draft_before']['title'] == 'Original'
    steps = [(step.id, step.state, step.result_json) for step in tasks.steps(agent).all()]
    if crash_gap:
        # Publication already saved the answer/marker; only workflow bookkeeping
        # was lost when the worker stopped.
        agent.status, agent.active_draft_id = 'running', agent.draft_id
        agent.lease_until = service.now() - timedelta(seconds=1)
        db.session.commit()
    monkeypatch.setattr(__import__('celery').current_app.control, 'revoke', lambda *a, **kw: pytest.fail('Published work is complete'))
    monkeypatch.setattr(tasks.advance, 'apply_async', lambda *a, **kw: pytest.fail('Do not schedule another execution'))
    monkeypatch.setattr(base, 'responses_completion', lambda *a, **kw: pytest.fail('Do not repeat a model call'))

    response = app.test_client().post(f'/api/workspace/runs/{agent.id}/{action}',
        json={'decision': {'cancelled': True}} if action == 'confirm' else {})

    assert response.status_code == 200
    assert response.json == {'run_id': agent.id, 'status': 'completed', 'cancelled': False}
    assert agent.status == 'completed' and agent.active_draft_id is None
    assert json.loads(agent.result_json)['response'] == 'Analysis finished.'
    assert state(agent) == published
    assert len(state(agent)['chat_history']) == 1
    assert [(step.id, step.state, step.result_json) for step in tasks.steps(agent).all()] == steps


def test_stale_worker_cannot_save_a_late_operation_result(agent, monkeypatch):
    choose(monkeypatch, 'analyze_material', {'instructions': 'Analyze', 'material_ids': []})
    tick(agent)
    def provider(*a, **kw):
        service.stop(agent, cancelled=True)
        return 'Late findings'
    monkeypatch.setattr(base, 'chat_completion', provider)
    tick(agent)
    assert agent.status == 'cancelled'
    pending = tasks.steps(agent).filter_by(kind='tool').one()
    assert pending.state == 'cancelled' and pending.result_json is None
    assert json.loads(pending.summary_json)['message']
    assert AgentArtifact.query.filter_by(run_id=agent.id).count() == 0
    assert not state(agent)['chat_history']


def test_changed_source_stops_run_without_using_saved_scope(agent, monkeypatch):
    from backend.models import DocumentPreparation
    uid = json.loads(agent.data_json)['document_snapshot']['files'][0]['file_id']
    operation(agent, monkeypatch, 'list_documents', {})
    db.session.get(DocumentPreparation, uid).generation += 1
    db.session.commit()
    choose(monkeypatch, 'complete_task', {'response': 'Done', 'candidate_id': None})
    tick(agent)
    assert agent.status == 'failed'
    assert not state(agent)['chat_history']


def test_controller_has_no_fifty_call_cutoff(agent, monkeypatch):
    choose(monkeypatch, 'read_sources', {'artifact_id': 'current_draft', 'ranges': [], 'start': 0, 'end': None})
    for _ in range(51):
        assert tick(agent) == {'continued': True}
        assert tick(agent) == {'continued': True}
    assert tasks.steps(agent).filter_by(kind='tool', state='ready').count() == 51
    assert agent.status == 'running'
