"""Explicit final file identity, successful execution linkage and availability."""
import hashlib
import json
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.ai import agent_tools
from backend.database import db
from backend.models import AgentArtifact, AgentStep, WorkspaceRun
from backend.util.output_files import resolve_output_files
from tests.test_document_preparation import app


@pytest.fixture
def files(app, tmp_path):
    app.config['AGENT_PYTHON_STORAGE_DIR'] = str(tmp_path / 'outputs')
    now = datetime.utcnow()
    rows = []
    for uid in ('current', 'other'):
        row = WorkspaceRun(id=uid, user_id=1, chat_thread_id=1, data_json='{}', draft_hash='test',
                           status='running', last_progress_at=now, created_at=now,
                           updated_at=now, deadline=now + timedelta(hours=1))
        db.session.add(row)
        rows.append(row)
    db.session.commit()

    def create(uid, content=b'calculated values', *, row=None):
        row = row or rows[0]
        root = tmp_path / 'outputs'
        root.mkdir(exist_ok=True)
        path = root / (uid + '.xlsx')
        path.write_bytes(content)
        value = {'filename': 'payroll.xlsx', 'file_size': len(content),
                 'sha256': hashlib.sha256(content).hexdigest(), 'storage_key': path.name}
        sequence = AgentStep.query.filter_by(run_id=row.id).count() + 1
        step = AgentStep(id='step-' + uid, run_id=row.id, sequence=sequence, kind='tool',
                         name='python_run', state='ready', created_at=now, updated_at=now)
        artifact = AgentArtifact(id=uid, run_id=row.id, step_id=step.id, kind='derived_file',
                                 content_json=json.dumps(value), created_at=now)
        result = {'status': 'success', 'error': False, 'files': [{'id': uid,
                  **{key: value[key] for key in ('filename', 'file_size', 'sha256')}}]}
        execution = AgentArtifact(id='execution-' + uid, run_id=row.id, step_id=step.id,
                                  kind='python_execution', content_json=json.dumps(result), created_at=now)
        db.session.add_all([step, artifact, execution])
        db.session.commit()
        return step, artifact, execution, path

    return rows, create


def test_exact_selection_not_filename_or_latest_deduplicates_and_hides_paths(files):
    rows, create = files
    create('recalculated', b'final verified values')
    create('other-version', b'same filename, different values')
    selected = resolve_output_files(rows[0], ['recalculated', 'recalculated'])
    assert selected == [{'file_id': 'recalculated', 'execution_id': 'execution-recalculated',
                         'filename': 'payroll.xlsx', 'file_size': 21}]
    assert 'storage_key' not in selected[0] and 'sha256' not in selected[0]
    assert resolve_output_files(rows[0], []) == []


@pytest.mark.parametrize('case', ['unknown', 'foreign', 'wrong_kind'])
def test_rejects_missing_foreign_and_wrong_kind_artifacts(files, case):
    rows, create = files
    if case == 'foreign':
        create('selected', row=rows[1])
    elif case == 'wrong_kind':
        _, artifact, _, _ = create('selected')
        artifact.kind = 'analysis'
        db.session.commit()
    with pytest.raises(ValueError, match='generated file from this request'):
        resolve_output_files(rows[0], ['selected'])


@pytest.mark.parametrize('state', ['queued', 'running', 'failed'])
def test_rejects_unfinished_or_failed_steps(files, state):
    rows, create = files
    step, _, _, _ = create('selected')
    step.state = state
    db.session.commit()
    with pytest.raises(ValueError, match='completed Python execution'):
        resolve_output_files(rows[0], ['selected'])


@pytest.mark.parametrize('case', ['error', 'status', 'unlinked', 'mismatch', 'execution_missing'])
def test_rejects_bad_execution_links_or_failed_results(files, case):
    rows, create = files
    _, _, execution, _ = create('selected')
    value = json.loads(execution.content_json)
    if case == 'error': value['error'] = True
    if case == 'status': value['status'] = 'execution_error'
    if case == 'unlinked': value['files'] = []
    if case == 'mismatch': value['files'][0]['sha256'] = 'wrong'
    if case == 'execution_missing': db.session.delete(execution)
    else: execution.content_json = json.dumps(value)
    db.session.commit()
    with pytest.raises(ValueError):
        resolve_output_files(rows[0], ['selected'])


@pytest.mark.parametrize('case', ['missing', 'changed', 'outside'])
def test_checks_actual_file_availability_and_integrity(files, tmp_path, case):
    rows, create = files
    _, artifact, _, path = create('selected')
    if case == 'missing': path.unlink()
    if case == 'changed': path.write_bytes(b'tampered values')
    if case == 'outside':
        outside = tmp_path / 'outside.xlsx'
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    with pytest.raises(ValueError):
        resolve_output_files(rows[0], ['selected'])


def test_new_complete_schema_requires_file_selection_but_old_calls_replay():
    definition = next(item for item in agent_tools.definitions() if item['name'] == 'complete_task')
    assert 'output_file_ids' in definition['parameters']['required']
    old = {'response': 'Answer', 'candidate_id': None}
    assert agent_tools.arguments('complete_task', old).output_file_ids == []
    assert 'output_file_ids' not in old
    with pytest.raises(ValidationError):
        agent_tools.Complete.model_validate(old)
    with pytest.raises(ValidationError):
        agent_tools.arguments('complete_task', {**old, 'output_file_ids': [123]})
