"""Run one analysis program in a disposable, isolated Docker container.

This module owns execution only. Callers authorize source/artifact IDs, choose a
durable attempt directory, and publish successful output files as artifacts.
Every invocation starts a fresh interpreter; files are the continuation state.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import uuid


IMAGE = 'zyx-agentic-llms-python-analysis:1'
PREVIEW_CHARACTERS = 16000
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 100 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
OUTPUT_EXTENSIONS = {'.csv', '.xlsx', '.json'}


def _docker():
    command = [os.environ.get('AGENT_PYTHON_DOCKER', 'docker')]
    if os.environ.get('AGENT_PYTHON_DOCKER_HOST'):
        command += ['--host', os.environ['AGENT_PYTHON_DOCKER_HOST']]
    return command


def _preview(path):
    with path.open('r', encoding='utf-8', errors='replace') as stream:
        value = stream.read(PREVIEW_CHARACTERS + 1)
    return value[:PREVIEW_CHARACTERS], len(value) > PREVIEW_CHARACTERS


def _source_path(value):
    return Path(value['path'] if isinstance(value, dict) else value).resolve(strict=True)


def _stage_file(source, directory, name):
    if not source.is_file():
        raise ValueError('An analysis input is not a regular file.')
    target = directory / (name + source.suffix.lower())
    shutil.copyfile(source, target)
    target.chmod(0o444)
    return '/inputs/' + target.name


def _output_size(directory):
    """Do not follow links created by the analysis program."""
    total = 0
    for root, directories, files in os.walk(directory, followlinks=False):
        directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
        for name in files:
            try:
                info = (Path(root) / name).lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _files(directory):
    result, rejected = [], []
    for path in sorted(directory.iterdir()):
        if path.name == '_execution.json':
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.suffix.lower() not in OUTPUT_EXTENSIONS:
            rejected.append(path.name)
            continue
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        result.append({'name': path.name, 'path': str(path), 'size': info.st_size,
                       'sha256': digest.hexdigest()})
    return result, rejected


def _remove_container(command, name):
    try:
        subprocess.run([*command, 'rm', '--force', name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


_CLEANUP_PROGRAM = '''import json, os, stat, sys
from pathlib import Path

def prune(directory, root, kept):
    info = directory.lstat()
    if info.st_uid == os.geteuid():
        directory.chmod(stat.S_IMODE(info.st_mode) | 0o700)
    for child in directory.iterdir():
        relative = child.relative_to(root).as_posix()
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode):
            child.unlink()
        elif stat.S_ISDIR(info.st_mode):
            prune(child, root, kept)
            if not any(name.startswith(relative + '/') for name in kept):
                child.rmdir()
        elif relative not in kept:
            child.unlink()

for name, kept in json.loads(sys.argv[1]).items():
    root = Path(name)
    prune(root, root, set(kept))
'''


def _cleanup_container_files(attempt_dir, retained_files):
    """Remove container-owned leftovers when host permissions prevent cleanup.

    Only this attempt's writable mount roots are exposed to a trusted program
    running as the same non-root user. Published files keep their existing paths.
    """
    attempt = Path(attempt_dir)
    if attempt.is_symlink():
        raise ValueError('Cleanup requires a real analysis attempt directory.')
    attempt = attempt.resolve(strict=True)
    retained = []
    for path in retained_files:
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(attempt):
            raise ValueError('Retained analysis files must belong to this attempt.')
        retained.append(path)
    mounts, manifest = [], {}
    for name in ('scratch', 'output'):
        directory = attempt / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        target = '/cleanup/' + name
        mounts += ['--mount', f'type=bind,src={directory},dst={target}']
        manifest[target] = [path.relative_to(directory).as_posix()
                            for path in retained if path.is_relative_to(directory)]
    if not manifest:
        return
    command, name = _docker(), 'zyx-agentic-llms-python-cleanup-' + uuid.uuid4().hex
    args = [*command, 'run', '--name', name, '--init', '--network', 'none',
            '--read-only', '--user', '65532:65532', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--pids-limit', '128',
            '--memory', '1g', '--memory-swap', '1g', '--cpus', '1',
            *mounts, '--entrypoint', '/opt/python/bin/python',
            os.environ.get('AGENT_PYTHON_IMAGE', IMAGE), '-B', '-c',
            _CLEANUP_PROGRAM, json.dumps(manifest)]
    try:
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, timeout=10, check=False)
        if result.returncode:
            raise RuntimeError('Analysis temporary-file cleanup failed: ' + (result.stderr or '')[-1000:])
    finally:
        _remove_container(command, name)


def run_python(code, inputs, artifacts, *, check_execution=None, timeout_seconds=120,
               attempt_dir=None):
    """Execute Python and return observations plus unpublished output candidates.

    ``inputs`` maps authorized file IDs to filesystem paths (or ``{'path': ...}``).
    ``artifacts`` maps authorized artifact IDs to JSON values; a ``{'file_path':
    ...}`` value explicitly imports a previously saved file instead. Only those
    files are copied into the container. ``check_execution`` may raise the
    application's cancellation/deadline exception; it is propagated after
    terminating the container. Script errors and script timeouts are returned.
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError('Supply a nonempty Python program.')
    if timeout_seconds <= 0:
        raise ValueError('Python execution timeout must be positive.')
    if check_execution:
        check_execution()
    attempt = Path(attempt_dir) if attempt_dir is not None else Path(tempfile.mkdtemp(prefix='zyx-agentic-llms-python-'))
    attempt.mkdir(parents=True, exist_ok=True, mode=0o700)
    # An execution attempt is immutable after use. A worker retry gets a new
    # directory, rather than reusing partially written files from a lost worker.
    directories = {name: attempt / name for name in ('inputs', 'job', 'scratch', 'output')}
    for name, directory in directories.items():
        directory.mkdir(mode=0o777 if name in {'scratch', 'output'} else 0o755)
        if name in {'scratch', 'output'}:
            directory.chmod(0o777)
    paths = {uid: _stage_file(_source_path(value), directories['inputs'], f'file-{index}')
             for index, (uid, value) in enumerate(inputs.items())}
    imported = {}
    for index, (uid, value) in enumerate(artifacts.items()):
        imported[uid] = (_stage_file(Path(value['file_path']).resolve(strict=True), directories['inputs'], f'artifact-{index}')
                         if isinstance(value, dict) and 'file_path' in value else value)
    (directories['job'] / 'script.py').write_text(code, encoding='utf-8')
    (directories['job'] / 'request.json').write_text(
        json.dumps({'input_paths': paths, 'input_artifacts': imported}, ensure_ascii=False, allow_nan=False),
        encoding='utf-8')
    command, container_name = _docker(), 'zyx-agentic-llms-python-' + uuid.uuid4().hex
    args = [*command, 'run', '--name', container_name, '--init', '--network', 'none',
            '--read-only', '--user', '65532:65532', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--pids-limit', '128',
            '--memory', '1g', '--memory-swap', '1g', '--cpus', '1',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=256m',
            '--env', 'HOME=/scratch/home', '--env', 'PYTHONUNBUFFERED=1']
    for name, target in (('inputs', '/inputs'), ('job', '/job'), ('scratch', '/scratch'), ('output', '/output')):
        args += ['--mount', f'type=bind,src={directories[name].resolve()},dst={target}'
                 + (',readonly' if name in {'inputs', 'job'} else '')]
    args += [os.environ.get('AGENT_PYTHON_IMAGE', IMAGE), f'{timeout_seconds:g}s',
             '/opt/python/bin/python', '-B', '/opt/runtime/bootstrap.py']
    stdout_path, stderr_path = attempt / 'stdout.log', attempt / 'stderr.log'
    process, interrupted, unavailable = None, None, None
    started = time.monotonic()
    try:
        with stdout_path.open('wb') as out, stderr_path.open('wb') as err:
            try:
                process = subprocess.Popen(args, stdout=out, stderr=err)
            except OSError as exc:
                unavailable = str(exc)
            while process is not None and process.poll() is None:
                if check_execution:
                    check_execution()
                if time.monotonic() - started > timeout_seconds:
                    interrupted = 'timeout'
                    break
                if stdout_path.stat().st_size + stderr_path.stat().st_size > MAX_LOG_BYTES:
                    interrupted = 'output_limit'
                    break
                if _output_size(directories['output']) + _output_size(directories['scratch']) > MAX_OUTPUT_BYTES:
                    interrupted = 'output_limit'
                    break
                time.sleep(0.25)
    finally:
        # This also runs when Celery raises SoftTimeLimitExceeded or the host's
        # execution callback raises WorkflowStopped. Code never outlives the task.
        try:
            _remove_container(command, container_name)
        finally:
            if process is not None and process.poll() is None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
    if check_execution:
        check_execution()
    stdout, stdout_truncated = _preview(stdout_path)
    stderr, stderr_truncated = _preview(stderr_path)
    result = {'error': True, 'status': 'execution_error', 'stdout': stdout, 'stderr': stderr,
              'stdout_truncated': stdout_truncated, 'stderr_truncated': stderr_truncated,
              'traceback': None, 'result': None, 'output_files': [], 'rejected_files': [],
              'exit_code': process.returncode if process is not None else None,
              'log_paths': {'stdout': str(stdout_path), 'stderr': str(stderr_path)},
              'duration_seconds': round(time.monotonic() - started, 3), 'runtime_versions': {}}
    if unavailable or (process is not None and process.returncode in {125, 126, 127}):
        result.update(status='runtime_unavailable', message='The isolated Python runtime is unavailable. '
                      'Start Docker and build the configured analysis image. ' + (unavailable or stderr))
        return result
    if interrupted or (process is not None and process.returncode == 124):
        interrupted = interrupted or 'timeout'
        result.update(status=interrupted, message='Python execution exceeded its time limit.' if interrupted == 'timeout'
                      else 'Python execution exceeded its output storage limit.')
        return result
    try:
        metadata_path = directories['output'] / '_execution.json'
        if metadata_path.is_symlink() or not metadata_path.is_file() or metadata_path.stat().st_size > MAX_RESULT_BYTES:
            result['message'] = 'Python execution ended without a usable result record.'
            return result
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        if not isinstance(metadata, dict) or metadata.get('status') not in {'success', 'execution_error'}:
            raise ValueError('Invalid execution metadata')
    except (OSError, ValueError):
        result['message'] = 'Python execution returned an unreadable result record.'
        return result
    result.update({key: metadata.get(key) for key in ('status', 'traceback', 'result', 'runtime_versions')})
    result['error'] = metadata['status'] != 'success' or process.returncode != 0
    result['message'] = 'Python analysis completed.' if not result['error'] else 'Python raised an error. Inspect the traceback and revise the program.'
    if not result['error']:
        try:
            result['output_files'], result['rejected_files'] = _files(directories['output'])
        except OSError as exc:
            result.update(error=True, status='execution_error', output_files=[],
                          message='Python output files could not be collected: ' + str(exc))
    return result
