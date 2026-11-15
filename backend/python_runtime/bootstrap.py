"""Container entrypoint: execute a single program and describe its result."""
import importlib.metadata
from datetime import date, datetime
import json
from pathlib import Path
import platform
import sys
import traceback

from helpers import check_workbook, recalculate_workbook


def result_json_default(value):
    """Only normalize well-defined date values; unsupported objects remain errors."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f'{type(value).__name__} is not JSON serializable; return plain values '
                    'or convert tabular data with to_dict(), or save it as a file.')


def main():
    request = json.loads(Path('/job/request.json').read_text(encoding='utf-8'))
    output_dir, scratch_dir = Path('/output'), Path('/scratch')
    (scratch_dir / 'home').mkdir(exist_ok=True)
    namespace = {'__name__': '__main__', '__file__': '/job/script.py',
                 'input_paths': request['input_paths'], 'input_artifacts': request['input_artifacts'],
                 'output_dir': output_dir, 'scratch_dir': scratch_dir,
                 'recalculate_workbook': recalculate_workbook, 'check_workbook': check_workbook}
    metadata = {'status': 'success', 'result': None, 'traceback': None,
                'runtime_versions': {'python': platform.python_version(), **{
                    name: importlib.metadata.version(name) for name in ('pandas', 'numpy', 'openpyxl')}}}
    exit_code = 0
    try:
        code = Path('/job/script.py').read_text(encoding='utf-8')
        exec(compile(code, '/job/script.py', 'exec'), namespace)
        metadata['result'] = json.loads(json.dumps(namespace.get('result'), ensure_ascii=False,
            allow_nan=False, default=result_json_default))
    except BaseException:
        metadata.update(status='execution_error', result=None, traceback=traceback.format_exc())
        print(metadata['traceback'], file=sys.stderr)
        exit_code = 1
    (output_dir / '_execution.json').write_text(json.dumps(metadata, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
