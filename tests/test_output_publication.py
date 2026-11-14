"""Final-file selection is durable, explicit and atomic with the saved answer."""
import hashlib
import json
import uuid

from backend.database import db
from backend.models import AgentArtifact, AgentStep, ConversationTurn
from backend.util import workspace_service as service, workspace_store
from tests.test_agent_controller import agent, choose, last_result, operation, tick
from tests.test_document_preparation import app


def saved_workbook(row, app, tmp_path, *, sequence, body):
    """Create a successful saved execution, not a model-supplied path stub."""
    root = tmp_path / 'generated'
    root.mkdir(exist_ok=True)
    app.config['AGENT_PYTHON_STORAGE_DIR'] = str(root)
    file_id, execution_id = str(uuid.uuid4()), str(uuid.uuid4())
    key = f'{row.id}/{file_id}/payroll.xlsx'
    path = root / key
    path.parent.mkdir(parents=True)
    path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    now = service.now()
    file = {'id': file_id, 'filename': 'payroll.xlsx', 'file_size': len(body), 'sha256': digest}
    call_id = f'call-{file_id}'
    arguments = json.dumps({'code': 'result = {}', 'file_ids': [], 'artifact_ids': []})
    decision = AgentStep(run_id=row.id, sequence=sequence * 2 - 1, kind='model',
        name='choose_next_action', arguments_json='{}', state='ready',
        result_json=json.dumps({'output': [{'type': 'function_call', 'name': 'python_run',
                                           'call_id': call_id, 'arguments': arguments}]}),
        created_at=now, updated_at=now)
    db.session.add(decision)
    step = AgentStep(run_id=row.id, sequence=sequence * 2, kind='tool', name='python_run',
                     call_id=call_id, arguments_json=arguments, state='ready',
                     result_json=json.dumps({'artifact_id': execution_id, 'kind': 'python_execution',
                                             'error': False, 'files': [file]}),
                     created_at=now, updated_at=now)
    db.session.add(step)
    db.session.flush()
    db.session.add(AgentArtifact(id=file_id, run_id=row.id, step_id=step.id,
        kind='derived_file', content_json=json.dumps({'filename': 'payroll.xlsx',
            'file_size': len(body), 'sha256': digest, 'storage_key': key}), created_at=now))
    db.session.add(AgentArtifact(id=execution_id, run_id=row.id, step_id=step.id,
        kind='python_execution', content_json=json.dumps({'status': 'success', 'error': False,
                                                        'files': [file]}), created_at=now))
    db.session.commit()
    return {'file_id': file_id, 'execution_id': execution_id,
            'filename': 'payroll.xlsx', 'file_size': len(body)}


def finish(row, monkeypatch, ids):
    choose(monkeypatch, 'complete_task',
           {'response': 'The recalculated workbook is ready.', 'candidate_id': None,
            'output_file_ids': ids})
    assert tick(row) == {'continued': True}, row.error_message
    return tick(row)


def test_exact_selected_file_survives_publication_reload_and_recovery(agent, app, monkeypatch, tmp_path):
    first = saved_workbook(agent, app, tmp_path, sequence=1, body=b'first uncached version')
    final = saved_workbook(agent, app, tmp_path, sequence=2, body=b'final recalculated version')
    newest = saved_workbook(agent, app, tmp_path, sequence=3, body=b'unselected later experiment')
    assert finish(agent, monkeypatch, [final['file_id'], final['file_id']]) == {'completed': True}

    turn = workspace_store.turn_for_workflow(agent.id)
    expected = [final]
    assert json.loads(turn.metadata_json)['output_files'] == expected
    assert json.loads(agent.result_json)['output_files'] == expected
    assert workspace_store.load(agent.chat_thread_id)['chat_history_meta'][0]['output_files'] == expected
    assert first['file_id'] != final['file_id'] != newest['file_id']
    assert {'storage_key', 'path', 'url'}.isdisjoint(expected[0])

    # The public API downloads precisely that file and refuses an execution/file
    # mismatch, despite all three user-visible filenames being identical.
    client = app.test_client()
    url = f'/api/workspace/runs/{agent.id}/artifacts/{final["execution_id"]}/files/{final["file_id"]}'
    download = client.get(url)
    assert download.status_code == 200 and download.data == b'final recalculated version'
    download.close()
    wrong = f'/api/workspace/runs/{agent.id}/artifacts/{first["execution_id"]}/files/{final["file_id"]}'
    assert client.get(wrong).status_code == 404

    # Recovery must use the canonical selection, even if transient bookkeeping
    # disappeared. It must not choose the last same-named artifact instead.
    agent.status, agent.active_chat_thread_id, agent.result_json = 'running', agent.chat_thread_id, None
    db.session.commit()
    run_id = agent.id
    db.session.remove()
    from backend.models import WorkspaceRun
    row = db.session.get(WorkspaceRun, run_id)
    assert service.complete_published_answer(row)
    assert row.status == 'completed'
    assert json.loads(row.result_json)['output_files'] == expected
    assert ConversationTurn.query.filter_by(run_id=row.id).count() == 1


def test_invalid_output_selection_cannot_publish_a_partial_answer(agent, app, monkeypatch, tmp_path):
    saved_workbook(agent, app, tmp_path, sequence=1, body=b'valid file retained')
    before = workspace_store.load(agent.chat_thread_id)
    assert finish(agent, monkeypatch, ['not-a-saved-file']) == {'continued': True}
    assert last_result(agent)['error'] is True
    assert workspace_store.turn_for_workflow(agent.id) is None
    assert workspace_store.load(agent.chat_thread_id)['draft'] == before['draft']
    assert agent.status == 'running'


def test_completion_staging_failure_rolls_back_file_selection_and_turn(agent, app, monkeypatch, tmp_path):
    final = saved_workbook(agent, app, tmp_path, sequence=1, body=b'correct workbook')

    def fail(*args, **kwargs):
        raise ValueError('Injected completion failure')

    monkeypatch.setattr(service, 'stage_publication', fail)
    assert finish(agent, monkeypatch, [final['file_id']]) == {'continued': True}
    assert last_result(agent)['error'] is True
    assert workspace_store.turn_for_workflow(agent.id) is None
    assert not workspace_store.load(agent.chat_thread_id)['chat_history']
    assert db.session.get(AgentArtifact, final['file_id']) is not None


def test_old_completion_without_file_selection_remains_replayable(agent, monkeypatch):
    assert operation(agent, monkeypatch, 'complete_task',
                     {'response': 'Historical answer without generated files.',
                      'candidate_id': None}) == {'completed': True}
    turn = workspace_store.turn_for_workflow(agent.id)
    assert json.loads(turn.metadata_json).get('output_files', []) == []
    assert json.loads(agent.result_json).get('output_files', []) == []
