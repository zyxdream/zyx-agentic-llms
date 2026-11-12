"""Prompt failures are terminal until explicit Retry; no provider/network I/O."""
import json
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from backend import case_assistant_tasks as tasks
from backend.database import db
from backend.models import ConversationTurn
from backend.util import case_assistant_workflow as service
from tests.test_case_assistant_workflows import app, workflow, complete


@pytest.mark.parametrize('phase', ['planning', 'query', 'retrieval_review', 'reduce', 'callback', 'answer'])
@pytest.mark.parametrize('timeout', [False, True])
def test_first_exception_fails_request_and_cancels_unfinished_work(workflow, monkeypatch, phase, timeout):
    from backend import everything
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise SoftTimeLimitExceeded() if timeout else ValueError('mock provider error')
    source = tasks.work_rows(workflow).one()
    if phase == 'planning':
        workflow.payload_json = None
        task, args, field = tasks.plan_prompt, [workflow.id], 'planning_task_id'
        monkeypatch.setattr(everything, '_everything_phase1_body', fail)
    elif phase in ('query', 'retrieval_review', 'reduce'):
        source.kind = phase
        task, args, field = tasks.process_section, [source.id], None
        processors = {'query': (tasks, 'extraction_result'), 'reduce': (tasks, 'reduction_result'),
                      'retrieval_review': (tasks.retrieval_review, 'result')}
        monkeypatch.setattr(*processors[phase], fail)
    else:
        complete(workflow, monkeypatch)
        assert tasks.prepare_answer(workflow)
        workflow.group_id = 'test-group'
        task = tasks.after_extraction if phase == 'callback' else tasks.answer_prompt
        args, field = [workflow.id, workflow.group_id], 'answer_task_id'
        monkeypatch.setattr(tasks if phase == 'callback' else everything,
                            'prepare_answer' if phase == 'callback' else '_everything_phase2_body', fail)
    if field:
        setattr(workflow, field, 'failed-task')
    else:
        source.task_id = 'failed-task'
    pending = tasks.add_work(workflow, 'query', {'start': 2, 'end': 10}, source.document_id, source.artifact_id)
    # Answer/callback require all extraction to be complete before entering the
    # provider. An extra pending task is useful for testing failure cancellation
    # in planning and section tasks only.
    if phase in ('callback', 'answer'):
        db.session.delete(pending)
    db.session.commit()
    monkeypatch.setattr(task, 'retry', lambda *a, **kw: pytest.fail('Automatic Celery retry'))
    task.push_request(id='failed-task')
    try:
        with pytest.raises(SoftTimeLimitExceeded if timeout else ValueError):
            task.run(*args)
        assert workflow.status == 'failed' and workflow.active_draft_id is None
        assert len(calls) == 1
        assert not tasks.work_rows(workflow).filter(service.Work.state.in_(['queued', 'running'])).count()
        if phase in ('query', 'retrieval_review', 'reduce'):
            assert source.state == 'failed' and source.attempts == 1 and pending.state == 'cancelled'
        if phase in ('callback', 'answer'):
            assert source.state == 'ready' and source.result_id
        # Obsolete delivery does not execute the failed provider a second time.
        task.run(*args)
        assert len(calls) == 1
    finally:
        task.pop_request()


@pytest.mark.parametrize('phase', ['planning', 'group', 'answer'])
def test_dispatch_failure_is_terminal_without_periodic_redispatch(workflow, monkeypatch, phase):
    publications = []
    def unavailable(*args, **kwargs):
        publications.append(kwargs)
        assert kwargs['retry'] is False
        raise ConnectionError('mock broker unavailable')
    if phase == 'planning':
        workflow.payload_json = None
        monkeypatch.setattr(tasks.plan_prompt, 'apply_async', unavailable)
        dispatch = tasks.dispatch_planning
    elif phase == 'group':
        monkeypatch.setattr(tasks, 'chord', lambda *a, **kw: SimpleNamespace(apply_async=unavailable))
        dispatch = tasks.dispatch_group
    else:
        complete(workflow, monkeypatch)
        assert tasks.prepare_answer(workflow)
        monkeypatch.setattr(tasks.answer_prompt, 'apply_async', unavailable)
        dispatch = tasks.dispatch_answer
    db.session.commit()
    dispatch(workflow)
    assert workflow.status == 'failed' and workflow.active_draft_id is None
    saved_ids = (workflow.planning_task_id, workflow.group_id, workflow.answer_task_id)
    tasks.reconcile_case_assistant_workflows.run()
    service.status(workflow)
    assert len(publications) == 1
    assert (workflow.planning_task_id, workflow.group_id, workflow.answer_task_id) == saved_ids


@pytest.mark.parametrize('lost', ['planning', 'section', 'answer', 'publication'])
def test_polling_expires_silent_worker_loss_without_scheduling(workflow, app, monkeypatch, lost):
    complete(workflow, monkeypatch)
    ready = tasks.work_rows(workflow).one()
    pending = tasks.add_work(workflow, 'query', {'start': 1, 'end': 10}, ready.document_id, ready.artifact_id)
    expired = service.now() - timedelta(seconds=1)
    if lost == 'section':
        pending.state, pending.lease_token, pending.lease_until = 'running', 'lost', expired
    elif lost == 'publication':
        workflow.last_progress_at = service.now() - timedelta(seconds=1801)
        workflow.dispatch_pending = True
    else:
        workflow.phase = 'classify' if lost == 'planning' else 'draft'
        workflow.lease_token, workflow.lease_until = 'lost', expired
    db.session.commit()
    for name in ('dispatch_planning', 'dispatch_group', 'dispatch_answer'):
        monkeypatch.setattr(tasks, name, lambda *a, **kw: pytest.fail('Failure check scheduled work'))
    response = app.test_client().get(f'/api/everything/workflows/{workflow.id}')
    assert response.status_code == 200 and response.json['status'] == 'failed'
    assert 'Retry' in response.json['value']['error_message']
    assert ready.state == 'ready' and ready.result_id
    assert pending.state == ('failed' if lost == 'section' else 'cancelled')
    assert workflow.lease_token is None and workflow.active_draft_id is None
    tasks.reconcile_case_assistant_workflows.run()
    assert workflow.status == 'failed'


def test_healthy_checks_and_duplicate_delivery_do_not_schedule_or_reclaim(workflow, monkeypatch):
    item = tasks.work_rows(workflow).one()
    item.state, item.task_id, item.lease_token = 'running', 'original', 'owned'
    item.lease_until = service.now() + timedelta(seconds=100)
    db.session.commit()
    for name in ('dispatch_planning', 'dispatch_group', 'dispatch_answer', 'extraction_result'):
        monkeypatch.setattr(tasks, name, lambda *a, **kw: pytest.fail('Unexpected processing'))
    monkeypatch.setattr(tasks.process_section, 'retry', lambda *a, **kw: pytest.fail('Duplicate retried'))
    assert tasks.process_section.run(item.id) == {'stale': True}
    tasks.reconcile_case_assistant_workflows.run()
    assert service.status(workflow)['status'] == 'running'
    assert item.lease_token == 'owned' and item.task_id == 'original'


def test_planning_continues_more_than_three_times_without_celery_retry(workflow, monkeypatch):
    from backend import everything
    payload = json.loads(workflow.payload_json)
    payload.pop('_sections_planned', None)
    payload.pop('_planned_file_ids', None)
    tasks.work_rows(workflow).delete()
    workflow.payload_json, workflow.phase = None, 'classify'
    db.session.commit()
    calls, scheduled = [], []
    def plan(*args, **kwargs):
        calls.append(1)
        if len(calls) <= 5:
            raise tasks.WorkPending('Next successfully checkpointed operation')
        return payload
    monkeypatch.setattr(everything, '_everything_phase1_body', plan)
    monkeypatch.setattr(tasks.plan_prompt, 'retry', lambda *a, **kw: pytest.fail('Continuation used retry'))
    monkeypatch.setattr(tasks.plan_prompt, 'apply_async', lambda **kw: scheduled.append(kw))
    tasks.dispatch_planning(workflow)
    first = workflow.planning_task_id
    tasks.dispatch_planning(workflow)  # An HTTP resubmission must not restart it.
    assert workflow.planning_task_id == first and len(scheduled) == 1
    for _ in range(6):
        tasks.plan_prompt.push_request(id=workflow.planning_task_id)
        try:
            tasks.plan_prompt.run(workflow.id)
        finally:
            tasks.plan_prompt.pop_request()
    assert len(calls) == len(scheduled) == 6
    assert len({s['task_id'] for s in scheduled}) == 6
    assert workflow.status == 'running' and workflow.phase == 'extract'
    assert tasks.work_rows(workflow, 'query').count() == 1


@pytest.mark.parametrize('kind', ['section', 'planning', 'callback', 'answer'])
def test_failure_notification_from_before_retry_cannot_fail_new_attempt(workflow, monkeypatch, kind):
    item = tasks.work_rows(workflow).one()
    if kind == 'section':
        item.task_id = 'old'
    else:
        setattr(workflow, 'planning_task_id' if kind == 'planning' else 'answer_task_id', 'old')
    db.session.commit()
    tasks.prompt_task_failed.run(SimpleNamespace(id='old'), ValueError('mock failure'), None)
    assert workflow.status == 'failed'
    if kind == 'section':
        assert item.state == 'failed'
    service.retry(workflow)
    tasks.dispatch_group(workflow)
    new_ids = (workflow.planning_task_id, workflow.answer_task_id, item.task_id)
    tasks.prompt_task_failed.run(SimpleNamespace(id='old'), ValueError('late failure'), None)
    assert workflow.status == 'running'
    assert (workflow.planning_task_id, workflow.answer_task_id, item.task_id) == new_ids


def test_already_published_answer_repairs_status_without_generating_again(workflow, monkeypatch):
    complete(workflow, monkeypatch)
    from backend.util import workspace_store
    state = workspace_store.load(workflow.chat_thread_id)
    workspace_store.update_document(workflow.chat_thread_id, state['draft_document_id'],
        {'title': 'Published draft'}, expected_version=state['draft_version'])
    workspace_store.append_turn(workflow.chat_thread_id, 'Question', 'Published answer',
        workflow_id=workflow.id, meta={'workflow_completed': True})
    workflow.lease_token, workflow.lease_until = 'lost', service.now() - timedelta(seconds=1)
    db.session.commit()
    for name in ('dispatch_planning', 'dispatch_group', 'dispatch_answer'):
        monkeypatch.setattr(tasks, name, lambda *a, **kw: pytest.fail('Published answer regenerated'))
    result = service.status(workflow)
    assert result['status'] == 'completed' and result['value']['response'] == 'Published answer'
    assert workflow.active_draft_id is None
    service.fail_task(workflow.answer_task_id, 'Late task failure')
    assert workflow.status == 'completed'
    assert len(workspace_store.load(workflow.chat_thread_id)['chat_history']) == 1
    assert ConversationTurn.query.filter_by(workflow_run_id=workflow.id).count() == 1


def test_prompt_task_options_and_worker_cli_do_not_enable_recovery(app):
    from backend.case_assistant_worker import worker_main
    for task in (tasks.plan_prompt, tasks.process_section, tasks.after_extraction, tasks.answer_prompt):
        assert task.acks_late is False and task.reject_on_worker_lost is False
        assert task.acks_on_failure_or_timeout is True and task.max_retries == 0
        assert task.soft_time_limit == 1200 and task.time_limit == 1260
    with pytest.raises(ValueError, match='removed'):
        worker_main(['--recovery'])


def test_beat_failure_check_uses_main_queue_and_keeps_preparation_queue(app):
    import celery
    previous = celery.current_app._get_current_object()
    from backend.app import celery_init_app
    check_app = celery_init_app(app)
    try:
        schedule = check_app.conf.beat_schedule
        check = schedule['reconcile-case-assistant-workflows']
        assert check['schedule'] == 30.0
        assert check['task'] == 'backend.case_assistant_tasks.reconcile_case_assistant_workflows'
        assert check['options']['queue'] == app.config['CELERY']['task_default_queue']
        assert schedule['reconcile-document-preparation']['options']['queue'] == tasks.queue_name().replace('.extraction', '.documents')
    finally:
        check_app.close()
        previous.set_default()
        previous.set_current()


@pytest.mark.skipif(os.environ.get('LEXINTEL_RUN_CELERY_INTEGRATION') != '1',
                    reason='Opt-in isolated prefork worker failure integration')
@pytest.mark.parametrize('failure', ['worker_loss', 'hard_timeout'])
def test_isolated_worker_loss_and_hard_timeout_fail_chord_without_redelivery(workflow, app, monkeypatch, failure):
    """Kill only a disposable test child; never use the application's broker.

    Parent-worker failure handling must invoke our errback when the child cannot
    execute Python cleanup. An in-memory broker/cache isolates this test from
    live queues. All provider work is replaced before the child is forked.
    """
    import time
    import celery
    from celery.contrib.testing.worker import start_worker
    previous = celery.current_app._get_current_object()
    class FlaskTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    isolated = celery.Celery('prompt-failure-integration', task_cls=FlaskTask,
                            broker='memory://', backend='cache+memory://')
    isolated.conf.update(task_default_queue='prompt-failure-integration', worker_prefetch_multiplier=1,
                         worker_lost_wait=0.1, task_send_sent_event=False)
    calls_file = os.path.join(app.config['DOCUMENT_PREPARATION_SOURCE_DIR'], failure + '-attempts')
    def crash(*args):
        with open(calls_file, 'a') as stream:
            stream.write('attempt\n')
        if failure == 'worker_loss':
            os._exit(17)
        time.sleep(15)
        raise AssertionError('Hard timeout did not stop the test child')
    try:
        isolated.set_default()
        isolated.set_current()
        monkeypatch.setattr(tasks, 'chord', celery.chord)
        monkeypatch.setattr(tasks, 'extraction_result', crash)
        # Only the disposable extraction task has a short hard limit here.
        monkeypatch.setattr(tasks.process_section, 'soft_time_limit', None)
        monkeypatch.setattr(tasks.process_section, 'time_limit', 1)
        uid, work_id = workflow.id, tasks.work_rows(workflow).one().id
        with start_worker(isolated, pool='prefork', concurrency=1, perform_ping_check=False,
                          queues=['prompt-failure-integration', tasks.queue_name()], shutdown_timeout=15):
            tasks.dispatch_group(workflow)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                db.session.rollback()
                current = db.session.get(service.Workflow, uid)
                item = db.session.get(service.Work, work_id)
                if current.status == 'failed' and item.state == 'failed':
                    break
                time.sleep(0.1)
            assert current.status == 'failed', current.error_message
            assert item.state == 'failed' and item.attempts == 1
            assert current.active_draft_id is None
            with open(calls_file) as stream:
                assert stream.readlines() == ['attempt\n']
            tasks.reconcile_case_assistant_workflows.run()
            assert current.status == 'failed' and item.attempts == 1
    finally:
        isolated.close()
        previous.set_default()
        previous.set_current()
