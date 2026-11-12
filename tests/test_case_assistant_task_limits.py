"""Case Assistant's shared turn budget and timeout cleanup."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from backend import everything


def _stub_turn_dependencies(monkeypatch):
    monkeypatch.setattr(
        everything,
        "ChatThread",
        SimpleNamespace(resolve_id=lambda _draft_id: 17),
    )
    monkeypatch.setattr(everything, "task_llm_usage_scope", lambda *_args: nullcontext())


def test_turn_and_confirmation_have_one_twenty_minute_budget():
    assert everything.everything_turn_task.soft_time_limit == 1200
    assert everything.everything_turn_task.time_limit == 1260
    assert everything.everything_confirmed_workflow_task.soft_time_limit == 1200
    assert everything.everything_confirmed_workflow_task.time_limit == 1260
    assert everything.DRAFT_LOCK_TTL == 1290
    # Old phase tasks remain registered for already-queued chains.
    assert callable(everything.everything_task_phase1.run)
    assert callable(everything.everything_task_phase2.run)


def test_draft_lock_gets_1290_second_fallback_expiry(monkeypatch):
    calls = []
    monkeypatch.setattr(
        everything,
        "_redis",
        SimpleNamespace(set=lambda *args, **kwargs: calls.append((args, kwargs)) or True),
    )
    assert everything.acquire_draft_lock(17, 4)
    assert calls == [(('draft_lock:17', '4'), {'nx': True, 'ex': 1290})]


def test_combined_turn_runs_both_phases_and_releases_lock(monkeypatch):
    _stub_turn_dependencies(monkeypatch)
    calls = []
    task_ids = []
    payload = {'doc_id': 17, 'user_id': 4, 'phase1_task_id': 'turn-1'}
    monkeypatch.setattr(everything, '_everything_phase1_body',
                        lambda task, data: task_ids.append(task.request.id)
                        or calls.append(('phase1', task, data)) or payload)
    monkeypatch.setattr(everything, '_everything_phase2_body',
                        lambda task, value, doc_id: task_ids.append(task.request.id)
                        or calls.append(('phase2', task, value, doc_id))
                        or {'response': 'Done', 'text': {}})
    monkeypatch.setattr(everything, '_record_successful_chat_answer',
                        lambda doc_id, result: calls.append(('record', doc_id, result)))
    monkeypatch.setattr(everything, 'release_draft_lock',
                        lambda doc_id, user_id: calls.append(('release', doc_id, user_id)))

    result = everything.everything_turn_task.apply(
        args=[{'draftId': 'draft-17', 'query': 'Help', 'user_id': 4}],
        task_id='turn-1',
    ).get()

    assert result == {'response': 'Done', 'text': {}}
    assert calls[0][0] == 'phase1'
    assert calls[1][0] == 'phase2'
    assert calls[0][1] is calls[1][1]
    assert task_ids == ['turn-1', 'turn-1']
    assert calls[0][2]['draftId'] == 'draft-17'
    assert calls[1][2:] == (payload, 17)
    assert calls[-1] == ('release', 17, 4)


@pytest.mark.parametrize('phase1_result', [
    {'awaiting_confirmation': True, 'doc_id': 17, 'user_id': 4},
    {'error': True, 'response': 'Planning failed', 'text': {}, 'error_message': 'Planning failed'},
    {
        'error': True, 'response': 'Timed out', 'text': {}, 'error_message': 'Timed out',
        'retry': True, 'retryable': True, 'error_code': 'task_timeout',
    },
])
def test_combined_turn_releases_lock_on_phase1_terminal_result(monkeypatch, phase1_result):
    _stub_turn_dependencies(monkeypatch)
    released = []
    monkeypatch.setattr(everything, '_everything_phase1_body', lambda *_args: phase1_result)
    monkeypatch.setattr(everything, '_everything_phase2_body',
                        lambda *_args: pytest.fail('phase 2 must not run'))
    monkeypatch.setattr(everything, 'release_draft_lock',
                        lambda doc_id, user_id: released.append((doc_id, user_id)))

    result = everything.everything_turn_task.run({'draftId': 'draft-17', 'query': 'Help', 'user_id': 4})

    assert result.get('awaiting_confirmation') or result['error']
    if 'error_code' in phase1_result:
        assert result['error_code'] == 'task_timeout'
        assert result['retryable'] is True
    assert released == [(17, 4)]


def test_combined_turn_releases_lock_on_phase1_exception(monkeypatch):
    _stub_turn_dependencies(monkeypatch)
    released = []
    def fail_phase1(*_args):
        raise RuntimeError('planning failed')
    monkeypatch.setattr(everything, '_everything_phase1_body', fail_phase1)
    monkeypatch.setattr(everything, 'release_draft_lock',
                        lambda doc_id, user_id: released.append((doc_id, user_id)))

    with pytest.raises(RuntimeError, match='planning failed'):
        everything.everything_turn_task.run({'draftId': 'draft-17', 'query': 'Help', 'user_id': 4})
    assert released == [(17, 4)]


def test_combined_turn_soft_timeout_is_retryable_and_releases_lock(monkeypatch):
    _stub_turn_dependencies(monkeypatch)
    released = []
    timeout_calls = []
    monkeypatch.setattr(everything, '_everything_phase1_body',
                        lambda *_args: {'doc_id': 17, 'user_id': 4})
    def timeout_phase2(*_args):
        raise SoftTimeLimitExceeded()
    monkeypatch.setattr(everything, '_everything_phase2_body', timeout_phase2)
    monkeypatch.setattr(everything, '_case_assistant_soft_timeout_result',
                        lambda doc_id, task_id: timeout_calls.append((doc_id, task_id))
                        or {'error': True, 'retryable': True, 'error_code': 'task_timeout'})
    monkeypatch.setattr(everything, 'release_draft_lock',
                        lambda doc_id, user_id: released.append((doc_id, user_id)))

    result = everything.everything_turn_task.run({'draftId': 'draft-17', 'query': 'Help', 'user_id': 4})

    assert result == {'error': True, 'retryable': True, 'error_code': 'task_timeout'}
    assert timeout_calls == [(17, None)]
    assert released == [(17, 4)]


def test_soft_timeout_clears_only_its_active_run(monkeypatch):
    states = [
        {
            'active_task_id': 'timed-out-task',
            'active_workflow': everything.create_active_workflow('wf_timed-out-task', 'Question'),
            'draft': {'title': 'Kept draft', 'sections': []},
        },
        {
            'active_task_id': 'newer-task',
            'active_workflow': everything.create_active_workflow('wf_newer-task', 'New question'),
            'draft': {'title': 'Newer draft', 'sections': []},
        },
    ]
    saves = []
    memory = object()
    monkeypatch.setattr(everything, 'DBMemory', lambda **_kwargs: memory)
    monkeypatch.setattr(everything, 'load_state_from_memory', lambda _memory: states.pop(0))
    monkeypatch.setattr(everything, 'save_state_to_memory',
                        lambda state, _memory, **kwargs: saves.append(state.copy()))

    timed_out = everything._case_assistant_soft_timeout_result(17, 'timed-out-task')
    newer = everything._case_assistant_soft_timeout_result(17, 'timed-out-task')

    assert timed_out['retryable'] and timed_out['error_code'] == 'task_timeout'
    assert newer['retryable'] and newer['error_code'] == 'task_timeout'
    assert len(saves) == 1
    assert 'active_task_id' not in saves[0]
    assert saves[0]['active_workflow']['status'] == 'failed'
    assert saves[0]['active_workflow']['pending_turn'] is None


def test_confirmation_phase1_exception_releases_lock(monkeypatch):
    _stub_turn_dependencies(monkeypatch)
    state = {
        'active_workflow': {
            'workflow_run_id': 'wf-existing',
            'status': 'awaiting_user_confirmation',
            'pending_confirmation': {
                'workflow_run_id': 'wf-existing',
                'original_turn_input': {'draftId': 'draft-17', 'query': 'Help'},
                'original_decision': {},
            },
        },
    }
    monkeypatch.setattr(everything, 'DBMemory', lambda **_kwargs: object())
    monkeypatch.setattr(everything, 'load_state_from_memory', lambda _memory: state)
    monkeypatch.setattr(everything, '_confirmation_clarification', lambda _decision: '')
    def fail_phase1(*_args, **_kwargs):
        raise RuntimeError('confirmed planning failed')
    monkeypatch.setattr(everything, '_everything_phase1_body', fail_phase1)
    released = []
    monkeypatch.setattr(everything, 'release_draft_lock',
                        lambda doc_id, user_id: released.append((doc_id, user_id)))
    task = SimpleNamespace(request=SimpleNamespace(id='confirmed-task'))

    with pytest.raises(RuntimeError, match='confirmed planning failed'):
        everything._everything_confirmed_workflow_body(task, {
            'draftId': 'draft-17', 'workflow_run_id': 'wf-existing',
            'user_id': 4, 'decision': {},
        })
    assert released == [(17, 4)]


def test_confirmation_phase1_soft_timeout_cleans_its_run(monkeypatch):
    _stub_turn_dependencies(monkeypatch)
    state = {
        'draft': {'title': 'Unchanged', 'sections': []},
        'active_workflow': {
            'workflow_run_id': 'wf-existing',
            'status': 'awaiting_user_confirmation',
            'pending_confirmation': {
                'workflow_run_id': 'wf-existing',
                'original_turn_input': {'draftId': 'draft-17', 'query': 'Help'},
                'original_decision': {},
            },
        },
    }
    monkeypatch.setattr(everything, 'DBMemory', lambda **_kwargs: object())
    monkeypatch.setattr(everything, 'load_state_from_memory', lambda _memory: state)
    saves = []
    monkeypatch.setattr(everything, 'save_state_to_memory',
                        lambda saved_state, _memory, **kwargs: saves.append(saved_state.copy()))
    monkeypatch.setattr(everything, '_confirmation_clarification', lambda _decision: '')
    def timeout_phase1(task, *_args, **_kwargs):
        state['active_task_id'] = task.request.id
        raise SoftTimeLimitExceeded()
    monkeypatch.setattr(everything, '_everything_phase1_body', timeout_phase1)
    released = []
    monkeypatch.setattr(everything, 'release_draft_lock',
                        lambda doc_id, user_id: released.append((doc_id, user_id)))

    result = everything.everything_confirmed_workflow_task.apply(
        args=[{
            'draftId': 'draft-17', 'workflow_run_id': 'wf-existing',
            'user_id': 4, 'decision': {},
        }],
        task_id='confirm-1',
    ).get()

    assert result['error_code'] == 'task_timeout'
    assert result['retryable'] is True
    assert released == [(17, 4)]
    assert state.get('active_task_id') is None
    assert state['active_workflow']['status'] == 'failed'
    assert saves


def test_stale_confirmation_does_not_release_any_lock(monkeypatch):
    _stub_turn_dependencies(monkeypatch)
    monkeypatch.setattr(everything, 'DBMemory', lambda **_kwargs: object())
    monkeypatch.setattr(everything, 'load_state_from_memory',
                        lambda _memory: {'active_workflow': {}})
    monkeypatch.setattr(everything, 'release_draft_lock',
                        lambda *_args: pytest.fail('stale confirmation owns no lock'))
    task = SimpleNamespace(request=SimpleNamespace(id='confirmed-task'))

    result = everything._everything_confirmed_workflow_body(task, {
        'draftId': 'draft-17', 'workflow_run_id': 'wf-stale', 'user_id': 4,
    })
    assert result['error'] is True


def test_parallel_file_extraction_does_not_wait_for_threads_after_soft_timeout(monkeypatch):
    shutdown_calls = []
    class FakePool:
        def __init__(self, max_workers):
            pass

        def submit(self, *_args, **_kwargs):
            return object()

        def shutdown(self, *, wait, cancel_futures=False):
            shutdown_calls.append((wait, cancel_futures))

    def timeout_while_waiting(_futures):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(everything.concurrent.futures, 'ThreadPoolExecutor', FakePool)
    monkeypatch.setattr(everything.concurrent.futures, 'as_completed', timeout_while_waiting)

    with pytest.raises(SoftTimeLimitExceeded):
        everything._extract_relevant_entries_threaded('question', [{'file_id': 'A'}])

    assert shutdown_calls == [(False, True)]
