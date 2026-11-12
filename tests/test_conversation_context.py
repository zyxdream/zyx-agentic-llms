"""History meaning, versioned source reuse and existing SQL checkpoint connections."""
import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend import everything
from backend.database import db
from backend.models import AgentStep, TextEntry, CaseAssistantWorkflow, DocumentSummarySegment
from backend.util import conversation_context as memory
from backend.util import source_context as sources
from backend.util import case_assistant_workflow as service
from backend.util import prepared_retrieval as retrieval
from backend.util import workspace_store as workspace
from backend.util.db import DBMemory
from backend.util.preparation_summary import token_count
from tests.test_source_context import modern, workflow, app, install, focused


def prior_request(row, payload, ranges, *, query=None, response='The notice exception is disputed.'):
    values = {column.name: getattr(row, column.name) for column in row.__table__.columns
              if column.name not in ('id', 'active_chat_thread_id')}
    values.update(status='completed', created_at=row.created_at - timedelta(minutes=1),
                  result_json=service.encode({'response': response}),
                  payload_json=service.encode({**payload, 'source_context': {'included_ranges': ranges}}),
                  lease_token=None, lease_until=None)
    prior = CaseAssistantWorkflow(id='wf_previous', active_draft_id=None, **values)
    data = json.loads(prior.data_json)
    if query is not None:
        data['query'] = query
        prior.data_json = service.encode(data)
    db.session.add(prior)
    db.session.flush()
    # Completed workflows are published through the canonical turn store;
    # adding another legacy JSON snapshot after initialization cannot publish.
    workspace.append_turn(row.draft_id, data.get('display_user_message') or data.get('query', ''),
                          response, workflow_id=prior.id, meta={'workflow_completed': True})
    db.session.commit()
    return prior


def test_middle_of_old_answer_is_retrieved_without_a_model_call(monkeypatch):
    monkeypatch.setattr(everything, 'expand_search_keywords', lambda *a, **k: pytest.fail('No LLM needed'))
    answer = 'Unrelated introduction.\n\n' * 1500 + 'RFP 7: obtain Zephyr source-control backups.\n\n' + 'Unrelated conclusion.\n\n' * 1500
    history = [('Discuss discovery', answer), ('Continue', 'Yes'), ('Next', 'Ready')]
    result = everything.retrieve_relevant_chat_turns('Narrow RFP 7 about Zephyr backups', history)
    assert 'RFP 7: obtain Zephyr source-control backups.' in result
    assert token_count(result) <= 4000


def test_build_small_context_has_recent_older_and_constraints_without_compaction(monkeypatch):
    monkeypatch.setattr(everything, 'chat_completion_parse', lambda *a, **k: pytest.fail('No compaction needed'))
    state = {'chat_history': [('old', 'license exception'), ('draft', 'RFP 7 text'), ('revise', 'narrowed')],
             'conversation_brief': 'We represent Uber.', 'constraints': {'jurisdiction': 'CA'}}
    result = memory.build(state, 'Narrow RFP 7', model='gpt-6-luna')
    assert all(text in result['text'] for text in ('license exception', 'RFP 7 text', 'Uber', 'CA'))
    assert token_count(result['text']) <= 16000


def test_large_exchange_compaction_processes_middle_and_end(monkeypatch):
    prompts = []
    def summarize(prompt, schema, **kwargs):
        prompts.append(prompt)
        return SimpleNamespace(brief='Preserve MIDPOINT and ENDPOINT; RFP 7 concerns backups.')
    monkeypatch.setattr(everything, 'chat_completion_parse', summarize)
    text = 'opening ' * 28000 + 'MIDPOINT ' + 'continuation ' * 28000 + 'ENDPOINT'
    result = memory.compact(text, 100, model='gpt-6-luna')
    assert 'ENDPOINT' in result
    assert len(prompts) >= 3
    assert any('MIDPOINT' in p for p in prompts) and 'ENDPOINT' in prompts[-1]
    assert all(token_count(p) < 40000 for p in prompts)


def test_compaction_rejects_incomplete_or_oversized_output(monkeypatch):
    monkeypatch.setattr(everything, 'chat_completion_parse', lambda *a, **k: SimpleNamespace(brief='too long ' * 100))
    with pytest.raises(ValueError, match='budget'):
        memory.compact('original ' * 100, 10, model='gpt-6-luna')


def test_legacy_turn_survives_workspace_window_and_is_scoped(app, monkeypatch):
    state = {'chat_history': [], 'chat_history_meta': []}
    monkeypatch.setattr(everything, 'MAX_CHAT_HISTORY_STORED', 2)
    for index in range(4):
        everything.add_chat_message(state, (f'question {index}', f'answer {index}'))
    everything.save_state_to_memory(state, DBMemory(db=db, doc_id=1))
    assert len(state['chat_history']) == 2
    assert [r['turn'][0] for r in memory.available_turns(state, 1)] == [f'question {i}' for i in range(4)]
    assert memory.available_turns({}, 2) == []
    assert len(workspace.turns(1)) == 4
    assert TextEntry.query.filter(TextEntry.textName.like('conversation-turn:%')).count() == 0
    everything.save_state_to_memory(state, DBMemory(db=db, doc_id=1))
    assert len(workspace.turns(1)) == 4


def test_completed_workflow_preserves_raw_exchange_without_archive_duplicate(modern):
    row = modern
    payload = json.loads(row.payload_json)
    prior = prior_request(row, payload, {})
    state = {'_history_to_archive': [(('old query', 'old answer'), {'workflow_run_id': prior.id})]}
    memory.archive_removed(state, SimpleNamespace(doc_id=row.draft_id))
    db.session.commit()
    assert TextEntry.query.filter(TextEntry.textName.like('conversation-turn:%')).count() == 0
    records = memory.available_turns({}, row.draft_id)
    assert records[0]['meta']['workflow_run_id'] == prior.id
    assert records[0]['turn'][1] == 'The notice exception is disputed.'


@pytest.mark.parametrize('in_workspace_history', [False, True])
def test_next_assignment_receives_agent_questions_and_answers(modern, monkeypatch, in_workspace_history):
    row = modern
    prior = prior_request(row, json.loads(row.payload_json), {},
                          query='Summarize the agreement.', response='Here is the summary.')
    prior.execution_version = 1
    questions = [('Which exhibit should I use?', 'Use Exhibit B.'),
                 ('Should I include the appendix?', 'No.')]
    # Insert in reverse order so the assertion checks saved step order, rather
    # than insertion order. Real agent calls store their arguments in raw JSON.
    for sequence in (2, 1):
        question, answer = questions[sequence - 1]
        arguments = {'question': question, 'options': []}
        if sequence == 1:
            arguments = {'raw': service.encode(arguments)}
        db.session.add(AgentStep(workflow_run_id=prior.id, sequence=sequence,
            kind='tool', name='request_user_input', state='ready',
            arguments_json=service.encode(arguments), result_json=service.encode({'answer': answer}),
            created_at=service.now(), updated_at=service.now()))
    state = {'chat_history': [], 'chat_history_meta': []}
    if in_workspace_history:
        state = {'chat_history': [['Summarize the agreement.', 'Here is the summary.']],
                 'chat_history_meta': [{'workflow_run_id': prior.id}]}
    original_state = copy.deepcopy(state)
    saved_prior = (prior.data_json, prior.result_json)
    row.lease_token, row.lease_until = 'next-request', service.now() + timedelta(minutes=5)
    db.session.commit()
    saved_steps = [(s.arguments_json, s.result_json) for s in AgentStep.query.order_by(AgentStep.sequence).all()]
    monkeypatch.setattr(everything, 'chat_completion_parse', lambda *a, **k: pytest.fail('No summary needed'))

    with service.execution(row.id, 'next-request'):
        context = memory.for_request(state, {}, 'Now draft a letter based on that.',
                                     doc_id=row.draft_id, model='gpt-6-luna')

    text = context['text']
    for question, answer in questions:
        assert question in text and answer in text
        assert text.index(question) < text.index(answer)
        assert text.count(question) == text.count(answer) == 1
    assert text.index(questions[0][0]) < text.index(questions[1][0])
    assert json.loads(row.data_json)['_conversation_context'] == json.loads(service.encode(context))
    assert (prior.data_json, prior.result_json) == saved_prior
    assert [(s.arguments_json, s.result_json) for s in AgentStep.query.order_by(AgentStep.sequence).all()] == saved_steps
    assert state == original_state


def test_agent_clarification_memory_excludes_failed_pending_and_other_chat_steps(modern):
    row = modern
    prior = prior_request(row, json.loads(row.payload_json), {})
    values = {column.name: getattr(prior, column.name) for column in prior.__table__.columns
              if column.name not in ('id', 'active_chat_thread_id')}
    values['draft_id'] = 2
    other = CaseAssistantWorkflow(id='wf_other_chat', active_draft_id=None, **values)
    db.session.add(other)
    db.session.flush()
    workspace.append_turn(2, 'Other chat question', 'Other chat answer', workflow_id=other.id)
    cases = [
        (prior.id, 'tool', 'request_user_input', 'ready', {'answer': 'Use Exhibit B.'}),
        (prior.id, 'tool', 'request_user_input', 'ready', {'error': 'Invalid call', 'answer': 'Failed answer'}),
        (prior.id, 'tool', 'request_user_input', 'ready', {'error': 'No answer'}),
        (prior.id, 'tool', 'request_user_input', 'waiting', {'answer': 'Pending answer'}),
        (prior.id, 'tool', 'read_sources', 'ready', {'answer': 'Not a clarification'}),
        (prior.id, 'model', 'request_user_input', 'ready', {'answer': 'Not a user answer'}),
        (row.id, 'tool', 'request_user_input', 'ready', {'answer': 'Incomplete assignment answer'}),
        (other.id, 'tool', 'request_user_input', 'ready', {'answer': 'Other chat answer'}),
    ]
    for sequence, (owner, kind, name, state, result) in enumerate(cases, 1):
        db.session.add(AgentStep(workflow_run_id=owner, sequence=sequence,
            kind=kind, name=name, state=state,
            arguments_json=service.encode({'raw': service.encode({'question': 'Which exhibit?'})}),
            result_json=service.encode(result), created_at=service.now(), updated_at=service.now()))
    db.session.commit()

    records = memory.available_turns({}, row.draft_id)

    assert len(records) == 1
    assert records[0]['meta']['agent_clarifications'] == [{'question': 'Which exhibit?', 'answer': 'Use Exhibit B.'}]
    text, _ = memory.conversation_turn(records[0])
    assert 'Use Exhibit B.' in text
    assert all(result['answer'] not in text for *_, result in cases[1:] if 'answer' in result)


def test_new_admission_gets_policy_and_request_context_is_checkpointed(modern, monkeypatch):
    row = modern
    assert json.loads(row.data_json)['_conversation_context_version'] == 1
    token = 'context-owner'
    row.lease_token, row.lease_until = token, service.now() + timedelta(minutes=5)
    db.session.commit()
    calls = []
    monkeypatch.setattr(memory, 'build', lambda *a, **kw: calls.append(True) or {'text': 'saved recent context'})
    with service.execution(row.id, token):
        a = memory.for_request({}, {}, 'first question', doc_id=row.draft_id, model='gpt-6-luna')
        b = memory.for_request({}, {}, 'clarified question', doc_id=row.draft_id, model='gpt-6-luna')
    assert a == b == {'text': 'saved recent context'}
    assert len(calls) == 1


def test_old_admission_does_not_get_new_context(modern):
    row = modern
    data = json.loads(row.data_json)
    data.pop('_conversation_context_version')
    row.data_json = service.encode(data)
    row.lease_token, row.lease_until = 'owner', service.now() + timedelta(minutes=5)
    db.session.commit()
    with service.execution(row.id, 'owner'):
        assert memory.for_request({}, {}, 'q', doc_id=row.draft_id, model='gpt-6-luna') is None


def test_compaction_yields_and_reuses_completed_parts(modern, monkeypatch):
    row = modern
    row.lease_token, row.lease_until = 'owner', service.now() + timedelta(minutes=5)
    db.session.commit()
    prompts = []
    monkeypatch.setattr(everything, 'chat_completion_parse', lambda prompt, *a, **k: (
        prompts.append(prompt) or SimpleNamespace(brief='compact complete exchange')))
    text = 'whole exchange ' * 30000 + 'LAST'
    with service.execution(row.id, 'owner', max_calls=1):
        with pytest.raises(service.WorkPending):
            memory.compact(text, 100, model='gpt-6-luna')
    assert len(prompts) == 1
    result = None
    for _ in range(10):
        try:
            with service.execution(row.id, 'owner', max_calls=1):
                result = memory.compact(text, 100, model='gpt-6-luna')
            break
        except service.WorkPending:
            continue
    assert result == 'compact complete exchange'
    assert len(prompts) >= 2
    assert len(prompts) == len(set(prompts))  # No completed chunk is repeated.
    calls = len(prompts)
    with service.execution(row.id, 'owner', max_calls=0):
        assert memory.compact(text, 100, model='gpt-6-luna') == result
    assert len(prompts) == calls


def test_simple_followup_reuses_original_ranges_without_retrieval(modern, monkeypatch):
    row = modern
    uid, manifest = install(row, ['The license exception requires written notice.'])
    payload = focused(row, 'Make those requests narrower')
    prior = prior_request(row, payload, {uid: [{'start': 0, 'end': 20}]})
    payload['conversation_context'] = {'previous_workflow_id': prior.id}
    payload['intent_decision'].update(context_policy='reuse', retrieval_queries=[])
    monkeypatch.setattr(sources, 'selected_spans', lambda *a, **k: pytest.fail('No new retrieval'))
    sources.plan(row, payload)
    assert sources.source_groups(row)[uid] == [{'start': 0, 'end': 20}]
    assert payload['source_context']['reused_from'][uid]['workflow_id'] == prior.id
    monkeypatch.setattr(retrieval, 'review_documents', lambda *a, **k: pytest.fail('No relevance LLM'))
    decision = retrieval.refine_selection(payload['document_snapshot'], payload['intent_decision'], 'Narrow those',
                                         model='gpt-6-luna', preserve_reuse=True)
    assert decision['context_policy'] == 'reuse'


def test_reuse_with_new_query_merges_ranges_without_duplication(modern, monkeypatch):
    row = modern
    uid, manifest = install(row, ['The license exception requires written notice.'])
    payload = focused(row)
    prior = prior_request(row, payload, {uid: [{'start': 0, 'end': 20}]})
    payload['conversation_context'] = {'previous_workflow_id': prior.id}
    payload['intent_decision'].update(context_policy='reuse', retrieval_queries=['license exception'])
    calls = []
    monkeypatch.setattr(sources, 'selected_spans', lambda *a, **k: calls.append(a[2]) or [{'start': 10, 'end': 30}])
    sources.plan(row, payload)
    assert calls == [['license exception']]
    assert sources.source_groups(row)[uid] == [{'start': 0, 'end': 30}]


def test_new_topic_refresh_does_not_reuse_previous_ranges(modern, monkeypatch):
    row = modern
    uid, _ = install(row, ['The license exception requires written notice.'])
    payload = focused(row)
    prior = prior_request(row, payload, {uid: [{'start': 0, 'end': 10}]})
    payload['conversation_context'] = {'previous_workflow_id': prior.id}
    payload['intent_decision'].update(context_policy='refresh', retrieval_queries=['license defense'])
    monkeypatch.setattr(sources, 'selected_spans', lambda *a, **k: [{'start': 20, 'end': 30}])
    sources.plan(row, payload)
    assert sources.source_groups(row)[uid] == [{'start': 20, 'end': 30}]


@pytest.mark.parametrize('mismatch', ['user', 'chat', 'artifact', 'excluded'])
def test_reuse_rejects_inaccessible_or_changed_sources(modern, mismatch):
    row = modern
    uid, _ = install(row, ['The license exception requires written notice.'])
    payload = focused(row)
    prior = prior_request(row, payload, {uid: [{'start': 0, 'end': 10}]})
    snapshot = copy.deepcopy(payload['document_snapshot'])
    if mismatch == 'artifact':
        old = json.loads(prior.payload_json)
        old['document_snapshot']['files'][0]['artifact_id'] = 'outdated'
        prior.payload_json = service.encode(old)
        db.session.commit()
    if mismatch == 'excluded':
        snapshot['files'] = []
    result = sources.reusable_context(snapshot, prior.id, doc_id=2 if mismatch == 'chat' else row.draft_id,
                                      user_id=2 if mismatch == 'user' else row.user_id)
    assert result == {}


def test_generated_findings_keep_original_record_and_reuse_original_blocks(modern):
    row = modern
    uid, manifest = install(row, ['License defense requires written notice.'])
    payload = focused(row)
    prior = prior_request(row, payload, {})
    value = {'format': sources.FINDINGS, 'findings': [{'statement': 'Notice is required.',
             'block_ids': [manifest['blocks'][0]['id']]}], 'complete': True}
    segment = DocumentSummarySegment(document_id=uid, segment_key='question-specific-original-key', kind='query',
                                     state='ready', result_json=service.encode(value))
    db.session.add(segment)
    db.session.flush()
    artifact = payload['document_snapshot']['files'][0]['artifact_id']
    db.session.add(service.Work(workflow_run_id=prior.id, work_key='prior-finding', kind='query', state='ready',
                   document_id=uid, artifact_id=artifact, material_json='{}', result_id=segment.id,
                   updated_at=service.now()))
    db.session.commit()
    result = sources.reusable_context(payload['document_snapshot'], prior.id, doc_id=row.draft_id, user_id=row.user_id)
    assert result[uid]['result_ids'] == [segment.id]
    assert segment.segment_key == 'question-specific-original-key'
    assert result[uid]['ranges'] == [{'start': 0, 'end': len(manifest['text'])}]


def test_out_of_order_brief_update_cannot_overwrite_newer_brief(monkeypatch):
    monkeypatch.setattr('backend.util.account_access.chat_account_id', lambda _id: 1)
    monkeypatch.setattr('backend.util.account_access.require_enabled_account', lambda _id: None)
    state = {'conversation_brief': 'old', 'constraints': {}, 'chat_history': [('q', 'a')], 'chat_history_meta': [{}]}
    latest = {**state, 'conversation_brief': 'newer', 'conversation_brief_through': 'later-turn'}
    loads = iter([state, latest])
    monkeypatch.setattr(everything, 'DBMemory', lambda **kw: {})
    monkeypatch.setattr(everything, 'load_state_from_memory', lambda m: next(loads))
    monkeypatch.setattr(everything, 'update_conversation_brief', lambda *a, **kw: ('stale result', {}))
    monkeypatch.setattr(everything, 'save_state_to_memory', lambda *a: pytest.fail('Must not overwrite'))
    everything._run_brief_update({'doc_id': 1, 'user_message': 'q', 'assistant_response': 'a'})
    assert latest['conversation_brief'] == 'newer'


def test_brief_update_covers_intervening_exchanges(monkeypatch):
    monkeypatch.setattr('backend.util.account_access.chat_account_id', lambda _id: 1)
    monkeypatch.setattr('backend.util.account_access.require_enabled_account', lambda _id: None)
    state = {'conversation_brief': 'old', 'constraints': {}, 'chat_history': [('q1', 'a1'), ('q2', 'a2')],
             'chat_history_meta': [{'conversation_turn_id': 't1'}, {'conversation_turn_id': 't2'}]}
    monkeypatch.setattr(everything, 'DBMemory', lambda **kw: {})
    monkeypatch.setattr(everything, 'load_state_from_memory', lambda m: copy.deepcopy(state))
    calls, saved = [], []
    monkeypatch.setattr(everything, 'update_conversation_brief', lambda b, c, q, a, **kw: (
        calls.append((q, a)) or (b + q, c)))
    monkeypatch.setattr(everything, 'save_state_to_memory', lambda s, m: saved.append(s))
    everything._run_brief_update({'doc_id': 1, 'user_message': 'q2', 'assistant_response': 'a2'})
    assert len(calls) == 1
    assert all(value in calls[0][1] for value in ('q1', 'a1', 'q2', 'a2'))
    assert saved[0]['conversation_brief_through'] == 't2'


def test_brief_update_receives_complete_answer_beyond_old_slice(monkeypatch):
    captured = []
    def summarize(prompt, schema, **kwargs):
        captured.append(prompt)
        return schema(brief='Retain the final exception.', constraints={
            'tone': None, 'exclusions': [], 'special_instructions': None, 'cases': []})
    monkeypatch.setattr(everything, 'chat_completion_parse', summarize)
    everything.update_conversation_brief('', {}, 'Explain', 'background ' * 1000 + 'FINAL IMPORTANT EXCEPTION')
    assert 'FINAL IMPORTANT EXCEPTION' in captured[0]
    assert len(captured) == 1


@pytest.mark.parametrize('with_auxiliary_row', [False, True])
def test_sql_brief_update_preserves_new_draft_and_records_coverage(app, monkeypatch, with_auxiliary_row):
    version = workspace.load(1)['draft_version']
    workspace.update_draft(1, {'title': 'before', 'sections': []}, expected_version=version)
    for index in (1, 2):
        workspace.append_turn(1, f'q{index}', f'a{index}', turn_id=f't{index}')
    if with_auxiliary_row:
        workspace.patch_auxiliary(1, {'conversation_brief': '', 'constraints': {}})
    db.session.commit()
    def update(*args, **kwargs):
        concurrent = workspace.load(1)
        workspace.update_draft(1, {'title': 'User changed draft', 'sections': []},
                               expected_version=concurrent['draft_version'])
        db.session.commit()
        return 'Both exchanges covered.', {}
    monkeypatch.setattr(everything, 'update_conversation_brief', update)
    everything._run_brief_update({'doc_id': 1, 'user_message': 'q2', 'assistant_response': 'a2'})
    db.session.expire_all()
    result = workspace.load(1)
    assert result['draft']['title'] == 'User changed draft'
    assert result['draft_version'] == version + 2
    assert result['chat_history'] == [['q1', 'a1'], ['q2', 'a2']]
    assert result['conversation_brief_through'] == 't2'
    assert 'draft' not in json.loads(TextEntry.query.filter_by(draftId=1, textName='state').one().content)


def test_hybrid_refinement_keeps_previous_support_and_edit_target(modern, monkeypatch):
    row = modern
    payload = focused(row)
    uid = payload['intent_decision']['selected_files'][0]['file_id']
    decision = copy.deepcopy(payload['intent_decision'])
    decision.update(context_policy='reuse', retrieval_queries=['amendments'], draft_action='edit_current_draft')
    decision['selected_files'][0]['role'] = 'primary_draft'
    monkeypatch.setattr(retrieval, 'review_documents', lambda *a, **kw: retrieval.DocumentReview(
        documents=[retrieval.SelectedDocument(file_id=uid, relevant=False, reason='No amendment found.')]))
    result = retrieval.refine_selection(payload['document_snapshot'], decision, 'Check amendments too',
                model='gpt-6-luna', preserve_reuse=True, conversation_context='The prior draft concerns notice.')
    assert result['selected_files'][0]['file_id'] == uid
    assert result['selected_files'][0]['role'] == 'primary_draft'
    assert result['draft_action'] == 'edit_current_draft'
    assert result['context_policy'] == 'reuse'


def test_brief_update_identifies_confirmed_turn_by_workflow_id(modern, monkeypatch):
    row = modern
    row.status = 'completed'
    row.result_json = service.encode({'response': 'answer'})
    workspace.append_turn(row.draft_id, 'Show client clarification', 'answer', workflow_id=row.id)
    workspace.patch_auxiliary(row.draft_id, {'conversation_brief': '', 'constraints': {}})
    db.session.commit()
    calls = []
    monkeypatch.setattr(everything, 'update_conversation_brief', lambda b, c, q, a, **kw: (
        calls.append((q, a)) or ('Confirmed exchange summarized.', {})))
    everything._run_brief_update({'doc_id': row.draft_id, 'user_message': 'internal execution instruction',
                                 'assistant_response': 'answer', 'workflow_run_id': row.id})
    db.session.expire_all()
    result = workspace.load(row.draft_id)
    assert calls == [('Show client clarification', 'answer')]
    assert result['conversation_brief_through'] == row.id


def test_acknowledgment_does_not_sever_document_context(modern, monkeypatch):
    row = modern
    payload = focused(row)
    uid = payload['intent_decision']['selected_files'][0]['file_id']
    prior = prior_request(row, payload, {uid: [{'start': 0, 'end': 10}]})
    values = {column.name: getattr(prior, column.name) for column in prior.__table__.columns
              if column.name not in ('id', 'active_chat_thread_id')}
    values.update(created_at=prior.created_at + timedelta(seconds=1),
                  payload_json=service.encode({'intent_decision': {'selected_files': []}}),
                  result_json=service.encode({'response': 'You are welcome.'}))
    acknowledgment = CaseAssistantWorkflow(id='wf_acknowledgment', active_draft_id=None, **values)
    db.session.add(acknowledgment)
    db.session.flush()
    workspace.append_turn(row.draft_id, 'Thank you', 'You are welcome.', workflow_id=acknowledgment.id)
    db.session.commit()
    monkeypatch.setattr(everything, 'chat_completion_parse', lambda *a, **kw: pytest.fail('No summary needed'))
    result = memory.build({}, 'Narrow those requests', doc_id=row.draft_id, model='gpt-6-luna')
    assert result['previous_workflow_id'] == prior.id
    assert uid in result['text']


def test_non_document_followup_skips_relevance_calls(modern, monkeypatch):
    payload = json.loads(modern.payload_json)
    decision = {'selected_files': [], 'context_policy': 'none', 'coverage_mode': 'focused'}
    monkeypatch.setattr(retrieval, 'review_documents', lambda *a, **kw: pytest.fail('No document review needed'))
    assert retrieval.refine_selection(payload['document_snapshot'], decision, 'Thank you',
            model='gpt-6-luna', preserve_reuse=True) == decision


def test_legacy_archive_date_does_not_move_old_exchange_after_sql_turns(modern):
    row = modern
    payload = json.loads(row.payload_json)
    # Represent an already imported archive. Its position, rather than a later
    # pruning timestamp or a retained rollback row, determines conversation order.
    legacy = workspace.append_turn(row.draft_id, 'Original instruction', 'Original answer', turn_id='legacy')
    legacy.created_at = row.created_at + timedelta(days=1)
    prior_request(row, payload, {})
    db.session.add(TextEntry(draftId=row.draft_id, textName='conversation-turn:legacy',
        content=service.encode({'turn': ['Original instruction', 'Original answer'],
                               'meta': {'conversation_turn_id': 'legacy'}})))
    db.session.commit()
    records = memory.available_turns({}, row.draft_id)
    assert records[0]['turn'][0] == 'Original instruction'
    assert records[1]['meta']['workflow_run_id'] == 'wf_previous'


@pytest.mark.parametrize('agent_clarifications', [[], [{'question': 'Which exhibit?', 'answer': 'Use Exhibit B.'}]])
def test_confirmation_clarification_reaches_planning_context(monkeypatch, agent_clarifications):
    monkeypatch.setattr(everything, 'chat_completion_parse', lambda *a, **k: pytest.fail('No summary needed'))
    state = {'chat_history': [['Revise that', 'Updated.']], 'chat_history_meta': [
        {'clarification': {'answer': 'Only narrow RFP 7; keep the other requests.'},
         'agent_clarifications': agent_clarifications}]}
    result = memory.build(state, 'Make it more specific', model='gpt-6-luna')
    assert 'Only narrow RFP 7; keep the other requests.' in result['text']
    for clarification in agent_clarifications:
        assert clarification['question'] in result['text'] and clarification['answer'] in result['text']
