"""Execution observations, immutable inputs, and optional real container checks."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from backend.util import python_execution as runtime


def fake_container(monkeypatch, *, metadata=None, returncode=0, output_files=None, running=False, stderr=''):
    calls = []
    stderr_text = stderr

    class Process:
        def __init__(self, command, stdout, stderr):
            self.returncode = None if running else returncode
            self.command = command
            mounts = [command[index + 1] for index, value in enumerate(command) if value == '--mount']
            self.mounts = {mount.split('dst=', 1)[1].split(',', 1)[0]:
                           Path(mount.split('src=', 1)[1].split(',', 1)[0]) for mount in mounts}
            self.request = json.loads((self.mounts['/job'] / 'request.json').read_text())
            stdout.write(b'analysis output\n')
            if metadata is not None:
                (self.mounts['/output'] / '_execution.json').write_text(json.dumps(metadata))
            for name, value in (output_files or {}).items():
                (self.mounts['/output'] / name).write_text(value)
            calls.append(self)

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = returncode
            return self.returncode

        def kill(self):
            self.returncode = -9

    def cleanup(command, **kwargs):
        if calls:
            calls[-1].returncode = returncode

    def popen(command, stdout, stderr):
        process = Process(command, stdout, stderr)
        if isinstance(stderr, str):
            raise AssertionError('Expected a log stream')
        stderr.write(stderr_text.encode())
        return process

    monkeypatch.setattr(runtime.subprocess, 'Popen', popen)
    monkeypatch.setattr(runtime.subprocess, 'run', cleanup)
    return calls


SUCCESS = {'status': 'success', 'result': {'total': 42}, 'traceback': None,
           'runtime_versions': {'python': '3.11'}}


def test_inputs_are_copied_read_only_and_results_are_saved(tmp_path, monkeypatch):
    source = tmp_path / 'payments.csv'
    source.write_text('amount\n20\n22\n')
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    calls = fake_container(monkeypatch, metadata=SUCCESS, output_files={'totals.csv': 'total\n42\n'})
    result = runtime.run_python('result = {"total": 42}', {'file-1': source},
                                {'analysis-1': {'text': 'Prior work'}}, attempt_dir=tmp_path / 'attempt')
    assert result['error'] is False
    assert result['result'] == {'total': 42}
    assert result['stdout'] == 'analysis output\n'
    assert result['output_files'][0]['name'] == 'totals.csv'
    assert Path(result['log_paths']['stdout']).read_text() == 'analysis output\n'
    assert calls[0].request['input_paths'] == {'file-1': '/inputs/file-0.csv'}
    assert calls[0].request['input_artifacts'] == {'analysis-1': {'text': 'Prior work'}}
    staged = calls[0].mounts['/inputs'] / 'file-0.csv'
    assert staged.stat().st_mode & 0o222 == 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    args = calls[0].command
    assert args[args.index('--network') + 1] == 'none'
    assert '--read-only' in args
    assert args[args.index('--user') + 1] == '65532:65532'


def test_python_exception_is_a_recoverable_observation(tmp_path, monkeypatch):
    traceback = 'Traceback: KeyError: Amount'
    fake_container(monkeypatch, metadata={'status': 'execution_error', 'result': None,
                   'traceback': traceback, 'runtime_versions': {}}, returncode=1,
                   output_files={'partial.csv': 'incomplete'})
    result = runtime.run_python('raise KeyError("Amount")', {}, {}, attempt_dir=tmp_path / 'attempt')
    assert result['error'] is True
    assert result['status'] == 'execution_error'
    assert result['traceback'] == traceback
    assert result['output_files'] == []


def test_saved_file_artifacts_are_explicit_inputs(tmp_path, monkeypatch):
    saved = tmp_path / 'prior.csv'
    saved.write_text('total\n42')
    calls = fake_container(monkeypatch, metadata=SUCCESS)
    runtime.run_python('result = 42', {}, {'saved-file': {'file_path': str(saved)}}, attempt_dir=tmp_path / 'attempt')
    assert calls[0].request['input_artifacts']['saved-file'] == '/inputs/artifact-0.csv'
    assert (calls[0].mounts['/inputs'] / 'artifact-0.csv').read_text() == 'total\n42'


def test_cancellation_propagates_after_container_cleanup(tmp_path, monkeypatch):
    calls = fake_container(monkeypatch, running=True, returncode=137)
    checks = []

    class Cancelled(Exception):
        pass

    def check():
        checks.append(True)
        if len(checks) == 2:
            raise Cancelled('Assignment stopped')

    with pytest.raises(Cancelled, match='Assignment stopped'):
        runtime.run_python('while True: pass', {}, {}, check_execution=check, attempt_dir=tmp_path / 'attempt')
    assert calls[0].returncode == 137


def test_timeout_is_returned_and_process_stops(tmp_path, monkeypatch):
    calls = fake_container(monkeypatch, running=True, returncode=137)
    moments = iter([0, 121, 122])
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: next(moments))
    result = runtime.run_python('while True: pass', {}, {}, attempt_dir=tmp_path / 'attempt')
    assert result['status'] == 'timeout'
    assert result['error'] is True
    assert calls[0].returncode == 137


def test_missing_engine_does_not_fall_back_to_host_execution(tmp_path, monkeypatch):
    fake_container(monkeypatch, returncode=125, stderr='Cannot connect to Docker')
    result = runtime.run_python('result = 42', {}, {}, attempt_dir=tmp_path / 'attempt')
    assert result['status'] == 'runtime_unavailable'
    assert result['result'] is None


def test_output_collection_rejects_links_and_unrequested_file_types(tmp_path):
    target = tmp_path / 'data.csv'
    target.write_text('amount\n42')
    (tmp_path / 'link.csv').symlink_to(target)
    (tmp_path / 'program.py').write_text('print(42)')
    files, rejected = runtime._files(tmp_path)
    assert [file['name'] for file in files] == ['data.csv']
    assert rejected == ['link.csv', 'program.py']


def test_permission_cleanup_exposes_only_writable_attempt_roots(tmp_path, monkeypatch):
    attempt = tmp_path / 'attempt'
    for name in ('inputs', 'job', 'scratch', 'output'):
        (attempt / name).mkdir(parents=True)
    retained = attempt / 'output' / 'kept.csv'
    retained.write_text('total\n42\n')
    log = attempt / 'stdout.log'
    log.write_text('saved diagnostic')
    monkeypatch.setenv('AGENT_PYTHON_DOCKER', 'configured-docker')
    monkeypatch.setenv('AGENT_PYTHON_DOCKER_HOST', 'unix:///configured.sock')
    monkeypatch.setenv('AGENT_PYTHON_IMAGE', 'configured-analysis:1')
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stderr='')

    monkeypatch.setattr(runtime.subprocess, 'run', run)
    runtime._cleanup_container_files(attempt, [retained, log])
    command, options = calls[0]
    assert command[:3] == ['configured-docker', '--host', 'unix:///configured.sock']
    assert command[command.index('--user') + 1] == '65532:65532'
    assert command[command.index('--network') + 1] == 'none'
    assert command[command.index('--cap-drop') + 1] == 'ALL'
    assert '--read-only' in command and 'no-new-privileges' in command
    assert command[command.index('--entrypoint') + 1] == '/opt/python/bin/python'
    assert 'configured-analysis:1' in command
    mounts = [command[index + 1] for index, value in enumerate(command) if value == '--mount']
    assert mounts == [f'type=bind,src={attempt / name},dst=/cleanup/{name}' for name in ('scratch', 'output')]
    assert command[-2] == runtime._CLEANUP_PROGRAM
    assert json.loads(command[-1]) == {'/cleanup/scratch': [], '/cleanup/output': ['kept.csv']}
    assert options['timeout'] == 10
    name = command[command.index('--name') + 1]
    assert calls[-1][0] == ['configured-docker', '--host', 'unix:///configured.sock', 'rm', '--force', name]


def test_trusted_cleanup_preserves_outputs_and_does_not_follow_links(tmp_path):
    attempt = tmp_path / 'attempt'
    scratch, output = attempt / 'scratch', attempt / 'output'
    scratch.mkdir(parents=True)
    output.mkdir()
    private = scratch / 'private'
    private.mkdir()
    (private / 'temporary.txt').write_text('temporary')
    private.chmod(0)
    retained = output / 'retained.csv'
    retained.write_text('amount\n42\n')
    retained.chmod(0o400)
    retained_mode = retained.stat().st_mode
    (output / 'partial.csv').write_text('partial')
    (output / '_execution.json').write_text('{}')
    outside = tmp_path / 'originals'
    outside.mkdir()
    original = outside / 'source.csv'
    original.write_text('amount\n20\n22\n')
    original.chmod(0o444)
    original_mode = original.stat().st_mode
    (scratch / 'outside').symlink_to(outside, target_is_directory=True)
    (output / 'outside.csv').symlink_to(original)
    os.link(original, output / 'hardlink.csv')
    outside.chmod(0o500)
    outside_mode = outside.stat().st_mode
    manifest = {str(scratch): [], str(output): ['retained.csv']}

    result = subprocess.run([sys.executable, '-B', '-c', runtime._CLEANUP_PROGRAM, json.dumps(manifest)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert list(scratch.iterdir()) == []
    assert list(output.iterdir()) == [retained]
    assert retained.read_text() == 'amount\n42\n' and retained.stat().st_mode == retained_mode
    assert original.read_text() == 'amount\n20\n22\n' and original.stat().st_mode == original_mode
    assert outside.stat().st_mode == outside_mode


@pytest.mark.parametrize('failure,expected', [
    ('exit', RuntimeError), ('timeout', subprocess.TimeoutExpired), ('engine', OSError),
])
def test_permission_cleanup_always_removes_its_helper_container(tmp_path, monkeypatch, failure, expected):
    attempt = tmp_path / 'attempt'
    (attempt / 'scratch').mkdir(parents=True)
    monkeypatch.setattr(runtime, '_docker', lambda: ['docker'])
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'rm':
            return SimpleNamespace(returncode=0, stderr='')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        if failure == 'engine':
            raise OSError('Docker unavailable')
        return SimpleNamespace(returncode=1, stderr='permission denied')

    monkeypatch.setattr(runtime.subprocess, 'run', run)
    with pytest.raises(expected):
        runtime._cleanup_container_files(attempt, [])
    name = calls[0][calls[0].index('--name') + 1]
    assert calls[-1] == ['docker', 'rm', '--force', name]


def test_permission_cleanup_rejects_another_attempts_retained_file(tmp_path, monkeypatch):
    attempt = tmp_path / 'attempt'
    (attempt / 'output').mkdir(parents=True)
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *a, **kw: pytest.fail('Invalid cleanup reached Docker'))
    with pytest.raises(ValueError, match='belong to this attempt'):
        runtime._cleanup_container_files(attempt, [tmp_path / 'another-output.csv'])


def test_permission_cleanup_does_not_mount_symlink_roots(tmp_path, monkeypatch):
    outside = tmp_path / 'originals'
    outside.mkdir()
    original = outside / 'original.csv'
    original.write_text('original')
    attempt = tmp_path / 'attempt'
    attempt.mkdir()
    (attempt / 'scratch').symlink_to(outside, target_is_directory=True)
    (attempt / 'output').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *a, **kw: pytest.fail('Symlink cleanup reached Docker'))
    runtime._cleanup_container_files(attempt, [])
    with pytest.raises(ValueError, match='real analysis attempt'):
        runtime._cleanup_container_files(tmp_path / 'attempt' / 'scratch', [])
    assert original.read_text() == 'original'


@pytest.fixture
def cleanup_app():
    from flask import Flask
    with Flask(__name__).app_context():
        yield


def test_host_permission_failure_uses_fallback_once_then_retries_pruning(tmp_path, monkeypatch, cleanup_app):
    from backend.util import agent_python
    attempt = tmp_path / 'attempt'
    private = attempt / 'scratch' / 'private'
    private.mkdir(parents=True)
    (private / 'temporary.txt').write_text('temporary')
    retained = attempt / 'output' / 'kept.csv'
    retained.parent.mkdir()
    retained.write_text('total\n42\n')
    log = attempt / 'stdout.log'
    log.write_text('saved diagnostic')
    inputs = attempt / 'inputs'
    inputs.mkdir()
    (inputs / 'copy.csv').write_text('copy')
    blocked = True
    original_rmtree = agent_python.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path) == attempt / 'scratch' and blocked:
            error = PermissionError('Container-owned directory')
            kwargs['onerror'](os.scandir, str(private), (PermissionError, error, None))
            return
        return original_rmtree(path, *args, **kwargs)

    calls = []
    def cleanup(directory, retained_files):
        nonlocal blocked
        calls.append((directory, set(retained_files)))
        blocked = False

    monkeypatch.setattr(agent_python.shutil, 'rmtree', rmtree)
    monkeypatch.setattr(runtime, '_cleanup_container_files', cleanup)
    agent_python._cleanup_attempt(attempt, {retained, log})
    assert calls == [(attempt, {retained, log})]
    assert not (attempt / 'scratch').exists() and not inputs.exists()
    assert retained.read_text() == 'total\n42\n'
    assert log.read_text() == 'saved diagnostic'


def test_host_nonpermission_cleanup_error_is_logged_without_docker(tmp_path, monkeypatch, cleanup_app, caplog):
    from backend.util import agent_python
    attempt = tmp_path / 'attempt'
    temporary = attempt / 'scratch' / 'temporary.txt'
    temporary.parent.mkdir(parents=True)
    temporary.write_text('temporary')
    original_rmtree = agent_python.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path) == attempt:
            error = OSError('Simulated filesystem failure')
            kwargs['onerror'](os.unlink, str(temporary), (OSError, error, None))
            return
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(agent_python.shutil, 'rmtree', rmtree)
    monkeypatch.setattr(runtime, '_cleanup_container_files', lambda *a: pytest.fail('Unexpected Docker cleanup'))
    agent_python._cleanup_attempt(attempt, set())
    assert temporary.read_text() == 'temporary'
    assert 'Python temporary file cleanup failed' in caplog.text


def test_host_fallback_failure_is_logged_without_replacing_the_result(tmp_path, monkeypatch, cleanup_app, caplog):
    from backend.util import agent_python
    attempt = tmp_path / 'attempt'
    private = attempt / 'scratch' / 'private'
    private.mkdir(parents=True)
    (private / 'temporary.txt').write_text('temporary')
    retained = attempt / 'output' / 'saved.csv'
    retained.parent.mkdir()
    retained.write_text('saved')
    original_rmtree = agent_python.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path) == attempt / 'scratch':
            error = PermissionError('Container-owned directory')
            kwargs['onerror'](os.scandir, str(private), (PermissionError, error, None))
            return
        return original_rmtree(path, *args, **kwargs)

    def fail(*args):
        raise RuntimeError('Cleanup engine unavailable')

    monkeypatch.setattr(agent_python.shutil, 'rmtree', rmtree)
    monkeypatch.setattr(runtime, '_cleanup_container_files', fail)
    agent_python._cleanup_attempt(attempt, {retained})
    assert retained.read_text() == 'saved'
    assert 'Python attempt permission cleanup failed' in caplog.text
    assert 'Python temporary file cleanup failed' in caplog.text


def test_host_pruning_unlinks_symlinks_without_touching_originals(tmp_path, monkeypatch, cleanup_app):
    from backend.util import agent_python
    original = tmp_path / 'original.csv'
    original.write_text('original')
    attempt = tmp_path / 'attempt'
    attempt.mkdir()
    (attempt / 'scratch').symlink_to(tmp_path, target_is_directory=True)
    output = attempt / 'output'
    output.mkdir()
    retained = output / 'saved.csv'
    retained.write_text('saved')
    (output / 'unpublished.csv').symlink_to(original)
    monkeypatch.setattr(runtime, '_cleanup_container_files', lambda *a: pytest.fail('Unexpected Docker cleanup'))
    agent_python._cleanup_attempt(attempt, {retained})
    assert original.read_text() == 'original'
    assert retained.read_text() == 'saved'
    assert list(output.iterdir()) == [retained]
    assert not (attempt / 'scratch').is_symlink()


def test_host_pruning_does_not_follow_a_directory_replaced_before_rmtree(tmp_path, monkeypatch, cleanup_app):
    from backend.util import agent_python
    outside = tmp_path / 'originals'
    outside.mkdir()
    original = outside / 'original.csv'
    original.write_text('original')
    original.chmod(0o444)
    original_mode = original.stat().st_mode
    attempt = tmp_path / 'attempt'
    scratch = attempt / 'scratch'
    scratch.mkdir(parents=True)
    (scratch / 'temporary.txt').write_text('temporary')
    retained = attempt / 'output' / 'saved.csv'
    retained.parent.mkdir()
    retained.write_text('saved')
    original_rmtree = agent_python.shutil.rmtree
    swapped = False

    def rmtree(path, *args, **kwargs):
        nonlocal swapped
        if Path(path) == scratch and not swapped:
            scratch.rename(attempt / 'moved-scratch')
            scratch.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(agent_python.shutil, 'rmtree', rmtree)
    monkeypatch.setattr(runtime, '_cleanup_container_files', lambda *a: pytest.fail('Unexpected Docker cleanup'))
    agent_python._cleanup_attempt(attempt, {retained})
    assert swapped
    assert original.read_text() == 'original' and original.stat().st_mode == original_mode
    assert retained.read_text() == 'saved'


@pytest.mark.skipif(os.environ.get('ZYX_PYTHON_LIVE') != '1', reason='Set ZYX_PYTHON_LIVE=1 for Docker integration.')
def test_real_container_analysis_recovery_and_input_isolation(tmp_path):
    source = tmp_path / 'payments.csv'
    source.write_text('invoice,amount\n001,20\n001,22\n002,7\n')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    code = '''import pandas as pd
data = pd.read_csv(input_paths['payments'], dtype={'invoice': str})
totals = data.groupby('invoice', as_index=False)['amount'].sum()
totals.to_csv(output_dir / 'totals.csv', index=False)
result = totals.to_dict(orient='records')
'''
    answer = runtime.run_python(code, {'payments': source}, {}, attempt_dir=tmp_path / 'good')
    assert answer['error'] is False, answer
    assert answer['result'] == [{'invoice': '001', 'amount': 42}, {'invoice': '002', 'amount': 7}]
    assert answer['output_files'][0]['name'] == 'totals.csv'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    failure = runtime.run_python("open(input_paths['payments'], 'w').write('changed')", {'payments': source}, {},
                                 attempt_dir=tmp_path / 'readonly')
    assert failure['error'] is True
    assert 'Read-only file system' in failure['traceback']
    missing = runtime.run_python("raise KeyError('Amount')", {}, {}, attempt_dir=tmp_path / 'error')
    assert missing['error'] is True and 'KeyError' in missing['traceback']


@pytest.mark.skipif(os.environ.get('ZYX_PYTHON_LIVE') != '1', reason='Set ZYX_PYTHON_LIVE=1 for Docker integration.')
def test_real_container_cleanup_removes_private_files_and_preserves_retained_outputs(tmp_path):
    source = tmp_path / 'original.csv'
    source.write_text('amount\n42\n')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    attempt = tmp_path / 'attempt'
    code = '''private = scratch_dir / 'private'
private.mkdir(mode=0o700)
(private / 'temporary.txt').write_text('temporary')
(scratch_dir / 'outside').symlink_to(input_paths['source'])
(output_dir / 'retained.csv').write_text('total\\n42\\n')
(output_dir / 'partial.py').write_text('unpublished')
discard = output_dir / 'private'
discard.mkdir(mode=0o700)
(discard / 'temporary.txt').write_text('temporary')
result = 42
'''
    answer = runtime.run_python(code, {'source': source}, {}, attempt_dir=attempt)
    assert not answer['error'], answer
    retained = attempt / 'output' / 'retained.csv'
    logs = [Path(value) for value in answer['log_paths'].values()]
    log_contents = [path.read_bytes() for path in logs]
    runtime._cleanup_container_files(attempt, [retained, *logs])
    assert list((attempt / 'scratch').iterdir()) == []
    assert list((attempt / 'output').iterdir()) == [retained]
    assert retained.read_text() == 'total\n42\n'
    assert [path.read_bytes() for path in logs] == log_contents
    assert (attempt / 'inputs' / 'file-0.csv').is_file()
    assert (attempt / 'job' / 'script.py').is_file()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest


@pytest.mark.skipif(os.environ.get('ZYX_PYTHON_LIVE') != '1', reason='Set ZYX_PYTHON_LIVE=1 for Docker integration.')
def test_real_container_recalculates_working_copy(tmp_path):
    import openpyxl
    source = tmp_path / 'formula.xlsx'
    workbook = openpyxl.Workbook()
    workbook.active.title = 'Inputs'
    workbook.active.append(['Amount', 20])
    workbook.active.append(['Amount', 22])
    workbook.create_sheet('Summary')['B4'] = '=SUM(Inputs!B1:B2)'
    workbook.save(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    code = '''from helpers import recalculate_workbook
from openpyxl import load_workbook
path = recalculate_workbook(input_paths['workbook'])
calculated = load_workbook(path, data_only=True)
result = {'total': calculated['Summary']['B4'].value}
'''
    answer = runtime.run_python(code, {'workbook': source}, {}, attempt_dir=tmp_path / 'recalculate')
    assert answer['error'] is False, answer
    assert answer['result'] == {'total': 42}
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


@pytest.mark.skipif(os.environ.get('ZYX_PYTHON_LIVE') != '1', reason='Set ZYX_PYTHON_LIVE=1 for Docker integration.')
def test_real_container_is_nonroot_without_network_and_has_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv('LEXINTEL_PRIVATE_TEST_TOKEN', 'must-not-reach-the-container')
    code = '''import os, socket
try:
    socket.create_connection(('1.1.1.1', 443), timeout=0.5)
    network_blocked = False
except OSError:
    network_blocked = True
result = {'uid': os.getuid(), 'network_blocked': network_blocked,
          'host_secret': os.environ.get('LEXINTEL_PRIVATE_TEST_TOKEN')}
'''
    answer = runtime.run_python(code, {}, {}, attempt_dir=tmp_path / 'isolation')
    assert answer['error'] is False, answer
    assert answer['result'] == {'uid': 65532, 'network_blocked': True, 'host_secret': None}
    timeout = runtime.run_python('import time; time.sleep(30)', {}, {}, timeout_seconds=1,
                                 attempt_dir=tmp_path / 'timeout')
    assert timeout['status'] == 'timeout'
    assert timeout['output_files'] == []


@pytest.mark.skipif(os.environ.get('ZYX_PYTHON_LIVE') != '1', reason='Set ZYX_PYTHON_LIVE=1 for Docker integration.')
def test_real_container_reports_formula_errors_and_external_dependencies(tmp_path):
    from zipfile import ZipFile
    import openpyxl
    source = tmp_path / 'errors.xlsx'
    workbook = openpyxl.Workbook()
    workbook.active['A1'] = '=1/0'
    workbook.save(source)
    code = '''from openpyxl import load_workbook
path = recalculate_workbook(input_paths['workbook'])
result = {'cell': load_workbook(path, data_only=True).active['A1'].value}
'''
    answer = runtime.run_python(code, {'workbook': source}, {}, attempt_dir=tmp_path / 'formula-error')
    assert answer['error'] is False, answer
    assert answer['result'] == {'cell': '#DIV/0!'}
    assert '#DIV/0!' in answer['stderr']
    # The helper detects workbook dependencies before asking LibreOffice to
    # load them, so stale external cached values cannot look freshly computed.
    with ZipFile(source, 'a') as archive:
        archive.writestr('xl/externalLinks/externalLink1.xml', '<externalLink/>')
    blocked = runtime.run_python(code, {'workbook': source}, {}, attempt_dir=tmp_path / 'external-link')
    assert blocked['error'] is True
    assert 'external workbooks' in blocked['traceback']
