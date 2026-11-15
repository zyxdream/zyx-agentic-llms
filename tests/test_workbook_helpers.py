"""Strict result serialization and explicit spreadsheet checks, with optional real Calc."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from backend.python_runtime import helpers
from backend.util import python_execution as runtime


def test_dates_are_normalized_without_accepting_arbitrary_objects(monkeypatch):
    monkeypatch.setitem(sys.modules, 'helpers', helpers)
    spec = importlib.util.spec_from_file_location('test_runtime_bootstrap',
        Path(__file__).parents[1] / 'backend/python_runtime/bootstrap.py')
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    value = {'rows': [{'day': date(2026, 5, 3),
                      'at': datetime(2026, 5, 3, 12, 30, tzinfo=timezone(timedelta(hours=-5)))}],
             'amount': 12.5, 'flag': True, 'empty': None}
    encoded = json.loads(json.dumps(value, default=bootstrap.result_json_default, allow_nan=False))
    assert encoded == {'rows': [{'day': '2026-05-03', 'at': '2026-05-03T12:30:00-05:00'}],
                       'amount': 12.5, 'flag': True, 'empty': None}
    for invalid in (object(), {1, 2}):
        with pytest.raises(TypeError, match='not JSON serializable'):
            json.dumps(invalid, default=bootstrap.result_json_default, allow_nan=False)
    for invalid in (float('nan'), float('inf')):
        with pytest.raises(ValueError):
            json.dumps(invalid, default=bootstrap.result_json_default, allow_nan=False)


@pytest.mark.parametrize('checks', [[], None, [{}], [{'changes': {}, 'expected': {}}],
    [{'changes': [], 'expected': {'Inputs!A1': 1}}],
    [{'changes': {}, 'expected': {'Inputs!A1': 1}, 'unknown': True}]])
def test_invalid_workbook_checks_fail_before_recalculation(tmp_path, monkeypatch, checks):
    source = tmp_path / 'unused.xlsx'
    source.touch()
    monkeypatch.setattr(helpers, 'recalculate_workbook', lambda *a, **kw: pytest.fail('Unexpected calculation'))
    with pytest.raises(ValueError):
        helpers.check_workbook(source, checks)


@pytest.mark.parametrize('reference', ['A1', 'Inputs!A1:B2', 'Inputs!A0', 'Inputs!1', 'Inputs!../A1'])
def test_probe_references_are_explicit_single_cells(reference):
    with pytest.raises(ValueError):
        helpers._workbook_cell({}, reference)


@pytest.mark.skipif(os.environ.get('ZYX_PYTHON_LIVE') != '1', reason='Set ZYX_PYTHON_LIVE=1 for Docker integration.')
def test_real_checks_detect_disconnected_inputs_and_fit_text_without_changing_original(tmp_path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment
    source = tmp_path / 'schedules.xlsx'
    book = Workbook()
    for index, name in enumerate(['Connected', 'Disconnected']):
        sheet = book.active if index == 0 else book.create_sheet()
        sheet.title = name
        sheet['B1'] = datetime(2026, 1, 1)
        sheet['B2'] = datetime(2026, 1, 31)
        sheet['B3'] = 0.12
        sheet['B4'] = 1000
        sheet['B5'] = '=B2-B1' if index == 0 else 30
        sheet['B6'] = '=B4*B3*B5/365'
        sheet['B6'].number_format = '$0.00'
        sheet.merge_cells('A9:C9')
        sheet['A9'] = 'A complete heading for a general financial schedule that must remain readable at the existing table width.'
        sheet['A10'] = 'A source note with several wrapped lines and important assumptions that should not be hidden below the row.'
        sheet['C10'] = '=B6*2'
        for col in ('A', 'B', 'C'):
            sheet.column_dimensions[col].width = 12
        for row in (9, 10):
            sheet.row_dimensions[row].height = 12
            sheet[f'A{row}'].alignment = Alignment(wrap_text=True)
    book.save(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    code = '''from datetime import datetime, date, timezone
from openpyxl import load_workbook
path = input_paths['book']
fitted = recalculate_workbook(path, output_dir/'finished.xlsx',
    fit_text_ranges={'Connected': ['A9:C10']})
checks = check_workbook(fitted, [
    {'changes': {'Connected!B2': datetime(2026,2,1), 'Disconnected!B2': datetime(2026,2,1)},
     'expected': {'Connected!B6': 1000*.12*31/365, 'Disconnected!B6': 1000*.12*31/365,
                  'Connected!B4': 1000}},
    {'changes': {'Connected!B3': .24}, 'expected': {'Connected!B6': 1000*.24*30/365, 'Connected!B4': 1000}}])
w = load_workbook(fitted, data_only=False)
v = load_workbook(fitted, data_only=True)
result = {'checks': checks, 'day': date(2026,2,1), 'at': datetime(2026,2,1,tzinfo=timezone.utc),
          'sheets': w.sheetnames, 'formula': w['Connected']['B6'].value,
          'numeric_format': w['Connected']['B6'].number_format,
          'value': v['Connected']['B6'].value,
          'heights': {str(r): w['Connected'].row_dimensions[r].height for r in (9,10)},
          'untouched_height': w['Disconnected'].row_dimensions[9].height,
          'widths': [{'min':d.min,'max':d.max,'width':d.width} for d in w['Connected'].column_dimensions.values()],
          'merge': str(w['Connected'].merged_cells), 'other_formula':w['Connected']['C10'].value}
'''
    answer = runtime.run_python(code, {'book': source}, {}, attempt_dir=tmp_path / 'probe')
    assert answer['error'] is False, answer
    result = answer['result']
    assert not result['checks']['passed']
    first, second = result['checks']['checks']
    assert [c['passed'] for c in first['comparisons']] == [True, False, True]
    assert second['passed'] and not first['formula_errors']
    assert result['day'] == '2026-02-01' and result['at'] == '2026-02-01T00:00:00+00:00'
    assert result['sheets'] == ['Connected', 'Disconnected']
    assert result['formula'] == '=B4*B3*B5/365' and result['other_formula'] == '=B6*2'
    assert result['value'] == pytest.approx(1000*.12*30/365)
    assert '0.00' in result['numeric_format']
    assert all(h > 12 for h in result['heights'].values())
    assert result['untouched_height'] == 12 and result['merge'] == 'A9:C9'
    # Calc groups identically sized columns in OOXML. Missing individual B/C
    # dimension entries are not default-width columns; resolve the group span.
    assert all(next(d['width'] for d in result['widths'] if d['min'] <= i <= d['max']) == 12
               for i in (1, 2, 3))
    assert len(answer['output_files']) == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


@pytest.mark.skipif(os.environ.get('ZYX_PYTHON_LIVE') != '1', reason='Set ZYX_PYTHON_LIVE=1 for Docker integration.')
def test_real_probe_cannot_replace_formulas_and_unsupported_result_still_errors(tmp_path):
    code = '''from openpyxl import Workbook
w=Workbook();w.active.title='Inputs';w.active['A1']='=1+1'
p=scratch_dir/'source.xlsx';w.save(p)
result=check_workbook(p,[{'changes':{'Inputs!A1':3},'expected':{'Inputs!A1':3}}])
'''
    blocked = runtime.run_python(code, {}, {}, attempt_dir=tmp_path/'blocked')
    assert blocked['error'] and 'must not overwrite formula' in blocked['traceback']
    unsupported = runtime.run_python('result = {1,2}', {}, {}, attempt_dir=tmp_path/'unsupported')
    assert unsupported['error'] and 'not JSON serializable' in unsupported['traceback']
    assert unsupported['output_files'] == []
