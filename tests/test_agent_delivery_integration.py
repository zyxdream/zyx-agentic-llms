"""Real Celery duplicate deliveries; provider calls and external I/O are doubles."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import Counter

import celery
import pytest
import redis
from celery.contrib.testing.worker import start_worker

from backend import agent_tasks as tasks, everything
from backend.ai import base
from backend.database import db
from backend.models import AgentArtifact, AgentStep, TextEntry
from backend.util import case_assistant_workflow as service, document_preparation as prep
from backend.util import workspace_store
from tests.test_document_preparation import app, admit, receive, run


@pytest.mark.skipif(os.environ.get('LEXINTEL_RUN_CELERY_INTEGRATION') != '1',
                    reason='Opt-in isolated Redis/Celery integration')
@pytest.mark.parametrize('transport', ['memory', 'redis'])
def test_duplicate_agent_deliveries_execute_each_saved_step_once(app, monkeypatch, transport):
    """Two execution slots receive identical IDs before, during and after work."""
    temporary = process = broker = test_celery = None
    socket_path = ''
    old_app = celery.current_app._get_current_object()
    release_model = threading.Event()
    release_tool = threading.Event()
    release_final_decision = threading.Event()
    model_started = threading.Event()
    tool_started = threading.Event()
    final_decision_started = threading.Event()
    counts, deliveries, claims = Counter(), Counter(), Counter()
    count_lock = threading.Lock()

    def wait_for(predicate, message):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        pytest.fail(message)

    def saved_workflow():
        db.session.rollback()
        return service.Workflow.query.populate_existing().filter_by(id=workflow_id).one()

    def count(counter, key):
        with count_lock:
            return counter[key]

    entry = receive(admit('duplicate-agent'))
    assert run(entry)['ok']
    state = everything.create_empty_state()
    state['draft'] = {'title': 'Original', 'sections': []}
    db.session.add(TextEntry(draftId=1, textName='state', content=service.encode(state)))
    db.session.commit()
    snapshot = prep.build_snapshot(1, 'draft-1', {'promptAttachments': [{'file_id': entry['document_id']}]})
    row = service.submit(1, {'draftId': 'draft-1', 'query': 'Analyze the assignment without editing.',
                             'document_snapshot': snapshot, 'assistant_mode': 'fast'})
    workflow_id, initial_id = row.id, row.planning_task_id
    assert initial_id and row.dispatch_pending

    def model(request):
        # Real checked calls release their authorization transaction before I/O.
        service.before_call()
        with count_lock:
            counts['model'] += 1
            number = counts['model']
        if number == 1:
            model_started.set()
            assert release_model.wait(20), 'Initial model was not released'
            name, arguments = 'analyze_material', {'instructions': 'Record the analysis.', 'material_ids': []}
        else:
            assert number == 2, 'A completed model decision was repeated'
            final_decision_started.set()
            assert release_final_decision.wait(20), 'Final model was not released'
            name, arguments = 'complete_task', {'response': 'Analysis completed.', 'candidate_id': None}
        return {'status': 'completed', 'output': [{'type': 'function_call', 'call_id': f'call_{number}',
                                                  'name': name, 'arguments': json.dumps(arguments)}]}

    def analyze(*args, **kwargs):
        service.before_call()
        with count_lock:
            counts['tool'] += 1
        tool_started.set()
        assert release_tool.wait(20), 'Analysis tool was not released'
        return 'Saved analysis with its original qualifications.'

    original_claim = tasks.claim_workflow

    def claim(*args, **kwargs):
        token = original_claim(*args, **kwargs)
        with count_lock:
            deliveries[kwargs['task_id']] += 1
            if token:
                claims[kwargs['task_id']] += 1
        return token

    monkeypatch.setattr(base, 'responses_completion', model)
    monkeypatch.setattr(base, 'chat_completion', analyze)
    monkeypatch.setattr(tasks, 'claim_workflow', claim)
    monkeypatch.setattr(everything, 'finalize_llm_response_for_display', lambda text, *a, **kw: (text, {}, {}))
    monkeypatch.setattr(everything, '_enqueue_brief_update', lambda *a, **kw: None)
    monkeypatch.setattr(everything, '_record_successful_chat_answer', lambda *a, **kw: None)

    try:
        if transport == 'redis':
            binary = shutil.which('redis-server')
            if not binary:
                pytest.skip('redis-server is unavailable')
            temporary = tempfile.TemporaryDirectory(prefix='agent-delivery-redis-', dir='/tmp')
            socket_path = temporary.name + '/redis.sock'
            process = subprocess.Popen([binary, '--port', '0', '--unixsocket', socket_path, '--save', '',
                '--appendonly', 'no', '--dir', temporary.name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            broker = redis.Redis(unix_socket_path=socket_path)
            for _ in range(100):
                try:
                    broker.ping()
                    break
                except redis.ConnectionError:
                    if process.poll() is not None:
                        output = process.communicate(timeout=1)[0].decode(errors='replace')
                        if 'Operation not permitted' in output or 'Permission denied' in output:
                            pytest.skip('Sandbox prevents starting an isolated Redis socket')
                        pytest.fail('Isolated Redis did not start: ' + output[-500:])
                    time.sleep(.05)

        class FlaskTask(celery.Task):
            def __call__(self, *args, **kwargs):
                with app.app_context():
                    return self.run(*args, **kwargs)

        test_celery = celery.Celery('agent-delivery-integration', task_cls=FlaskTask,
            broker=f'redis+socket://{socket_path}?virtual_host=0' if broker else 'memory://',
            backend=f'redis+socket://{socket_path}?virtual_host=1' if broker else 'cache+memory://')
        test_celery.conf.update(task_default_queue='agent-delivery-integration', worker_prefetch_multiplier=1)
        test_celery.set_default()
        test_celery.set_current()
        advance = test_celery.tasks['backend.agent_tasks.advance']
        monkeypatch.setattr(tasks, 'advance', advance)

        # Both messages are published before either execution slot can claim.
        advance.apply_async(args=[workflow_id], task_id=initial_id)
        advance.apply_async(args=[workflow_id], task_id=initial_id)
        with start_worker(test_celery, pool='threads', concurrency=2, perform_ping_check=False,
                          queues=['agent-delivery-integration'], shutdown_timeout=15):
            assert model_started.wait(20), 'The initial decision did not start'
            advance.apply_async(args=[workflow_id], task_id=initial_id)
            wait_for(lambda: count(deliveries, initial_id) >= 3, 'Duplicate initial deliveries were not checked')
            assert count(claims, initial_id) == 1
            assert saved_workflow().dispatch_pending is False
            release_model.set()

            assert tool_started.wait(20), 'The saved tool call did not start'
            tool_id = saved_workflow().planning_task_id
            assert tool_id != initial_id
            advance.apply_async(args=[workflow_id], task_id=initial_id)
            advance.apply_async(args=[workflow_id], task_id=tool_id)
            advance.apply_async(args=[workflow_id], task_id=tool_id)
            wait_for(lambda: count(deliveries, tool_id) >= 3, 'Duplicate running-tool deliveries were not checked')
            assert count(claims, tool_id) == 1
            assert count(counts, 'tool') == 1
            release_tool.set()

            assert final_decision_started.wait(20), 'The next saved decision did not start'
            advance.apply_async(args=[workflow_id], task_id=tool_id)
            wait_for(lambda: count(deliveries, tool_id) >= 4, 'The old tool delivery was not checked after completion')
            assert count(claims, tool_id) == 1
            release_final_decision.set()
            wait_for(lambda: saved_workflow().status == 'completed', 'The assignment did not finish')
            completed_id = saved_workflow().planning_task_id
            advance.apply_async(args=[workflow_id], task_id=completed_id)
            advance.apply_async(args=[workflow_id], task_id=initial_id)
            wait_for(lambda: count(deliveries, completed_id) >= 2, 'A terminal duplicate was not checked')
            wait_for(lambda: count(deliveries, initial_id) >= 5, 'The old initial delivery was not checked after publication')

            assert count(counts, 'model') == 2 and count(counts, 'tool') == 1
            with count_lock:
                assert len(claims) == 4 and all(number == 1 for number in claims.values())
            db.session.rollback()
            assert AgentStep.query.filter_by(workflow_run_id=workflow_id, state='ready').count() == 4
            assert AgentArtifact.query.filter_by(workflow_run_id=workflow_id, kind='analysis').count() == 1
            artifact = AgentArtifact.query.filter_by(workflow_run_id=workflow_id).one()
            assert json.loads(artifact.content_json)['text'] == 'Saved analysis with its original qualifications.'
            published = workspace_store.load(row.draft_id)
            assert published['chat_history'] == [['Analyze the assignment without editing.', 'Analysis completed.']]
            assert published['draft'] == state['draft']
            assert published['chat_history_meta'][0]['workflow_completed'] is True
    finally:
        release_model.set()
        release_tool.set()
        release_final_decision.set()
        if test_celery:
            test_celery.close()
        old_app.set_default()
        old_app.set_current()
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        if broker:
            broker.close()
        if temporary:
            temporary.cleanup()
