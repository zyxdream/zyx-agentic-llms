"""Candidate publication permission against real SQL; provider calls are doubles."""
import copy
import json
from datetime import timedelta

import pytest

from backend import agent_tasks as tasks
from backend.workspace import state as everything
from backend.ai import base
from backend.ai.token_budget import IncompleteStructuredOutput
from backend.database import db
from backend.models import AgentArtifact
from backend.util import workspace_service as service
from backend.util import document_drafts
from tests.test_agent_controller import agent, app, last_result, operation, state


def assignment(row, text, **options):
    data = json.loads(row.data_json)
    data.update(query=text, **options)
    row.data_json = service.encode(data)
    db.session.commit()


def candidate(row, monkeypatch, title='Private candidate'):
    result = document_drafts.MarkdownDraftResult(title=title, markdown='PRIVATE_CANDIDATE_WORDS', update_court_format=False)
    monkeypatch.setattr(document_drafts, 'write_document', lambda **kw: result)
    head = json.loads(row.payload_json or '{}').get('agent_context', {}).get('working_candidate_id')
    args = {'instructions': 'GENERATED_DRAFT_INSTRUCTIONS', 'material_ids': []}
    if head:
        args.update(target='working_draft', revision_mode='general_revision')
    assert operation(row, monkeypatch, 'revise_draft' if head else 'create_draft', args) == {'continued': True}
    return last_result(row)['artifact_id']


def cache(row):
    return copy.deepcopy(json.loads(row.payload_json)['agent_context'].get('editor_write_check'))


def permission_provider(monkeypatch, *decisions, callback=None):
    calls = []
    outcomes = iter(decisions)

    def classify(prompt, response_model, **kwargs):
        calls.append((prompt, kwargs))
        if callback:
            callback()
        return response_model(decision=next(outcomes), reason='Saved permission interpretation.',
                              supporting_wording='User wording retained for inspection.')

    monkeypatch.setattr(base, 'chat_completion_parse', classify)
    return calls


@pytest.mark.parametrize('decision', ['forbidden', 'unclear'])
def test_blocked_candidate_never_loads_or_changes_editor_and_answer_only_still_finishes(agent, monkeypatch, decision):
    original = 'Analysis only. Do not change the editor.'
    assignment(agent, original, draft_mode=False)
    calls = permission_provider(monkeypatch, decision)
    uid = candidate(agent, monkeypatch)
    assert calls == []  # Private candidate creation has no permission check.
    before = state(agent)
    saved_candidate = db.session.get(AgentArtifact, uid).content_json
    load = everything.load_state_from_memory
    finalize = everything.finalize_llm_response_for_display
    monkeypatch.setattr(everything, 'load_state_from_memory', lambda *a, **kw: pytest.fail('Guard must run before editor load'))
    monkeypatch.setattr(everything, 'finalize_llm_response_for_display', lambda *a, **kw: pytest.fail('Guard must run before finalization'))

    assert operation(agent, monkeypatch, 'complete_task', {'response': 'GENERATED_COMPLETION_TEXT',
        'candidate_id': uid}) == {'continued': True}

    assert last_result(agent)['error'] is True
    assert agent.status == 'running'
    assert state(agent) == before
    assert db.session.get(AgentArtifact, uid).content_json == saved_candidate
    assert len(calls) == 1
    assert calls[0][1]['model'] == 'gpt-6-luna'
    assert original in calls[0][0]
    assert all(text not in calls[0][0] for text in (
        'PRIVATE_CANDIDATE_WORDS', 'GENERATED_DRAFT_INSTRUCTIONS', 'GENERATED_COMPLETION_TEXT'))
    saved_check = cache(agent)
    assert saved_check['decision'] == decision
    assert saved_check['reason'] == 'Saved permission interpretation.'
    assert saved_check['supporting_wording'] == 'User wording retained for inspection.'

    monkeypatch.setattr(everything, 'load_state_from_memory', load)
    monkeypatch.setattr(everything, 'finalize_llm_response_for_display', finalize)
    assert operation(agent, monkeypatch, 'complete_task', {'response': 'Here is the analysis without editor changes.',
        'candidate_id': None}) == {'completed': True}
    assert len(calls) == 1
    assert state(agent)['draft'] == before['draft']
    assert len(state(agent)['chat_history']) == 1


def test_answer_only_makes_no_permission_call(agent, monkeypatch):
    assignment(agent, 'Explain this agreement; do not edit the document.')
    before = state(agent)
    monkeypatch.setattr(base, 'chat_completion_parse', lambda *a, **kw: pytest.fail('Answer-only needs no write check'))

    assert operation(agent, monkeypatch, 'complete_task', {'response': 'The agreement requires notice.',
        'candidate_id': None}) == {'completed': True}

    assert state(agent)['draft'] == before['draft']
    assert cache(agent) is None


def test_ordinary_drafting_from_ask_mode_publishes_and_preserves_fresh_payload_fields(agent, monkeypatch):
    assignment(agent, 'Prepare a short draft in the editor.', draft_mode=False)
    uid = candidate(agent, monkeypatch)
    original_draft = copy.deepcopy(state(agent)['draft'])

    def concurrent_payload_update():
        # The provider starts after the guard releases its SQL transaction.
        assert not db.session().in_transaction()
        payload = json.loads(agent.payload_json)
        payload['agent_context']['separately_saved_value'] = 'keep me'
        payload['other_payload_value'] = {'saved': True}
        agent.payload_json = service.encode(payload)
        db.session.commit()

    calls = permission_provider(monkeypatch, 'not_forbidden', callback=concurrent_payload_update)
    assert operation(agent, monkeypatch, 'complete_task', {'response': 'Draft prepared.', 'candidate_id': uid}) == {'completed': True}

    assert len(calls) == 1
    assert state(agent)['draft']['title'] == 'Private candidate'
    assert state(agent)['draft_before'] == original_draft
    payload = json.loads(agent.payload_json)
    assert payload['agent_context']['separately_saved_value'] == 'keep me'
    assert payload['other_payload_value'] == {'saved': True}
    assert cache(agent)['decision'] == 'not_forbidden'


def test_same_user_context_reuses_cached_check_across_private_candidate_revisions(agent, monkeypatch):
    assignment(agent, 'Analysis only; leave the editor unchanged.')
    calls = permission_provider(monkeypatch, 'forbidden')
    first = candidate(agent, monkeypatch, 'Candidate A')
    before = state(agent)
    assert operation(agent, monkeypatch, 'complete_task', {'response': 'Attempt A', 'candidate_id': first}) == {'continued': True}
    first_check = cache(agent)
    second = candidate(agent, monkeypatch, 'Candidate B')
    assert operation(agent, monkeypatch, 'complete_task', {'response': 'Attempt B', 'candidate_id': second}) == {'continued': True}

    assert len(calls) == 1
    assert cache(agent) == first_check
    assert state(agent) == before
    assert first != second
    assert last_result(agent)['error'] is True


@pytest.mark.parametrize('grant_permission', [False, True])
def test_user_clarification_invalidates_check_and_can_grant_or_withdraw_permission(agent, monkeypatch, grant_permission):
    original = 'Analysis only. Do not edit.' if grant_permission else 'Prepare a draft in the editor.'
    answer = 'Now apply the candidate to the editor.' if grant_permission else 'Analysis only now; do not change the editor.'
    assignment(agent, original)
    uid = candidate(agent, monkeypatch)
    calls = permission_provider(monkeypatch, *(
        ('forbidden', 'not_forbidden') if grant_permission else ('not_forbidden', 'forbidden')))
    before = state(agent)
    finalize = everything.finalize_llm_response_for_display
    # Defer the initially permitted publication so the same active assignment
    # can receive a later instruction withdrawing permission.
    monkeypatch.setattr(everything, 'finalize_llm_response_for_display', lambda *a, **kw: (_ for _ in ()).throw(ValueError('Publication deferred for clarification')))
    assert operation(agent, monkeypatch, 'complete_task', {'response': 'First attempt', 'candidate_id': uid}) == {'continued': True}
    first_hash = cache(agent)['context_hash']
    assert state(agent) == before
    operation(agent, monkeypatch, 'request_user_input', {'question': 'Should editor changes be allowed?', 'options': []})
    tasks.answer_question(agent, {'answer': answer})
    monkeypatch.setattr(everything, 'finalize_llm_response_for_display', finalize)

    outcome = operation(agent, monkeypatch, 'complete_task', {'response': 'Final outcome.', 'candidate_id': uid})

    assert len(calls) == 2
    assert answer in calls[1][0]
    assert 'Should editor changes be allowed?' in calls[1][0]
    assert cache(agent)['context_hash'] != first_hash
    if grant_permission:
        assert outcome == {'completed': True}
        assert state(agent)['draft']['title'] == 'Private candidate'
    else:
        assert outcome == {'continued': True} and last_result(agent)['error'] is True
        assert state(agent) == before


@pytest.mark.parametrize('failure', ['none', 'incomplete', 'provider_error', 'invalid'])
def test_unusable_permission_result_fails_without_replacing_previous_cache(agent, monkeypatch, failure):
    uid = candidate(agent, monkeypatch)
    before = state(agent)
    previous = {'context_hash': 'older-user-context', 'decision': 'not_forbidden',
                'reason': 'Previous valid result.', 'supporting_wording': 'Previous wording.'}
    payload = json.loads(agent.payload_json)
    payload['agent_context']['editor_write_check'] = previous
    agent.payload_json = service.encode(payload)
    db.session.commit()

    def unavailable(*a, **kw):
        if failure == 'incomplete':
            raise IncompleteStructuredOutput('Incomplete result')
        if failure == 'provider_error':
            raise RuntimeError('Provider unavailable')
        if failure == 'invalid':
            return {'decision': 'publish_anything', 'reason': 'Invalid decision', 'supporting_wording': ''}
        return None

    monkeypatch.setattr(base, 'chat_completion_parse', unavailable)
    assert operation(agent, monkeypatch, 'complete_task', {'response': 'Must not publish.', 'candidate_id': uid}) == {'stopped': True}

    assert agent.status == 'failed'
    assert state(agent) == before
    assert cache(agent) == previous
    assert tasks.steps(agent).filter_by(name='complete_task').one().state == 'failed'
    assert db.session.get(AgentArtifact, uid)


@pytest.mark.parametrize('ownership', ['cancelled', 'replaced'])
def test_late_permission_result_cannot_publish_or_save_cache_after_ownership_changes(agent, monkeypatch, ownership):
    uid = candidate(agent, monkeypatch)
    before = state(agent)

    def change_ownership():
        assert not db.session().in_transaction()
        if ownership == 'cancelled':
            service.stop(agent, cancelled=True)
        else:
            agent.lease_token = 'replacement-worker'
            agent.lease_until = service.now() + timedelta(minutes=5)
            db.session.commit()

    calls = permission_provider(monkeypatch, 'not_forbidden', callback=change_ownership)
    assert operation(agent, monkeypatch, 'complete_task', {'response': 'Late result.', 'candidate_id': uid}) == {'stopped': True}

    assert len(calls) == 1
    assert state(agent) == before
    assert cache(agent) is None
    if ownership == 'cancelled':
        assert agent.status == 'cancelled'
    else:
        assert agent.status == 'running' and agent.lease_token == 'replacement-worker'


def test_invalid_candidate_is_rejected_before_permission_provider(agent, monkeypatch):
    monkeypatch.setattr(base, 'chat_completion_parse', lambda *a, **kw: pytest.fail('Reject invalid candidate before checking permission'))
    before = state(agent)

    assert operation(agent, monkeypatch, 'complete_task', {'response': 'Invalid candidate.', 'candidate_id': 'outside-this-assignment'}) == {'continued': True}

    assert last_result(agent)['error'] is True
    assert state(agent) == before
    assert cache(agent) is None
