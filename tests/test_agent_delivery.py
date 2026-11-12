"""Durable continuation delivery; provider/broker calls are boundary doubles."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest

from backend import agent_tasks as tasks, case_assistant_tasks as workers
from backend.ai import agent_tools, base
from backend.database import db
from backend.models import AgentArtifact, AgentStep
from backend.util import case_assistant_workflow as service
from tests.test_agent_controller import agent, app, call, choose, operation, tick


def recover(row):
    # Make this saved delivery eligible for the existing 30-second Beat check.
    row.updated_at = service.now() - timedelta(seconds=31)
    db.session.commit()
    workers.reconcile_case_assistant_workflows.run()
    db.session.expire_all()


@pytest.mark.parametrize('execution', ['model', 'tool', 'tool_error'])
def test_saved_handoff_survives_process_loss_without_repeating_completed_work(agent, monkeypatch, execution):
    calls = {'model': 0, 'tool': 0}
    original_read = agent_tools.Operations.read_sources

    def model(request):
        calls['model'] += 1
        if any(item.get('type') == 'function_call_output' for item in request['input']):
            return call('complete_task', {'response': 'Saved work retained.', 'candidate_id': None})
        return call('read_sources', {'artifact_id': 'missing' if execution == 'tool_error' else 'current_draft',
            'ranges': [], 'start': 0, 'end': None})

    def read(self, args):
        calls['tool'] += 1
        return original_read(self, args)

    monkeypatch.setattr(base, 'responses_completion', model)
    monkeypatch.setattr(agent_tools.Operations, 'read_sources', read)
    if execution != 'model':
        assert tick(agent) == {'continued': True}
    previous_id = agent.planning_task_id
    real_dispatch = tasks.dispatch
    real_commit = db.session.commit
    existing_ready = {step.id for step in tasks.steps(agent).filter_by(state='ready').all()}
    handoff_commits = []

    def commit():
        # Inspect the actual commit boundary, not just the eventual saved state:
        # a result must never commit before its delivery intent/lease handoff.
        ready = {step.id for step in tasks.steps(agent).filter_by(state='ready').all()}
        if ready - existing_ready:
            assert agent.planning_task_id != previous_id and agent.dispatch_pending is True
            assert agent.lease_token is None and agent.lease_until is None
            handoff_commits.append(True)
        return real_commit()

    def crash(*args, **kwargs):
        raise SystemExit('Process lost after SQL commit, before Redis publication')

    monkeypatch.setattr(tasks, 'dispatch', crash)
    monkeypatch.setattr(db.session, 'commit', commit)
    with pytest.raises(SystemExit):
        tick(agent)
    monkeypatch.setattr(db.session, 'commit', real_commit)
    assert handoff_commits
    next_id = agent.planning_task_id
    assert next_id != previous_id
    assert agent.status == 'running' and agent.dispatch_pending is True
    assert agent.lease_token is None and agent.lease_until is None
    saved_steps = {step.id: (step.result_json, step.summary_json)
                   for step in tasks.steps(agent).filter_by(state='ready').all()}
    saved_artifacts = {item.id: item.content_json for item in AgentArtifact.query.all()}
    completed_calls = dict(calls)

    sent = []
    monkeypatch.setattr(tasks, 'dispatch', real_dispatch)
    monkeypatch.setattr(tasks.advance, 'apply_async', lambda **kwargs: sent.append(kwargs))
    recover(agent)
    assert [message['task_id'] for message in sent] == [next_id]
    assert agent.dispatch_pending is True  # Only claiming, not sending, clears it.
    assert calls == completed_calls
    assert tick(agent) == {'continued': True}
    assert calls['model'] == (1 if execution == 'model' else 2)
    assert calls['tool'] == 1
    for step_id, content in saved_steps.items():
        step = db.session.get(AgentStep, step_id)
        assert (step.state, step.result_json, step.summary_json) == ('ready', *content)
    for artifact_id, content in saved_artifacts.items():
        assert db.session.get(AgentArtifact, artifact_id).content_json == content


@pytest.mark.parametrize('entry', ['submission', 'clarification', 'retry'])
def test_entry_commit_already_contains_its_recoverable_task_id(agent, monkeypatch, entry):
    initial_id = agent.planning_task_id
    real_dispatch = tasks.dispatch
    if entry == 'submission':
        service.stop(agent, 'Finish fixture assignment before a new submission')
        data = json.loads(agent.data_json)
        data.pop('workflow_run_id', None)
        # No dispatcher has run for this newly admitted assignment yet.
        agent = service.submit(agent.user_id, data)
        initial_id = agent.planning_task_id
        assert initial_id
        assert service.submit(agent.user_id, {**data, 'workflow_run_id': agent.id}).planning_task_id == initial_id
    elif entry == 'clarification':
        operation(agent, monkeypatch, 'request_user_input', {'question': 'Which exhibit?', 'options': []})
        initial_id = agent.planning_task_id
        monkeypatch.setattr(tasks, 'dispatch', lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit()))
        with pytest.raises(SystemExit):
            tasks.answer_question(agent, {'answer': 'Use Exhibit B.'})
        assert json.loads(tasks.steps(agent).filter_by(name='request_user_input').one().result_json) == {'answer': 'Use Exhibit B.'}
    else:
        operation(agent, monkeypatch, 'list_documents', {})
        initial_id = agent.planning_task_id
        service.stop(agent, 'Stop before explicit Retry')
        service.retry(agent)  # Simulate loss before its route calls dispatch.
    saved_id = agent.planning_task_id
    assert saved_id and agent.dispatch_pending is True and agent.lease_token is None
    if entry != 'submission':
        assert saved_id != initial_id
    sent = []
    monkeypatch.setattr(tasks, 'dispatch', real_dispatch)
    monkeypatch.setattr(tasks.advance, 'apply_async', lambda **kwargs: sent.append(kwargs['task_id']))
    recover(agent)
    assert sent == [saved_id]


@pytest.mark.parametrize('accepted', [False, True])
def test_publication_failure_preserves_pending_delivery_and_its_id(agent, monkeypatch, accepted):
    saved_id = agent.planning_task_id
    delivered = []

    def broker(**kwargs):
        if accepted:
            delivered.append(kwargs['task_id'])
        raise ConnectionError('Broker acknowledgment unavailable')

    monkeypatch.setattr(tasks.advance, 'apply_async', broker)
    monkeypatch.setattr(base, 'responses_completion', lambda *args: pytest.fail('Sending must not call a provider'))
    tasks.dispatch(agent)
    db.session.expire_all()
    assert agent.status == 'running' and agent.dispatch_pending is True
    assert agent.planning_task_id == saved_id and agent.lease_token is None
    assert service.fail_task(saved_id, 'Lost before claiming') is False
    assert agent.status == 'running' and agent.dispatch_pending is True
    monkeypatch.setattr(tasks.advance, 'apply_async', lambda **kwargs: delivered.append(kwargs['task_id']))
    recover(agent)
    assert delivered == [saved_id] * (2 if accepted else 1)


@pytest.mark.parametrize('raise_after_acceptance', [False, True])
def test_late_publisher_cannot_overwrite_a_newer_pending_handoff(agent, monkeypatch, raise_after_acceptance):
    old_id = agent.planning_task_id
    choose(monkeypatch, 'list_documents', {})
    sent = []

    def deliver_immediately(**kwargs):
        sent.append(kwargs['task_id'])
        if kwargs['task_id'] == old_id:
            # The worker can finish before the preceding send gets its response.
            assert tick(agent) == {'continued': True}
            if raise_after_acceptance:
                raise ConnectionError('The acknowledgment was lost after execution')

    monkeypatch.setattr(tasks.advance, 'apply_async', deliver_immediately)
    tasks.dispatch(agent, task_id=old_id)
    db.session.expire_all()
    new_id = agent.planning_task_id
    assert new_id != old_id and sent == [old_id, new_id]
    assert agent.status == 'running' and agent.dispatch_pending is True
    assert agent.lease_token is None
    assert service.fail_task(old_id, 'Late notification') is False
    assert agent.status == 'running' and agent.planning_task_id == new_id
    assert workers.claim_workflow(agent.id, task_id=old_id, task_field='planning_task_id') is None


def test_duplicate_deliveries_have_one_conditional_sql_claim(agent, app):
    workflow_id, task_id = agent.id, agent.planning_task_id
    db.session.commit()
    barrier = Barrier(2)

    def claim():
        with app.app_context():
            barrier.wait(timeout=10)
            try:
                return workers.claim_workflow(workflow_id, task_id=task_id, task_field='planning_task_id')
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(token is not None for token in results) == 1
    db.session.expire_all()
    assert agent.lease_token in results and agent.dispatch_pending is False
    assert workers.claim_workflow(workflow_id, task_id=task_id, task_field='planning_task_id') is None


@pytest.mark.parametrize('boundary', ['active_lease', 'expired_lease', 'waiting', 'cancelled', 'failed', 'completed', 'deadline', 'not_pending'])
def test_recovery_never_restarts_execution_or_stopped_assignments(agent, monkeypatch, boundary):
    if boundary in ('active_lease', 'expired_lease'):
        assert workers.claim_workflow(agent.id, task_id=agent.planning_task_id, task_field='planning_task_id')
        if boundary == 'expired_lease':
            agent.lease_until = service.now() - timedelta(seconds=1)
    elif boundary == 'waiting':
        operation(agent, monkeypatch, 'request_user_input', {'question': 'Which exhibit?', 'options': []})
    elif boundary in ('cancelled', 'failed'):
        service.stop(agent, 'Stopped', cancelled=boundary == 'cancelled')
    elif boundary == 'completed':
        operation(agent, monkeypatch, 'complete_task', {'response': 'Published once.', 'candidate_id': None})
    elif boundary == 'deadline':
        agent.deadline = service.now() - timedelta(seconds=1)
    else:
        agent.dispatch_pending = False
    sent = []
    monkeypatch.setattr(tasks.advance, 'apply_async', lambda **kwargs: sent.append(kwargs))
    recover(agent)
    assert sent == []
    if boundary in ('deadline', 'expired_lease'):
        assert agent.status == 'failed'
    elif boundary == 'active_lease':
        assert agent.status == 'running' and agent.lease_token


def test_periodic_delivery_reservation_preserves_progress_and_existing_deadlines(agent, monkeypatch):
    clock = [service.now()]
    monkeypatch.setattr(service, 'now', lambda: clock[0])
    saved_id, progress, deadline = agent.planning_task_id, agent.last_progress_at, agent.deadline
    sent = []
    monkeypatch.setattr(tasks.advance, 'apply_async', lambda **kwargs: sent.append(kwargs['task_id']))
    for _ in range(3):
        clock[0] += timedelta(seconds=31)
        workers.reconcile_case_assistant_workflows.run()
        workers.reconcile_case_assistant_workflows.run()  # No burst from a second check.
        db.session.expire_all()
        assert agent.last_progress_at == progress and agent.deadline == deadline
    assert sent == [saved_id] * 3
    clock[0] = progress + timedelta(seconds=1801)
    workers.reconcile_case_assistant_workflows.run()
    assert agent.status == 'failed' and agent.dispatch_pending is False
    assert sent == [saved_id] * 3


def test_concurrent_recovery_checks_reserve_only_one_periodic_send(agent, app, monkeypatch):
    workflow_id, task_id = agent.id, agent.planning_task_id
    agent.updated_at = service.now() - timedelta(seconds=31)
    db.session.commit()
    sent = []
    monkeypatch.setattr(tasks.advance, 'apply_async', lambda **kwargs: sent.append(kwargs['task_id']))
    barrier = Barrier(2)

    def send():
        with app.app_context():
            row = db.session.get(service.Workflow, workflow_id)
            barrier.wait(timeout=10)
            try:
                return tasks.dispatch(row, task_id=task_id, recover=True)
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(), range(2)))
    assert sum(bool(sent_now) for sent_now in results) == 1
    assert sent == [task_id]


def test_claimed_failure_requires_manual_retry_and_does_not_republish(agent, monkeypatch):
    task_id = agent.planning_task_id
    assert workers.claim_workflow(agent.id, task_id=task_id, task_field='planning_task_id')
    assert service.fail_task(task_id, 'Claimed worker stopped') is True
    assert agent.status == 'failed' and agent.dispatch_pending is False
    sent = []
    monkeypatch.setattr(tasks.advance, 'apply_async', lambda **kwargs: sent.append(kwargs))
    recover(agent)
    assert sent == []
