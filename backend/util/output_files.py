"""Resolve explicitly selected final files into path-free download metadata."""
import json
from contextlib import ExitStack
from pathlib import Path
import time
from zipfile import ZipFile

from celery.exceptions import SoftTimeLimitExceeded
from openpyxl import load_workbook

from backend.database import db
from backend.models import AgentArtifact, AgentStep
from backend.util.agent_python import stored_file
from backend.util import workspace_service as service
from backend.util.python_execution import MAX_OUTPUT_BYTES


def _resolved_files(row, output_file_ids):
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
        selected.append(({'file_id': artifact.id, 'execution_id': execution.id,
                          'filename': filename, 'file_size': size}, value, path))
    return selected


def resolve_output_files(row, output_file_ids, *, verification=None):
    """Recheck selected bytes under publication locks; keep wire metadata stable."""
    files = _resolved_files(row, output_file_ids)
    if verification is not None:
        expected = {item['file_id']: item['sha256'] for item in verification
                    if item['status'] == 'passed'}
        actual = {item['file_id']: value['sha256'] for item, value, _ in files}
        if expected != actual:
            raise ValueError('Selected output changed after verification; select and verify it again.')
    return [item for item, _, _ in files]


def _workbook_readiness(path, check_deadline):
    """Read saved formulas/caches only: never calculate or fetch external links."""
    check_deadline()
    with ZipFile(path) as archive:
        if sum(item.file_size for item in archive.infolist()) > MAX_OUTPUT_BYTES:
            raise ValueError('Workbook exceeds the expanded output-size limit.')
    counts = {'sheets': 0, 'formula_cells': 0, 'missing_formula_results': 0,
              'error_cells': 0}
    examples = []
    with ExitStack() as stack:
        books = []
        for data_only in (False, True):
            book = load_workbook(path, read_only=True, data_only=data_only, keep_links=False)
            stack.callback(book.close)
            books.append(book)
            check_deadline()
        if not books[0].worksheets:
            raise ValueError('Workbook has no readable worksheets.')
        for sheet, values in zip(books[0].worksheets, books[1].worksheets, strict=True):
            counts['sheets'] += 1
            # Ignore understated dimensions, but reject invalid oversized grids.
            for view in (sheet, values):
                if (view.max_row or 0) > 1048576 or (view.max_column or 0) > 16384:
                    raise ValueError('Workbook has invalid worksheet dimensions.')
                view.reset_dimensions()
            for row, cached_row in zip(sheet.iter_rows(), values.iter_rows(), strict=True):
                check_deadline()
                for index, (cell, cached) in enumerate(zip(row, cached_row, strict=True)):
                    if index % 1024 == 0:
                        check_deadline()
                    reason = None
                    if cell.data_type == 'f':
                        counts['formula_cells'] += 1
                        # A serialized string cache may legitimately be empty.
                        if cached.value is None and cached.data_type not in ('str', 's', 'inlineStr'):
                            counts['missing_formula_results'] += 1
                            reason = 'missing formula result'
                    if cached.data_type == 'e':
                        counts['error_cells'] += 1
                        reason = 'Excel error ' + str(cached.value)
                    if reason and len(examples) < 5:
                        examples.append(f'{sheet.title}!{cell.coordinate}: {reason}')
    check_deadline()
    if counts['missing_formula_results'] or counts['error_cells']:
        raise ValueError(f"{counts['missing_formula_results']} missing formula results; "
                         f"{counts['error_cells']} Excel error cells. " + '; '.join(examples))
    return counts


def preflight_output_files(row, output_file_ids):
    """Inspect immutable selected files outside publication write transactions.

    Only this server creates reports. The caller must resolve the same selection
    against these hashes again under its normal publication locks.
    """
    service.validate_execution()
    deadline = row.deadline
    files = _resolved_files(row, output_file_ids)
    db.session.commit()  # Release the read transaction before workbook I/O.
    # A bounded cooperative scan also respects the request's earlier deadline.
    expires = time.monotonic() + min(30, max(0, (deadline - service.now()).total_seconds()))

    def check_deadline():
        if time.monotonic() >= expires:
            raise ValueError('File verification exceeded its time limit; the file was not verified.')

    reports = []
    for item, value, path in files:
        try:
            check_deadline()
            if item['file_size'] > MAX_OUTPUT_BYTES:
                raise ValueError('File exceeds the output-size limit.')
            report = {'file_id': item['file_id'], 'sha256': value['sha256'],
                      'check_version': 1, 'status': 'passed', 'check': 'integrity'}
            if Path(item['filename']).suffix.lower() == '.xlsx':
                report.update(check='xlsx_readiness', counts=_workbook_readiness(path, check_deadline))
            # A file changed during inspection must never receive a passing report.
            stored_file(value)
            check_deadline()
        except (service.WorkflowStopped, SoftTimeLimitExceeded, MemoryError):
            raise
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) else 'Workbook could not be read.'
            raise ValueError(f"Output {item['file_id']} ({item['filename']}) failed verification: {reason} "
                             'Repair or recalculate into a new output_dir file, then select its new file ID.') from exc
        reports.append(report)
    return reports
