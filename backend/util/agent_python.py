"""Application-owned inputs and durable artifacts around isolated Python runs."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import uuid

from flask import current_app

from backend.database import db
from backend.models import AgentArtifact, DocumentPreparation, UploadedFiles
from backend.util import case_assistant_workflow as service
from backend.util.document_preparation import validate_snapshot


def storage_root():
    root = Path(current_app.config.get('AGENT_PYTHON_STORAGE_DIR',
                os.environ.get('AGENT_PYTHON_STORAGE_DIR', Path(current_app.instance_path) / 'agent_python'))).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def stored_file(value):
    """Resolve a server-written storage key, never a model-supplied host path."""
    root = storage_root()
    key = value.get('storage_key')
    if not isinstance(key, str):
        raise ValueError('Saved file is unavailable.')
    unresolved = root / key
    path = unresolved.resolve()
    if unresolved.is_symlink() or not path.is_relative_to(root) or not path.is_file():
        raise ValueError('Saved file is unavailable.')
    if file_hash(path) != value.get('sha256'):
        raise ValueError('Saved file content changed; run the analysis again.')
    return path


def _preview(value, limit=12_000):
    rendered = json.dumps(value, ensure_ascii=False)
    return value if len(rendered) <= limit else {
        'preview_json': rendered[:limit], 'truncated': True,
        'message': 'Full results are saved in the execution artifact; read_sources supports paged reads.'}


def _cleanup_attempt(attempt, retained_files):
    """Keep artifact-linked files in place and discard this attempt's staging.

    The runner has already stopped its container. Links are removed as links;
    neither uploaded originals nor another attempt is traversed during cleanup.
    """
    retained = set(retained_files)
    retained_parents = {parent for path in retained for parent in path.parents
                        if parent.is_relative_to(attempt)}

    def prune():
        errors = []

        def failed(_function, filename, exception):
            if not isinstance(exception[1], FileNotFoundError):
                errors.append((Path(filename), exception[1]))

        def remove(path):
            if path in retained:
                return
            try:
                if stat.S_ISDIR(path.lstat().st_mode):
                    if path not in retained_parents:
                        # Use descriptor-based deletion for disposable trees;
                        # a replaced symlink must never redirect the walk.
                        shutil.rmtree(path, onerror=failed)
                    else:
                        for child in path.iterdir():
                            remove(child)
                        if not any(path.iterdir()):
                            path.rmdir()
                else:
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append((path, exc))

        remove(attempt)
        return errors

    errors = prune()
    if any(isinstance(exc, PermissionError)
           and path.relative_to(attempt).parts[:1] in {('scratch',), ('output',)}
           for path, exc in errors):
        # Linux workers may not own directories created by the container UID.
        # Restore our writable mount roots, then let that same non-root UID
        # remove its private children without mounting originals or other runs.
        from backend.util.python_execution import _cleanup_container_files
        for name in ('scratch', 'output'):
            directory = attempt / name
            try:
                if stat.S_ISDIR(directory.lstat().st_mode):
                    directory.chmod(directory.stat().st_mode | 0o777)
            except OSError:
                pass
        try:
            _cleanup_container_files(attempt, retained)
        except Exception:
            current_app.logger.exception('Python attempt permission cleanup failed: %s', attempt)
        errors = prune()
    for path, exc in errors:
        current_app.logger.warning('Python temporary file cleanup failed for %s: %s', path, exc)


def execute(operations, args):
    from backend.util.python_execution import run_python
    from backend.util.prepared_retrieval import authorized_artifacts

    validate_snapshot(operations.row.user_id, operations.row.chat_thread_id, operations.snapshot)
    accepted = {entry['file_id']: entry for entry in operations.snapshot['files']}
    if set(args.file_ids) - accepted.keys():
        raise ValueError('Python input file IDs must come from this assignment\'s document catalogue.')
    authorized_artifacts(operations.snapshot)
    # No open SQL transaction is kept across execution or input-file I/O.
    source_rows = {doc.id: doc for doc in DocumentPreparation.query.filter(
        DocumentPreparation.id.in_(args.file_ids)).all()}
    sources = {}
    references = []
    for uid in dict.fromkeys(args.file_ids):
        doc = source_rows[uid]
        uploaded = db.session.get(UploadedFiles, doc.uploaded_file_id)
        references.append({'file_id': uid, 'filename': doc.filename, 'relative_path': doc.relative_path,
                           'prepared_artifact_id': accepted[uid]['artifact_id'],
                           'source_hash': accepted[uid]['source_hash']})
        sources[uid] = {'filename': doc.filename, 'path': doc.source_key,
                        'text': uploaded.content if not doc.filename.lower().endswith(('.xlsx', '.csv')) else None,
                        'hash': accepted[uid]['source_hash']}
    artifacts = {}
    for uid in dict.fromkeys(args.artifact_ids):
        value = operations.artifact(uid)
        for reference in value.get('references', []):
            if reference not in references:
                references.append(reference)
        artifacts[uid] = ({'file_path': str(stored_file(value)), 'filename': value['filename']}
                          if value.get('kind') == 'derived_file' else value)
    db.session.commit()

    root = storage_root()
    attempt = root / operations.row.id / operations.step.id / uuid.uuid4().hex
    attempt.mkdir(parents=True, mode=0o700)
    inputs = {}
    saved = False
    retained_files = set()
    try:
        for uid, source in sources.items():
            if source['text'] is not None:
                path = attempt / 'extracted' / (uid + '.txt')
                path.parent.mkdir(exist_ok=True)
                path.write_text(source['text'], encoding='utf-8')
            else:
                path = Path(source['path']) if source['path'] else None
                if not path or not path.is_file():
                    raise ValueError(f'Original spreadsheet bytes are unavailable for {source["filename"]}; upload the file again.')
                if file_hash(path) != source['hash']:
                    raise ValueError(f'Original spreadsheet content changed for {source["filename"]}; upload it again.')
            inputs[uid] = str(path)

        def check_execution():
            service.validate_execution()
            authorized_artifacts(operations.snapshot)
            db.session.commit()

        check_execution()
        remaining = max(0.01, (operations.row.deadline - service.now()).total_seconds())
        timeout = min(float(current_app.config.get('AGENT_PYTHON_TIMEOUT_SECONDS',
                      os.environ.get('AGENT_PYTHON_TIMEOUT_SECONDS', 120))), remaining)
        result = run_python(args.code, inputs, artifacts, check_execution=check_execution,
                            timeout_seconds=timeout, attempt_dir=attempt)
        check_execution()
        files = []
        if not result.get('error'):
            for output in result.get('output_files', []):
                path = Path(output['path'])
                if path.is_symlink() or not path.resolve().is_relative_to(attempt.resolve()):
                    raise ValueError('The runner returned an invalid output path.')
                value = {'kind': 'derived_file', 'filename': output['name'],
                         'text': 'New file generated by Python analysis: ' + output['name'],
                         'storage_key': str(path.resolve().relative_to(root)),
                         'sha256': file_hash(path), 'file_size': path.stat().st_size,
                         'references': references}
                file_id = operations.save('derived_file', value)
                retained_files.add(path.resolve())
                files.append({'id': file_id, 'filename': value['filename'],
                              'file_size': value['file_size'], 'sha256': value['sha256']})
        text = ('Python execution completed.' if not result.get('error') else 'Python execution returned an error.')
        text += '\n' + result.get('message', '')
        record = {key: result.get(key) for key in ('status', 'error', 'message', 'stdout', 'stderr', 'traceback', 'result',
                                                  'runtime_versions', 'stdout_truncated', 'stderr_truncated',
                                                  'duration_seconds', 'exit_code', 'rejected_files')}
        record.update(kind='python_execution', text=text, code=args.code,
                      references=references, input_artifact_ids=args.artifact_ids, files=files)
        # The runtime's logs remain durable without exposing host paths to the model.
        record['logs'] = {name: str(Path(path).resolve().relative_to(root))
                          for name, path in result.get('log_paths', {}).items()
                          if Path(path).resolve().is_relative_to(root)}
        # Save more than the runner's model-facing preview. read_sources can
        # page the saved stdout/stderr without rerunning the analytical code.
        for name, key in record['logs'].items():
            if name not in {'stdout', 'stderr'}:
                continue
            with (root / key).open('rb') as log:
                body = log.read(8 * 1024 * 1024 + 1)
            record[name] = body[:8 * 1024 * 1024].decode('utf-8', errors='replace')
            record[name + '_truncated'] = len(body) > 8 * 1024 * 1024
        artifact_id = operations.save('python_execution', record)
        retained_files.update(root / key for key in record['logs'].values())
        saved = True
        return {'artifact_id': artifact_id, 'kind': 'python_execution',
                'status': record['status'], 'error': bool(record['error']),
                'stdout': (record['stdout'] or '')[:8000], 'stderr': (record['stderr'] or '')[:4000],
                'traceback': (record['traceback'] or '')[:8000], 'result': _preview(record['result']),
                'stdout_truncated': bool(record['stdout_truncated']) or len(record['stdout'] or '') > 8000,
                'stderr_truncated': bool(record['stderr_truncated']) or len(record['stderr'] or '') > 4000,
                'rejected_files': record.get('rejected_files') or [],
                'references': references, 'files': files,
                'message': 'Execution and results saved; read_sources reads the full execution artifact.'
                           if not record['error'] else (record.get('message') or 'Execution error saved. Inspect the error and revise your Python if useful.')}
    finally:
        # Outputs/logs remain at their existing storage keys. A failed staging
        # operation publishes nothing, so its whole attempt is disposable.
        try:
            _cleanup_attempt(attempt, retained_files if saved else set())
        except Exception:
            current_app.logger.exception('Python attempt cleanup failed: %s', attempt)


def downloadable_file(workflow, execution_id, file_id):
    execution = AgentArtifact.query.filter_by(id=execution_id, workflow_run_id=workflow.id,
                                             kind='python_execution').first()
    if not execution or file_id not in {item['id'] for item in json.loads(execution.content_json).get('files', [])}:
        raise service.WorkflowNotFound('Generated file not found')
    artifact = AgentArtifact.query.filter_by(id=file_id, workflow_run_id=workflow.id, kind='derived_file').first()
    if not artifact:
        raise service.WorkflowNotFound('Generated file not found')
    value = json.loads(artifact.content_json)
    return stored_file(value), value['filename']
