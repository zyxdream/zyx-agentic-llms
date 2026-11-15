"""Selected workbook readiness is about the saved bytes, not an agent assertion."""
from io import BytesIO
import hashlib
import json
from zipfile import ZipFile, ZIP_DEFLATED
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from openpyxl import Workbook
import pytest

from backend.database import db
from backend.util import output_files
from tests.test_document_preparation import app
from tests.test_output_files import files


def workbook_bytes(*, cache='2', cache_type='n', formula=True, error=None):
    book = Workbook()
    book.active['A1'] = 'Calculation'
    book.active['B1'] = '=1+1' if formula else 2
    # An ordinary string resembling an Excel error is not an error cell.
    book.active['A2'] = '#N/A'
    book.active['A2'].data_type = 's'
    if error:
        book.active['C1'] = error
    stream = BytesIO()
    book.save(stream)
    result = BytesIO()
    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    with ZipFile(stream) as source, ZipFile(result, 'w', compression=ZIP_DEFLATED) as target:
        for name in source.namelist():
            body = source.read(name)
            if name == 'xl/worksheets/sheet1.xml' and formula:
                tree = ET.fromstring(body)
                cell = next(c for c in tree.iter(ns + 'c') if c.get('r') == 'B1')
                cell.set('t', cache_type)
                value = cell.find(ns + 'v')
                if cache is None:
                    cell.remove(value)
                else:
                    value.text = cache
                body = ET.tostring(tree)
            target.writestr(name, body)
    return result.getvalue()


@pytest.mark.parametrize('cache,cache_type,formula', [
    ('2', 'n', True), ('0', 'n', True), ('', 'str', True),
    ('0', 'b', True), ('result', 'str', True), ('2', 'n', False)])
def test_readiness_accepts_stored_results_and_formula_free_files(files, cache, cache_type, formula):
    rows, create = files
    body = workbook_bytes(cache=cache, cache_type=cache_type, formula=formula)
    create('selected', body)
    report, = output_files.preflight_output_files(rows[0], ['selected', 'selected'])
    assert report == {'file_id': 'selected', 'sha256': hashlib.sha256(body).hexdigest(),
                      'check_version': 1, 'status': 'passed', 'check': 'xlsx_readiness',
                      'counts': {'sheets': 1, 'formula_cells': int(formula),
                                 'missing_formula_results': 0, 'error_cells': 0}}
    assert output_files.resolve_output_files(rows[0], ['selected'], verification=[report])[0]['file_id'] == 'selected'


@pytest.mark.parametrize('body,reason', [
    (b'not a ZIP workbook', 'could not be read'),
    (workbook_bytes(cache=None), 'B1: missing formula result'),
    (workbook_bytes(cache='', cache_type='n'), 'missing formula result'),
    (workbook_bytes(cache='#DIV/0!', cache_type='e'), 'B1: Excel error #DIV/0!'),
    (workbook_bytes(error='#REF!'), 'C1: Excel error #REF!')])
def test_readiness_errors_identify_file_and_cell_without_exposing_path(files, body, reason):
    rows, create = files
    _, _, _, path = create('selected', body)
    with pytest.raises(ValueError) as failure:
        output_files.preflight_output_files(rows[0], ['selected'])
    message = str(failure.value)
    assert 'selected (payroll.xlsx)' in message and reason in message
    assert 'new file ID' in message and str(path.parent) not in message


def test_checked_same_named_copy_cannot_validate_another_file(files):
    rows, create = files
    create('uncached', workbook_bytes(cache=None))
    create('recalculated', workbook_bytes())
    report = output_files.preflight_output_files(rows[0], ['recalculated'])
    with pytest.raises(ValueError, match='uncached .*missing formula result'):
        output_files.preflight_output_files(rows[0], ['uncached'])
    with pytest.raises(ValueError, match='changed after verification'):
        output_files.resolve_output_files(rows[0], ['uncached'], verification=report)


def test_scan_releases_read_transaction_and_detects_changed_bytes(files, monkeypatch):
    rows, create = files
    _, _, _, path = create('selected', workbook_bytes())
    original = output_files._workbook_readiness

    def inspect(*args):
        assert not db.session().in_transaction()
        counts = original(*args)
        path.write_bytes(workbook_bytes(cache='3'))
        return counts

    monkeypatch.setattr(output_files, '_workbook_readiness', inspect)
    with pytest.raises(ValueError, match='content changed'):
        output_files.preflight_output_files(rows[0], ['selected'])


def test_verification_rechecks_hash_even_if_saved_metadata_is_replaced(files):
    rows, create = files
    _, artifact, execution, path = create('selected', workbook_bytes())
    report = output_files.preflight_output_files(rows[0], ['selected'])
    body = workbook_bytes(cache='3')
    path.write_bytes(body)
    value = json.loads(artifact.content_json)
    value.update(sha256=hashlib.sha256(body).hexdigest(), file_size=len(body))
    artifact.content_json = json.dumps(value)
    result = json.loads(execution.content_json)
    result['files'][0].update(sha256=value['sha256'], file_size=value['file_size'])
    execution.content_json = json.dumps(result)
    db.session.commit()
    with pytest.raises(ValueError, match='changed after verification'):
        output_files.resolve_output_files(rows[0], ['selected'], verification=report)


def test_verification_limits_do_not_report_success(files, monkeypatch):
    rows, create = files
    body = workbook_bytes()
    create('selected', body)
    # ZIP expansion is independently limited, even if the saved bytes fit.
    monkeypatch.setattr(output_files, 'MAX_OUTPUT_BYTES', len(body) + 1)
    with pytest.raises(ValueError, match='expanded output-size limit'):
        output_files.preflight_output_files(rows[0], ['selected'])
    monkeypatch.setattr(output_files, 'MAX_OUTPUT_BYTES', 100 * 1024 * 1024)
    calls = iter([0, 31])
    monkeypatch.setattr(output_files, 'time', SimpleNamespace(monotonic=lambda: next(calls)))
    with pytest.raises(ValueError, match='exceeded its time limit'):
        output_files.preflight_output_files(rows[0], ['selected'])


@pytest.mark.parametrize('filename', ['table.csv', 'data.json'])
def test_other_output_formats_keep_integrity_checks_only(files, filename):
    rows, create = files
    _, artifact, execution, _ = create('selected', b'unchanged bytes')
    value = json.loads(artifact.content_json)
    value['filename'] = filename
    artifact.content_json = json.dumps(value)
    result = json.loads(execution.content_json)
    result['files'][0]['filename'] = filename
    execution.content_json = json.dumps(result)
    db.session.commit()
    report, = output_files.preflight_output_files(rows[0], ['selected'])
    assert report['check'] == 'integrity' and 'counts' not in report
