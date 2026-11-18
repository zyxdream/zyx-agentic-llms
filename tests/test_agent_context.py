"""Working-context compaction against real SQL; provider/broker calls are doubled."""
import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from backend import agent_tasks as tasks
from backend.ai import agent_tools, base, token_budget
from backend.database import db
from backend.models import AgentArtifact, AgentStep
from backend.util import agent_context, workspace_service as service
from tests.test_agent_controller import agent, choose, state, tick
from tests.test_document_preparation import app


@pytest.fixture
def context_row(agent, monkeypatch):
    monkeypatch.setattr(base, 'chat_completion_parse', lambda *a, **kw: pytest.fail('Unexpected compaction'))
    choose(monkeypatch, 'list_documents', {})
    assert tick(agent) == {'continued': True}
    assert tick(agent) == {'continued': True}
    # Leave working-history headroom above the complete, growing tool catalogue.
    # Large reads below still force compaction and exercise the actual preflight.
    monkeypatch.setitem(token_budget.CONTEXT_LIMITS, 'context-test', 16000)
    monkeypatch.setattr(agent_context, 'SUMMARY_TOKENS', 128)
    return agent


def request_for(row):
    return {'model': 'context-test', 'max_output_tokens': 256,
        'input': copy.deepcopy(json.loads(row.payload_json)['agent_context']['input']),
        'tools': agent_tools.definitions()}


def add_group(row, payloads, *, names=None, arguments=None):
    """Store a completed decision and all of its completed tool observations."""
    names = names or ['read_sources'] * len(payloads)
    arguments = arguments or [{} for _ in payloads]
    model = tasks.add_step(row, 'model', 'choose_next_action')
    calls = [{'type': 'function_call', 'call_id': f'call_{model.sequence}_{index}',
        'name': name, 'arguments': json.dumps(args)}
        for index, (name, args) in enumerate(zip(names, arguments))]
    model.result_json = service.encode({'output': [
        {'id': f'rs_{model.sequence}', 'type': 'reasoning', 'summary': [],
         'encrypted_content': f'ciphertext-{model.sequence}'}, *calls]})
    model.state = 'ready'
    tools = []
    for call, value in zip(calls, payloads):
        step = tasks.add_step(row, 'tool', call['name'], call_id=call['call_id'],
            arguments={'raw': call['arguments']})
        step.result_json, step.state = service.encode(value), 'ready'
        tools.append(step)
    db.session.commit()
    return model, tools


def raw_history(row):
    history = copy.deepcopy(json.loads(row.payload_json)['agent_context']['input'])
    for step in tasks.steps(row).filter_by(state='ready').all():
        if step.kind == 'model':
            history.extend(json.loads(step.result_json)['output'])
        else:
            history.append({'type': 'function_call_output', 'call_id': step.call_id, 'output': step.result_json})
    return history


def claim(row):
    token = tasks.claim_workflow(row.id, task_id=row.expected_task_id, task_field='expected_task_id')
    assert token
    return token


def prepare(row, request=None, *, token=None):
    token = token or claim(row)
    with service.execution(row.id, token):
        return agent_context.prepare_input(row, request or request_for(row))


def summary_provider(monkeypatch, text='Keep source-material and candidate-1; the disputed total remains unresolved.'):
    prompts = []
    def summarize(prompt, schema, **kwargs):
        prompts.append((prompt, kwargs))
        return SimpleNamespace(summary=text)
    monkeypatch.setattr(base, 'chat_completion_parse', summarize)
    return prompts


def checkpoint(row):
    db.session.refresh(row)
    return json.loads(row.payload_json)['agent_context'].get('working_context')


def completed_bytes(row):
    return [(s.id, s.arguments_json, s.result_json, s.summary_json, s.state)
        for s in tasks.steps(row).filter_by(state='ready').all()]


def test_small_history_is_exact_replay_without_compaction(context_row):
    expected = raw_history(context_row)
    result = prepare(context_row)
    assert [item for item in result if not item.get('content', '').startswith('Application working-draft state (')] == expected
    assert checkpoint(context_row) is None


def test_unregistered_model_preserves_replay_without_estimating(context_row, monkeypatch):
    row = context_row
    add_group(row, [{'text': 'large source ' * 8000}])
    request = {**request_for(row), 'model': 'unregistered-context-model'}
    monkeypatch.setattr(agent_context, 'estimate_text_request', lambda *a, **kw: pytest.fail('Unregistered model was estimated'))
    result = prepare(row, request)
    assert [item for item in result if not item.get('content', '').startswith('Application working-draft state (')] == raw_history(row)
    assert checkpoint(row) is None


def test_repeated_large_reads_are_bounded_and_preserve_saved_steps_and_artifacts(context_row, monkeypatch):
    row = context_row
    _, tools = add_group(row, [{'artifact_id': 'source-material', 'kind': 'source', 'text': 'original passage ' * 6000}])
    artifact = AgentArtifact(id='source-material', run_id=row.id, step_id=tools[0].id,
        kind='source', content_json=service.encode({'kind': 'source', 'text': 'Exact original source body.'}), created_at=service.now())
    db.session.add(artifact); db.session.commit()
    saved_steps, saved_artifact, editor = completed_bytes(row), artifact.content_json, state(row)
    prompts = summary_provider(monkeypatch)
    token = claim(row)
    for index in range(3):
        if index:
            add_group(row, [{'artifact_id': 'source-material', 'text': 'original passage ' * 6000}])
        result = prepare(row, token=token)
        request = {**request_for(row), 'input': result}
        assert token_budget.estimate_text_request(request) <= agent_context.COMPACTION_RATIO * token_budget.usable_input_budget(request)
        token_budget.validate_text_request(request)
    assert prompts and all(kwargs['model'] == 'context-test' for _, kwargs in prompts)
    assert all('ciphertext-' not in prompt for prompt, _ in prompts)
    assert completed_bytes(row)[:len(saved_steps)] == saved_steps
    assert db.session.get(AgentArtifact, artifact.id).content_json == saved_artifact
    assert state(row) == editor


def test_retained_suffix_keeps_whole_multi_call_group_and_encrypted_items(context_row, monkeypatch):
    row = context_row
    add_group(row, [{'text': 'old source ' * 8000}])
    latest, tools = add_group(row, [{'text': 'first recent result'}, {'text': 'second recent result'}])
    summary_provider(monkeypatch)
    result = prepare(row)
    calls = [item['call_id'] for item in result if item.get('type') == 'function_call']
    outputs = [item['call_id'] for item in result if item.get('type') == 'function_call_output']
    assert calls == outputs == [step.call_id for step in tools]
    assert {'id': f'rs_{latest.sequence}', 'type': 'reasoning', 'summary': [],
        'encrypted_content': f'ciphertext-{latest.sequence}'} in result
    assert checkpoint(row)['through_sequence'] < latest.sequence


def test_latest_oversized_read_is_compacted_instead_of_stranding_run(context_row, monkeypatch):
    row = context_row
    markers = ['BEGIN_CONTEXT_SOURCE', 'MIDDLE_CONTEXT_SOURCE', 'END_CONTEXT_SOURCE']
    source = markers[0] + 'very large source ' * 8000 + markers[1] + 'very large source ' * 8000 + markers[2]
    _, tools = add_group(row, [{'artifact_id': 'source-material', 'text': source}])
    before = completed_bytes(row)
    prompts = summary_provider(monkeypatch)
    # Force a small decision-input budget while leaving the existing 32K
    # structured-summary output reserve enough room for each 24K source batch.
    monkeypatch.setitem(token_budget.CONTEXT_LIMITS, 'gpt-6-luna', 141000)
    request = {**request_for(row), 'model': 'gpt-6-luna'}
    request.pop('max_output_tokens')
    result = prepare(row, request)
    assert checkpoint(row)['through_sequence'] == tools[-1].sequence
    assert all(item.get('type') != 'function_call_output' for item in result)
    token_budget.validate_text_request({**request, 'input': result})
    assert len(prompts) > 1
    assert all(any(marker in prompt for prompt, _ in prompts) for marker in markers)
    for prompt, kwargs in prompts:
        token_budget.validate_text_request({'model': kwargs['model'], 'text_format': agent_context.WorkingSummary,
            'max_output_tokens': kwargs['max_output_tokens'], 'input': [
                {'role': 'system', 'content': kwargs['sys_prompt']}, {'role': 'user', 'content': prompt}]})
    assert completed_bytes(row) == before


def test_compacted_clarification_and_original_assignment_remain_verbatim(context_row, monkeypatch):
    row = context_row
    original = copy.deepcopy(json.loads(row.payload_json)['agent_context']['input'])
    question = 'Which exhibit and jurisdiction should control this summary?'
    answer = 'Use Exhibit B — California; preserve uncertainty.'
    add_group(row, [{'answer': answer}], names=['request_user_input'],
        arguments=[{'question': question, 'options': []}])
    add_group(row, [{'text': 'large later source ' * 8000}])
    summary_provider(monkeypatch, 'A deliberately short working summary.')
    result = prepare(row)
    assert result[:len(original)] == original
    assert all(item.get('role') not in ('system', 'developer') or
               item.get('content', '').startswith('Application working-draft state (')
               for item in result[len(original):])
    contents = '\n'.join(item.get('content', '') for item in result if isinstance(item.get('content'), str))
    assert question in contents and answer in contents


def test_compaction_preserves_report_target_without_marking_a_later_candidate_checked(context_row, monkeypatch):
    row = context_row
    candidate_a, report_id, candidate_b = 'candidate-a', 'report-for-a', 'candidate-b'
    _, first_steps = add_group(row, [{'artifact_id': candidate_a, 'kind': 'draft'}], names=['create_draft'])
    _, report_steps = add_group(row, [{'artifact_id': report_id, 'kind': 'verification',
        'target': candidate_a, 'message': 'Read the saved findings.'}], names=['verify_draft'],
        arguments=[{'target': candidate_a, 'instructions': 'Check qualifications.', 'check_citations': False}])
    _, source_steps = add_group(row, [{'text': 'older supporting passages ' * 6000}])
    latest, revised_steps = add_group(row, [{'artifact_id': candidate_b, 'kind': 'draft'}], names=['revise_draft'],
        arguments=[{'target': candidate_a, 'instructions': 'Apply the findings.'}])
    for uid, kind, step, body in [
        (candidate_a, 'draft', first_steps[0], {'kind': 'draft', 'draft': {'title': 'A', 'sections': []}}),
        (report_id, 'verification', report_steps[0], {'kind': 'verification', 'target': candidate_a,
            'instructions': 'Check qualifications.', 'check_citations': False, 'error': False,
            'text': 'A qualification remains unresolved.'}),
        (candidate_b, 'draft', revised_steps[0], {'kind': 'draft', 'revision_target': candidate_a,
            'draft': {'title': 'B', 'sections': []}}),
    ]:
        db.session.add(AgentArtifact(id=uid, run_id=row.id, step_id=step.id,
            kind=kind, content_json=service.encode(body), created_at=service.now()))
    db.session.commit()
    saved_steps = completed_bytes(row)
    saved_artifacts = {item.id: item.content_json for item in AgentArtifact.query.all()}
    editor = state(row)
    # Include fixed-catalogue headroom while still compacting the large source
    # group and retaining the final small model/tool group verbatim.
    monkeypatch.setitem(token_budget.CONTEXT_LIMITS, 'gpt-6-luna', 144000)
    requests = []
    notes = (f'Verification report {report_id} checked {candidate_a} only. '
             f'The revised current candidate is {candidate_b}; no saved report checks it. '
             'The controller decides whether another check is useful.')

    def summarize(prompt, schema, **kwargs):
        requests.append(prompt)
        completed = json.loads(prompt.split('Next completed work:\n', 1)[1])
        reports = [item['result'] for item in completed
                   if item['operation'] == 'verify_draft' and 'result' in item]
        assert reports == [{'artifact_id': report_id, 'kind': 'verification',
                            'target': candidate_a, 'message': 'Read the saved findings.'}]
        guidance = kwargs['sys_prompt'] + prompt.split('Original assignment and context:\n', 1)[0]
        assert 'verification' in guidance.lower() and 'target' in guidance.lower()
        return SimpleNamespace(summary=notes)

    monkeypatch.setattr(base, 'chat_completion_parse', summarize)
    request = {**request_for(row), 'model': 'gpt-6-luna'}
    request.pop('max_output_tokens')
    token = claim(row)
    result = prepare(row, request, token=token)
    assert checkpoint(row)['through_sequence'] >= source_steps[0].sequence
    assert checkpoint(row)['through_sequence'] < latest.sequence
    assert len(requests) == 1
    assert notes in [item.get('content', '').removeprefix(agent_context.NOTES_LABEL) for item in result]
    # The old report is now represented by notes, while the newer candidate is
    # still a separate raw result. No report is copied or relabeled for B.
    outputs = [json.loads(item['output']) for item in result if item.get('type') == 'function_call_output']
    assert outputs == [{'artifact_id': candidate_b, 'kind': 'draft'}]
    from tests.test_agent_controller import call

    def decide(model_request):
        assert notes in [item.get('content', '').removeprefix(agent_context.NOTES_LABEL)
                         for item in model_request['input']]
        return call('complete_task', {'response': 'The report concerns A; B is a later revision.', 'candidate_id': None})

    monkeypatch.setattr(base, 'responses_completion', decide)
    with service.execution(row.id, token):
        tasks.model_request(row)
    assert len(requests) == 1  # Replaying the checkpoint adds no compaction call.
    assert completed_bytes(row) == saved_steps
    assert {item.id: item.content_json for item in AgentArtifact.query.all()} == saved_artifacts
    assert state(row) == editor


def test_failed_question_calls_do_not_become_pinned_clarifications(context_row, monkeypatch):
    row = context_row
    model, question_steps = add_group(row, [
        {'error': True, 'message': 'Question is required.'},
        {'error': True, 'message': 'Arguments were not valid JSON.'},
    ], names=['request_user_input', 'request_user_input'], arguments=[{}, {}])
    question_steps[-1].arguments_json = service.encode({'raw': '{invalid json'})
    model_result = json.loads(model.result_json)
    model_result['output'][-1]['arguments'] = '{invalid json'
    model.result_json = service.encode(model_result)
    db.session.commit()
    add_group(row, [{'text': 'later original source ' * 8000}])
    before = completed_bytes(row)
    summary_provider(monkeypatch, 'Earlier question calls failed; inspect the saved operation errors if needed.')
    result = prepare(row)
    assert checkpoint(row)['through_sequence'] >= question_steps[-1].sequence
    assert all('User clarification responses' not in item.get('content', '') for item in result)
    assert completed_bytes(row) == before


@pytest.mark.parametrize('bad_result', ['missing', 'empty', 'oversized', 'incomplete'])
def test_invalid_summary_preserves_previous_valid_checkpoint(context_row, monkeypatch, bad_result):
    row = context_row
    payload = json.loads(row.payload_json)
    prior = {'summary': 'Previously saved valid working notes.', 'through_sequence': 1}
    payload['agent_context']['working_context'] = prior
    row.payload_json = service.encode(payload); db.session.commit()
    add_group(row, [{'text': 'new source ' * 8000}])
    def invalid(*args, **kwargs):
        if bad_result == 'incomplete':
            raise token_budget.IncompleteStructuredOutput('Output was incomplete')
        if bad_result == 'missing':
            return None
        return SimpleNamespace(summary='' if bad_result == 'empty' else 'oversized ' * 1000)
    monkeypatch.setattr(base, 'chat_completion_parse', invalid)
    before = completed_bytes(row)
    with pytest.raises(agent_context.CompactionError):
        prepare(row)
    assert checkpoint(row) == prior
    assert completed_bytes(row) == before


def test_retry_reuses_successful_compaction_batches(context_row, monkeypatch):
    row = context_row
    monkeypatch.setattr(agent_context, 'BATCH_TOKENS', 300)
    add_group(row, [{'artifact_id': 'source-material', 'text': 'original evidence ' * 7000}])
    prompts = []
    def interrupted(prompt, schema, **kwargs):
        prompts.append(prompt)
        if len(prompts) == 2:
            raise SoftTimeLimitExceeded()
        return SimpleNamespace(summary='Keep source-material; one disputed fact remains unresolved.')
    monkeypatch.setattr(base, 'chat_completion_parse', interrupted)
    before = completed_bytes(row)
    with pytest.raises(SoftTimeLimitExceeded):
        prepare(row)
    first_prompt = prompts[0]
    assert checkpoint(row) is None
    assert json.loads(row.payload_json)['agent_context']['compaction_parts']
    service.stop(row, 'Worker stopped during compaction')
    service.retry(row)
    tasks.dispatch(row)
    result = prepare(row)
    assert prompts.count(first_prompt) == 1
    assert checkpoint(row)
    assert not json.loads(row.payload_json)['agent_context'].get('compaction_parts')
    token_budget.validate_text_request({**request_for(row), 'input': result})
    assert completed_bytes(row) == before


@pytest.mark.parametrize('interruption', ['cancel', 'stale', 'expired'])
def test_interrupted_worker_cannot_save_summary(context_row, monkeypatch, interruption):
    row = context_row
    add_group(row, [{'text': 'source text ' * 8000}])
    def interrupted(*args, **kwargs):
        if interruption == 'cancel':
            service.stop(row, cancelled=True)
        elif interruption == 'stale':
            row.lease_token = 'replacement-worker'
            db.session.commit()
        else:
            row.deadline = service.now() - timedelta(seconds=1)
            db.session.commit()
        return SimpleNamespace(summary='This late summary must not be saved.')
    monkeypatch.setattr(base, 'chat_completion_parse', interrupted)
    with pytest.raises(service.WorkflowStopped):
        prepare(row)
    assert checkpoint(row) is None
    assert not json.loads(row.payload_json)['agent_context'].get('compaction_parts')


def test_budget_helpers_include_tool_schema_and_output_reserve(monkeypatch):
    monkeypatch.setitem(token_budget.CONTEXT_LIMITS, 'context-test', 12000)
    request = {'model': 'context-test', 'input': [{'role': 'user', 'content': 'A small request.'}], 'max_output_tokens': 256}
    with_tools = {**request, 'tools': agent_tools.definitions()}
    assert token_budget.estimate_text_request(with_tools) > token_budget.estimate_text_request(request)
    assert token_budget.usable_input_budget(request) - token_budget.usable_input_budget({**request, 'max_output_tokens': 1256}) == 1000


def test_controller_finishes_from_compacted_synthetic_working_state(context_row, monkeypatch):
    row = context_row
    source_id, candidate_id = 'source-material', 'candidate-1'
    facts = 'The order is $240 and June 10; the $260 invoice and June 12 date are disputed.'
    _, source_steps = add_group(row, [{'artifact_id': source_id, 'references': [{'file_id': 'order-file', 'start': 0, 'end': 70}],
        'text': facts + '\n' + 'supporting source passages ' * 9000}])
    _, candidate_steps = add_group(row, [{'artifact_id': candidate_id, 'message': 'Draft candidate saved; editor unchanged.'}])
    for uid, kind, step, body in [
        (source_id, 'source', source_steps[0], {'kind': 'source', 'text': facts}),
        (candidate_id, 'draft', candidate_steps[0], {'kind': 'draft', 'draft': {
            'title': 'Candidate order summary', 'sections': [{'section_id': 'facts', 'section_name': 'Facts', 'section_content': facts}]}}),
    ]:
        db.session.add(AgentArtifact(id=uid, run_id=row.id, step_id=step.id,
            kind=kind, content_json=service.encode(body), created_at=service.now()))
    db.session.commit()
    monkeypatch.setitem(token_budget.CONTEXT_LIMITS, 'gpt-6-luna', 141000)
    summary = f'{facts} Source {source_id}, order-file characters 0:70. Candidate {candidate_id}. Acceptance remains unresolved.'
    prompts = summary_provider(monkeypatch, summary)
    requests = []
    from tests.test_agent_controller import call
    def decide(request):
        requests.append(request)
        text = json.dumps(request['input'], ensure_ascii=False)
        assert all(value in text for value in ('$240', 'June 10', source_id, candidate_id, 'Acceptance remains unresolved'))
        return call('complete_task', {'response': facts + ' Acceptance remains unresolved.', 'candidate_id': None})
    monkeypatch.setattr(base, 'responses_completion', decide)
    assert tick(row) == {'continued': True}
    assert tick(row) == {'completed': True}
    assert prompts and checkpoint(row)
    assert row.status == 'completed' and len(state(row)['chat_history']) == 1
    assert state(row)['draft']['title'] == 'Original'


def test_authoritative_working_head_survives_compaction_and_reload(context_row, monkeypatch):
    row = context_row
    payload = json.loads(row.payload_json)
    payload['agent_context']['working_candidate_id'] = 'latest-exact-candidate'
    row.payload_json = service.encode(payload)
    db.session.commit()
    for _ in range(8):
        add_group(row, [{'text': 'Earlier candidate old-base. ' * 1400}])
    summary_provider(monkeypatch, 'Older draft was old-base; continue the task.')
    token = claim(row)
    result = prepare(row, token=token)
    assert checkpoint(row)
    message = next(item['content'] for item in result if item.get('content', '').startswith('Application working-draft state ('))
    assert 'latest-exact-candidate' in message
    db.session.expire_all()
    result = prepare(row, token=token)
    assert any('latest-exact-candidate' in item.get('content', '') for item in result)


def test_compaction_retains_material_purposes_and_outstanding_user_checks(context_row, monkeypatch):
    row=context_row
    use='source-material is a style example only; verify the new figures before finishing.'
    for _ in range(7):
        add_group(row,[{'artifact_id':'source-material','text':'Long reference passage. '*1300}],
            names=['analyze_material'], arguments=[{'instructions':'Review supplied material.',
                'material_ids':['source-material'],'material_uses':[{'artifact_id':'source-material','purpose':use}]}])
    prompts=summary_provider(monkeypatch,use)
    token=claim(row)
    result=prepare(row,token=token)
    assert checkpoint(row)
    assert 'style example only' in json.dumps(result)
    assert any('source-material' in prompt and 'style example only' in prompt for prompt,_ in prompts)
    assert 'outstanding checks' in agent_context.SUMMARY_SYSTEM
    db.session.expire_all()
    restored=prepare(row,token=token)
    assert 'verify the new figures' in json.dumps(restored)
