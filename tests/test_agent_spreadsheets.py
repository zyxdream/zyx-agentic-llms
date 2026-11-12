"""Spreadsheet selection context and Python artifacts through real SQL steps."""
import io
import json
import os
import shutil
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from flask import current_app
import pytest
from werkzeug.datastructures import FileStorage

from backend import agent_tasks as tasks, document_preparation_tasks as preparation_tasks, case_assistant_routes as routes
from backend.ai import agent_tools, base
from backend.database import db
from backend.models import AgentArtifact, DocumentPreparation, UploadedFiles
from backend.util import attachment_parser, document_preparation as prep, python_execution
from backend.util import case_assistant_workflow as service
from backend.util.agent_catalogue import documents
from backend.util.agent_python import file_hash, storage_root
from tests.test_agent_controller import agent, call, last_result, operation, state, tick
from tests.test_document_preparation import app, run


@pytest.fixture(autouse=True)
def isolated_python_storage(app, tmp_path):
    app.config['AGENT_PYTHON_STORAGE_DIR'] = str(tmp_path / 'python-artifacts')


def attempt_files(attempt):
    return {str(path.relative_to(attempt)) for path in attempt.rglob('*')
            if path.is_file() or path.is_symlink()}


def assert_retained_attempt(attempt, expected):
    assert attempt_files(attempt) == expected
    assert {str(path.relative_to(attempt)) for path in attempt.rglob('*') if path.is_dir()} == (
        {'output'} if any(name.startswith('output/') for name in expected) else set())


def add_input(row, monkeypatch, filename, data, *, relative_path=None, scope='chat'):
    monkeypatch.setattr(preparation_tasks, 'parse_attachment', attachment_parser.parse_attachment)
    group = prep.admit_group(1, scope, 'draft-1' if scope == 'chat' else 1, filename + str(relative_path), [{
        'client_upload_id': filename + str(relative_path), 'filename': filename,
        'relative_path': relative_path, 'file_size': len(data)}])
    entry = prep.stage_transfer(prep.owned_group(group['group_id'], 1),
        group['documents'][0]['clientUploadId'], FileStorage(stream=io.BytesIO(data), filename=filename))
    assert run(entry)['ok']
    original = json.loads(row.data_json)
    ids = [item['file_id'] for item in original['document_snapshot']['files']] + [entry['document_id']]
    original['document_snapshot'] = prep.build_snapshot(1, 'draft-1', {
        'promptAttachments': [{'file_id': uid} for uid in ids]})
    row.data_json = json.dumps(original)
    db.session.commit()
    return entry['document_id']


def observed_run(tmp_path, result=None, *, error=False, output=False):
    attempts = []
    def execute(code, inputs, artifacts, **kwargs):
        kwargs['check_execution']()
        attempt = Path(kwargs['attempt_dir'])
        attempts.append(attempt)
        for name in ('inputs', 'job', 'scratch', 'extracted', 'output'):
            (attempt / name).mkdir(exist_ok=True)
        for index, path in enumerate(inputs.values()):
            staged = attempt / 'inputs' / f'file-{index}{Path(path).suffix}'
            shutil.copyfile(path, staged)
            staged.chmod(0o444)
        for index, value in enumerate(artifacts.values()):
            if isinstance(value, dict) and 'file_path' in value:
                shutil.copyfile(value['file_path'], attempt / 'inputs' / f'artifact-{index}.csv')
        (attempt / 'job' / 'script.py').write_text(code)
        (attempt / 'job' / 'request.json').write_text('{}')
        (attempt / 'extracted' / 'working-text.txt').write_text('temporary extraction')
        (attempt / 'scratch' / 'working').mkdir()
        (attempt / 'scratch' / 'working' / 'intermediate.csv').write_text('temporary data')
        outside = next(iter(inputs.values()), None)
        if outside:
            (attempt / 'scratch' / 'source-directory').symlink_to(Path(outside).parent, target_is_directory=True)
        (attempt / 'output' / '_execution.json').write_text('{}')
        (attempt / 'output' / 'rejected.py').write_text('not a published output')
        (attempt / 'output' / 'nested').mkdir()
        (attempt / 'output' / 'nested' / 'partial.csv').write_text('unpublished')
        (attempt / 'stdout.log').write_text('inspected original rows')
        (attempt / 'stderr.log').write_text('KeyError: Amount' if error else '')
        files = []
        if output:
            directory = attempt / 'output'
            path = directory / 'reconciled.csv'; path.write_text('invoice,balance\nINV1,75\n')
            files = [{'name': path.name, 'path': str(path), 'size': path.stat().st_size, 'sha256': file_hash(path)}]
        return {'status': 'execution_error' if error else 'success', 'error': error,
                'stdout': 'inspected original rows', 'stderr': 'KeyError: Amount' if error else '',
                'traceback': 'KeyError: Amount' if error else None, 'result': result,
                'message': 'Python raised an error.' if error else 'Python analysis completed.',
                'output_files': files,
                'log_paths': {name: str(attempt / f'{name}.log') for name in ('stdout', 'stderr')},
                'runtime_versions': {'python': 'test'}}
    execute.attempts = attempts
    return execute


def test_first_call_and_rediscovery_share_all_file_cards(agent, monkeypatch):
    first = add_input(agent, monkeypatch, 'ledger.csv', b'Invoice,Amount\nINV1,100\n', relative_path='Invoices/ledger.csv')
    second = add_input(agent, monkeypatch, 'ledger.csv', b'Invoice,Paid\nINV1,25\n', relative_path='Payments/ledger.csv', scope='matter')
    captured = []
    def model(request):
        captured.append(request)
        return call('list_documents', {})
    monkeypatch.setattr(base, 'responses_completion', model)
    assert tick(agent) == {'continued': True}
    initial = json.loads(captured[0]['input'][1]['content'])
    cards = {item['file_id']: item for item in initial['documents']}
    assert set(cards) == {item['file_id'] for item in json.loads(agent.data_json)['document_snapshot']['files']}
    assert cards[first]['relative_path'] == 'Invoices/ledger.csv'
    assert cards[second]['relative_path'] == 'Payments/ledger.csv'
    assert cards[first]['current'] and cards[second]['scope'] == 'matter'
    assert cards[first]['metadata']['spreadsheet']['sheets'][0]['samples']
    assert cards[first]['metadata']['summary_exhaustive'] is False
    assert 'prepared_artifact_id' in cards[first] and 'artifact_id' not in cards[first]
    assert tick(agent) == {'continued': True}
    assert last_result(agent)['documents'] == initial['documents'] == documents(agent)
    assert 'python_run' in {tool['name'] for tool in captured[0]['tools']}


def test_python_error_correction_saved_file_reuse_and_download(agent, monkeypatch, tmp_path):
    uid = add_input(agent, monkeypatch, 'invoices.csv', b'Invoice,Amount\nINV1,100\n')
    source = Path(db.session.get(DocumentPreparation, uid).source_key)
    original_hash = file_hash(source)
    before = state(agent)
    failed_run = observed_run(tmp_path, error=True)
    monkeypatch.setattr(python_execution, 'run_python', failed_run)
    operation(agent, monkeypatch, 'python_run', {'code': "result = df['Amount']", 'file_ids': [uid], 'artifact_ids': []})
    failed = last_result(agent)
    assert failed['error'] and failed['traceback'] == 'KeyError: Amount'
    saved_error = db.session.get(AgentArtifact, failed['artifact_id'])
    assert json.loads(saved_error.content_json)['code'] == "result = df['Amount']"
    assert_retained_attempt(failed_run.attempts[0], {'stdout.log', 'stderr.log'})
    assert agent.status == 'running' and state(agent) == before

    successful_run = observed_run(tmp_path, {'balance': 75}, output=True)
    monkeypatch.setattr(python_execution, 'run_python', successful_run)
    operation(agent, monkeypatch, 'python_run', {'code': "result = {'balance': 75}", 'file_ids': [uid], 'artifact_ids': []})
    success = last_result(agent)
    assert not success['error'] and success['result'] == {'balance': 75}
    file_id = success['files'][0]['id']
    kept = successful_run.attempts[0]
    assert_retained_attempt(kept, {'stdout.log', 'stderr.log', 'output/reconciled.csv'})
    calls = []
    def reuse(code, inputs, artifacts, **kwargs):
        calls.append((inputs, artifacts))
        assert artifacts[success['artifact_id']]['result'] == {'balance': 75}
        assert Path(artifacts[file_id]['file_path']).read_text().endswith('INV1,75\n')
        return observed_run(tmp_path, {'reused': True})(code, inputs, artifacts, **kwargs)
    monkeypatch.setattr(python_execution, 'run_python', reuse)
    operation(agent, monkeypatch, 'python_run', {'code': "result = {'reused': True}", 'file_ids': [],
        'artifact_ids': [success['artifact_id'], file_id]})
    assert calls and last_result(agent)['result']['reused']
    assert last_result(agent)['references'][0]['file_id'] == uid
    assert_retained_attempt(kept, {'stdout.log', 'stderr.log', 'output/reconciled.csv'})
    db.session.expire_all()  # Reload saved metadata, without depending on live objects.
    client = current_app.test_client()
    url = f'/api/everything/workflows/{agent.id}/artifacts/{success["artifact_id"]}/files/{file_id}'
    response = client.get(url)
    assert response.status_code == 200 and response.data == b'invoice,balance\nINV1,75\n'
    assert 'reconciled.csv' in response.headers['Content-Disposition']
    assert client.get(url.replace(success['artifact_id'], failed['artifact_id'])).status_code == 404
    assert file_hash(source) == original_hash and state(agent) == before
    monkeypatch.setattr(routes, 'current_user', SimpleNamespace(id=2))
    assert client.get(url).status_code == 404


@pytest.mark.parametrize('status', ['execution_error', 'timeout', 'output_limit', 'runtime_unavailable'])
def test_failed_python_attempt_keeps_only_saved_diagnostics(agent, monkeypatch, tmp_path, status):
    uid = add_input(agent, monkeypatch, 'invoices.csv', b'Invoice,Amount\nINV1,100\n')
    original = Path(db.session.get(DocumentPreparation, uid).source_key)
    digest = file_hash(original)
    runner = observed_run(tmp_path, error=True, output=True)
    def failed(code, inputs, artifacts, **kwargs):
        result = runner(code, inputs, artifacts, **kwargs)
        result.update(status=status, output_files=[])
        return result
    monkeypatch.setattr(python_execution, 'run_python', failed)
    operation(agent, monkeypatch, 'python_run', {'code': 'failing analysis', 'file_ids': [uid], 'artifact_ids': []})
    answer = last_result(agent)
    assert answer['error'] and answer['status'] == status and answer['files'] == []
    db.session.expire_all()
    saved = json.loads(db.session.get(AgentArtifact, answer['artifact_id']).content_json)
    assert saved['code'] == 'failing analysis' and saved['status'] == status
    assert saved['traceback'] == 'KeyError: Amount'
    assert saved['stdout'] == 'inspected original rows' and saved['stderr'] == 'KeyError: Amount'
    assert saved['runtime_versions'] == {'python': 'test'}
    assert_retained_attempt(runner.attempts[0], {'stdout.log', 'stderr.log'})
    assert {name: (storage_root() / key).read_text() for name, key in saved['logs'].items()} == {
        'stdout': 'inspected original rows', 'stderr': 'KeyError: Amount'}
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': answer['artifact_id'], 'ranges': [], 'start': 0, 'end': None})
    assert 'KeyError: Amount' in last_result(agent)['text']
    assert file_hash(original) == digest


@pytest.mark.parametrize('failure', ['cancelled', 'runtime_failure', 'save_failure'])
def test_unsaved_python_attempt_is_removed_without_touching_prior_files(agent, monkeypatch, tmp_path, failure):
    uid = add_input(agent, monkeypatch, 'invoices.csv', b'Invoice,Amount\nINV1,100\n')
    original = Path(db.session.get(DocumentPreparation, uid).source_key)
    digest = file_hash(original)
    prior_runner = observed_run(tmp_path, {'balance': 75}, output=True)
    monkeypatch.setattr(python_execution, 'run_python', prior_runner)
    operation(agent, monkeypatch, 'python_run', {'code': 'previous analysis', 'file_ids': [uid], 'artifact_ids': []})
    prior = last_result(agent)
    prior_id = prior['files'][0]['id']
    prior_file = storage_root() / json.loads(db.session.get(AgentArtifact, prior_id).content_json)['storage_key']
    prior_hash = file_hash(prior_file)
    count = AgentArtifact.query.count()
    runner = observed_run(tmp_path, {'balance': 75}, output=True)

    def interrupted(code, inputs, artifacts, **kwargs):
        result = runner(code, inputs, artifacts, **kwargs)
        if failure == 'cancelled':
            raise service.WorkflowStopped('Assignment stopped during Python')
        if failure == 'runtime_failure':
            raise RuntimeError('Runner failed unexpectedly')
        return result

    monkeypatch.setattr(python_execution, 'run_python', interrupted)
    if failure == 'save_failure':
        original_save = agent_tools.Operations.save
        def save(self, kind, value, **kwargs):
            if kind == 'python_execution':
                raise RuntimeError('Execution artifact staging failed')
            return original_save(self, kind, value, **kwargs)
        monkeypatch.setattr(agent_tools.Operations, 'save', save)
    args = {'code': 'new analysis', 'file_ids': [uid], 'artifact_ids': [prior_id]}
    if failure == 'cancelled':
        assert operation(agent, monkeypatch, 'python_run', args) == {'stopped': True}
    else:
        with pytest.raises(RuntimeError, match='failed'):
            operation(agent, monkeypatch, 'python_run', args)
    assert not runner.attempts[0].exists()
    assert file_hash(original) == digest and file_hash(prior_file) == prior_hash
    assert_retained_attempt(prior_runner.attempts[0], {'stdout.log', 'stderr.log', 'output/reconciled.csv'})
    assert AgentArtifact.query.count() == count


@pytest.mark.parametrize('interrupted', [False, True])
def test_cleanup_failure_is_logged_without_replacing_execution_outcome(agent, monkeypatch, tmp_path, caplog, interrupted):
    from backend.util import agent_python
    runner = observed_run(tmp_path, {'total': 42})
    def execute(code, inputs, artifacts, **kwargs):
        result = runner(code, inputs, artifacts, **kwargs)
        if interrupted:
            raise service.WorkflowStopped('Original Python interruption')
        return result
    original_cleanup = agent_python._cleanup_attempt
    def fail_cleanup(*args):
        raise OSError('Temporary storage cleanup unavailable')
    monkeypatch.setattr(python_execution, 'run_python', execute)
    monkeypatch.setattr(agent_python, '_cleanup_attempt', fail_cleanup)
    try:
        outcome = operation(agent, monkeypatch, 'python_run', {
            'code': 'result = 42', 'file_ids': [], 'artifact_ids': []})
        if interrupted:
            assert outcome == {'stopped': True}
            assert agent.error_message == 'Original Python interruption'
            assert AgentArtifact.query.count() == 0
        else:
            assert outcome == {'continued': True}
            result = last_result(agent)
            assert result['error'] is False and result['result'] == {'total': 42}
            assert db.session.get(AgentArtifact, result['artifact_id']) is not None
        assert 'Python attempt cleanup failed' in caplog.text
        assert 'Temporary storage cleanup unavailable' in caplog.text
    finally:
        original_cleanup(runner.attempts[0], set())


def test_python_rejects_unselected_sources_and_foreign_artifacts_before_execution(agent, monkeypatch):
    monkeypatch.setattr(python_execution, 'run_python', lambda *a, **kw: pytest.fail('Invalid input reached runner'))
    operation(agent, monkeypatch, 'python_run', {'code': 'print(1)', 'file_ids': ['foreign-file'], 'artifact_ids': []})
    assert last_result(agent)['error']
    prepared = json.loads(agent.data_json)['document_snapshot']['files'][0]['artifact_id']
    operation(agent, monkeypatch, 'python_run', {'code': 'print(1)', 'file_ids': [], 'artifact_ids': [prepared]})
    assert last_result(agent)['error']


def test_active_spreadsheet_bytes_survive_preparation_cleanup(agent, monkeypatch):
    uid = add_input(agent, monkeypatch, 'payments.csv', b'Invoice,Paid\nINV1,25\n')
    doc = db.session.get(DocumentPreparation, uid)
    path = Path(doc.source_key)
    doc.updated_at = prep.now() - timedelta(days=10)
    db.session.commit()
    prep.reconcile()
    assert path.is_file() and db.session.get(DocumentPreparation, uid).source_key


def test_changed_original_is_an_observation_and_never_reaches_runner(agent, monkeypatch):
    uid = add_input(agent, monkeypatch, 'payments.csv', b'Invoice,Paid\nINV1,25\n')
    Path(db.session.get(DocumentPreparation, uid).source_key).write_bytes(b'changed')
    monkeypatch.setattr(python_execution, 'run_python', lambda *a, **kw: pytest.fail('Changed file reached runner'))
    operation(agent, monkeypatch, 'python_run', {'code': 'print(1)', 'file_ids': [uid], 'artifact_ids': []})
    assert last_result(agent)['error'] and 'content changed' in last_result(agent)['message']


def test_moved_canonical_source_cannot_reach_python(agent, monkeypatch):
    from backend.util.agent_python import execute
    uid = add_input(agent, monkeypatch, 'payments.csv', b'Invoice,Paid\nINV1,25\n')
    operation(agent, monkeypatch, 'list_documents', {})
    uploaded = UploadedFiles.query.filter_by(unique_id=uid).one()
    uploaded.draft_id = 2
    db.session.commit()
    monkeypatch.setattr(python_execution, 'run_python', lambda *a, **kw: pytest.fail('Moved source reached runner'))
    step = tasks.steps(agent).order_by(None).first()
    ops = agent_tools.Operations(agent, step, None)
    with pytest.raises(prep.PreparationError, match='moved outside'):
        execute(ops, agent_tools.arguments('python_run', {'code': 'print(1)', 'file_ids': [uid], 'artifact_ids': []}))


def test_full_execution_log_remains_readable_after_tool_preview(agent, monkeypatch, tmp_path):
    body = 'output row\n' * 3000 + 'FINAL LOG MARKER'
    runner = observed_run(tmp_path)
    def noisy(code, inputs, artifacts, **kwargs):
        answer = runner(code, inputs, artifacts, **kwargs)
        path = Path(kwargs['attempt_dir']) / 'stdout.log'; path.write_text(body)
        answer.update(stdout=body[:16000], stdout_truncated=True, log_paths={'stdout': str(path)})
        return answer
    monkeypatch.setattr(python_execution, 'run_python', noisy)
    operation(agent, monkeypatch, 'python_run', {'code': "print('many rows')", 'file_ids': [], 'artifact_ids': []})
    answer = last_result(agent)
    assert answer['stdout_truncated'] and 'FINAL LOG MARKER' not in answer['stdout']
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': answer['artifact_id'], 'ranges': [], 'start': 0, 'end': None})
    assert 'FINAL LOG MARKER' in last_result(agent)['text']
    assert_retained_attempt(runner.attempts[0], {'stdout.log'})
    saved = json.loads(db.session.get(AgentArtifact, answer['artifact_id']).content_json)
    assert (storage_root() / saved['logs']['stdout']).read_text().endswith('FINAL LOG MARKER')


def test_last_sheet_title_rows_and_source_locations_reach_agent(agent, monkeypatch):
    from openpyxl import Workbook
    from backend.util import source_context
    workbook = Workbook()
    workbook.active.title = 'Instructions'
    workbook.active['A1'] = 'This tab contains no payment transactions.'
    data = workbook.create_sheet('Payment detail')
    data['A1'] = 'Payment records'
    data.append([])
    data.append(['Invoice', 'Paid'])
    data.append(['0001', 25])
    stream = io.BytesIO(); workbook.save(stream); workbook.close()
    uid = add_input(agent, monkeypatch, 'records.xlsx', stream.getvalue())
    operation(agent, monkeypatch, 'list_documents', {})
    card = next(item for item in last_result(agent)['documents'] if item['file_id'] == uid)
    profile = card['metadata']['spreadsheet']
    assert [sheet['name'] for sheet in profile['sheets']] == ['Instructions', 'Payment detail']
    assert profile['sheets'][-1]['header_candidates'][0]['row'] == 3
    doc = source_context.document(json.loads(agent.data_json)['document_snapshot'], uid)
    rendered = source_context.format_document(doc, [{'start': 0, 'end': len(doc['manifest']['text'])}], 'records.xlsx')
    assert 'Payment detail!A4:B4' in rendered and '0001' in rendered


@pytest.mark.skipif(os.environ.get('LEXINTEL_PYTHON_LIVE') != '1', reason='Set LEXINTEL_PYTHON_LIVE=1 for Docker integration.')
def test_real_python_agent_uses_full_data_saves_and_downloads_new_file(agent, monkeypatch):
    content = ('Invoice,Amount\n' + ''.join(f'INV{i:03},{i}\n' for i in range(1, 101))).encode()
    uid = add_input(agent, monkeypatch, 'invoices.csv', content)
    original = Path(db.session.get(DocumentPreparation, uid).source_key)
    digest = file_hash(original)
    operation(agent, monkeypatch, 'python_run', {
        'code': f"import pandas as pd\n(scratch_dir / 'intermediate.txt').write_text('temporary')\n(output_dir / 'partial.csv').write_text('unfinished')\ndata = pd.read_csv(input_paths[{uid!r}])\nresult = int(data['Missing'].sum())",
        'file_ids': [uid], 'artifact_ids': []})
    failed = last_result(agent)
    assert failed['error'] and 'KeyError' in failed['traceback']
    failure_record = json.loads(db.session.get(AgentArtifact, failed['artifact_id']).content_json)
    failed_attempt = (storage_root() / failure_record['logs']['stdout']).parent
    assert_retained_attempt(failed_attempt, {'stdout.log', 'stderr.log'})
    assert 'KeyError' in (storage_root() / failure_record['logs']['stderr']).read_text()
    operation(agent, monkeypatch, 'python_run', {
        'code': f"import pandas as pd\n(scratch_dir / 'working').mkdir()\n(scratch_dir / 'working' / 'intermediate.txt').write_text('temporary')\n(output_dir / 'rejected.tmp').write_text('not a published file')\ndata = pd.read_csv(input_paths[{uid!r}])\nresult = {{'total': int(data['Amount'].sum()), 'rows': len(data)}}\ndata.to_excel(output_dir / 'new-data.xlsx', index=False)",
        'file_ids': [uid], 'artifact_ids': []})
    answer = last_result(agent)
    assert not answer['error'], answer
    assert answer['result'] == {'total': 5050, 'rows': 100}
    file_id = answer['files'][0]['id']
    saved_file = storage_root() / json.loads(db.session.get(AgentArtifact, file_id).content_json)['storage_key']
    success_attempt = saved_file.parent.parent
    assert_retained_attempt(success_attempt, {'stdout.log', 'stderr.log', 'output/new-data.xlsx'})
    operation(agent, monkeypatch, 'python_run', {
        'code': f"import pandas as pd\ndata = pd.read_excel(input_artifacts[{file_id!r}])\nresult = {{'rows': len(data), 'total': int(data['Amount'].sum())}}",
        'file_ids': [], 'artifact_ids': [file_id]})
    assert last_result(agent)['result'] == {'rows': 100, 'total': 5050}
    assert_retained_attempt(success_attempt, {'stdout.log', 'stderr.log', 'output/new-data.xlsx'})
    db.session.expire_all()
    response = current_app.test_client().get(
        f'/api/everything/workflows/{agent.id}/artifacts/{answer["artifact_id"]}/files/{file_id}')
    assert response.status_code == 200 and response.data.startswith(b'PK')
    assert file_hash(original) == digest
