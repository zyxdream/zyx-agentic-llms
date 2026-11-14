"""Resolve explicitly selected final files into path-free download metadata."""
import json

from backend.models import AgentArtifact, AgentStep
from backend.util.agent_python import stored_file


def resolve_output_files(row, output_file_ids):
    """Validate immutable file/execution relationships without publishing writes.

    The caller authorizes the run and its source snapshot and owns the atomic
    publication transaction. Filenames never identify a file or select a version.
    """
    if not isinstance(output_file_ids, list) or any(
            not isinstance(uid, str) or not uid for uid in output_file_ids):
        raise ValueError('Select generated file IDs from successful Python results.')
    selected = []
    for uid in dict.fromkeys(output_file_ids):
        artifact = AgentArtifact.query.filter_by(id=uid, run_id=row.id, kind='derived_file').first()
        if artifact is None:
            raise ValueError('Selected output must be a generated file from this request.')
        step = AgentStep.query.filter_by(id=artifact.step_id, run_id=row.id,
                                         kind='tool', name='python_run', state='ready').first()
        executions = AgentArtifact.query.filter_by(run_id=row.id, step_id=artifact.step_id,
                                                    kind='python_execution').all() if step else []
        if len(executions) != 1:
            raise ValueError('Selected output has no completed Python execution.')
        execution = executions[0]
        result = json.loads(execution.content_json)
        value = json.loads(artifact.content_json)
        files = result.get('files')
        matches = [file for file in files if isinstance(file, dict) and file.get('id') == uid] if isinstance(files, list) else []
        if result.get('status') != 'success' or result.get('error') or len(matches) != 1:
            raise ValueError('Selected output must belong to a successful Python execution.')
        if any(matches[0].get(key) != value.get(key) for key in ('filename', 'file_size', 'sha256')):
            raise ValueError('Selected output metadata does not match its Python execution.')
        filename, size = value.get('filename'), value.get('file_size')
        if (not isinstance(filename, str) or not filename or any(char in filename for char in ('/', '\\', '\x00'))
                or type(size) is not int or size < 0):
            raise ValueError('Selected output metadata is invalid.')
        path = stored_file(value)
        if path.stat().st_size != size:
            raise ValueError('Selected output size changed; run the analysis again.')
        selected.append({'file_id': artifact.id, 'execution_id': execution.id,
                         'filename': filename, 'file_size': size})
    return selected
