"""Container entrypoint: execute a single program and describe its result."""
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
import traceback

from helpers import recalculate_workbook


def main():
    request = json.loads(Path('/job/request.json').read_text(encoding='utf-8'))
    output_dir, scratch_dir = Path('/output'), Path('/scratch')
    (scratch_dir / 'home').mkdir(exist_ok=True)
    namespace = {'__name__': '__main__', '__file__': '/job/script.py',
                 'input_paths': request['input_paths'], 'input_artifacts': request['input_artifacts'],
                 'output_dir': output_dir, 'scratch_dir': scratch_dir,
                 'recalculate_workbook': recalculate_workbook}
    metadata = {'status': 'success', 'result': None, 'traceback': None,
                'runtime_versions': {'python': platform.python_version(), **{
                    name: importlib.metadata.version(name) for name in ('pandas', 'numpy', 'openpyxl')}}}
    exit_code = 0
    try:
        code = Path('/job/script.py').read_text(encoding='utf-8')
        exec(compile(code, '/job/script.py', 'exec'), namespace)
        metadata['result'] = namespace.get('result')
        # Fail as an ordinary Python observation for e.g. a raw DataFrame result;
        # the agent can use to_dict() or write a CSV instead.
        json.dumps(metadata['result'], ensure_ascii=False, allow_nan=False)
    except BaseException:
        metadata.update(status='execution_error', result=None, traceback=traceback.format_exc())
        print(metadata['traceback'], file=sys.stderr)
        exit_code = 1
    (output_dir / '_execution.json').write_text(json.dumps(metadata, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
