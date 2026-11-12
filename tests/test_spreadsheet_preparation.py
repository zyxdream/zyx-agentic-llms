"""Spreadsheet ingestion preserves evidence and prepares bounded navigation."""
from datetime import date
import hashlib
import io
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import httpx
from openai import OpenAI
from openpyxl import Workbook
from openpyxl.worksheet.table import Table
from pydantic import ValidationError
import pytest
from werkzeug.datastructures import FileStorage

from backend.database import db
from backend.models import DocumentPreparation, DocumentPreparedArtifact, UploadedFiles
from backend.util import attachment_parser as parser
from backend.util import document_preparation as prep
from backend.util import preparation_summary as summaries
from backend.util.spreadsheet_ingestion import compact_profile
from backend import document_preparation_tasks as tasks
from tests.test_document_preparation import app, prepared_provider, run


def workbook_bytes(*, sparse=False):
    wb = Workbook()
    sheet = wb.active
    sheet.title = 'Invoice ledger'
    sheet['A1'] = 'Ledger title'
    sheet.merge_cells('A1:C1')
    sheet.append([])
    for address, value in {'A4': 'Invoice', 'B4': 'Amount', 'C4': 'Paid',
                           'A5': '000123', 'B5': 50.25, 'C5': True,
                           'A6': '000124', 'B6': -10.25, 'C6': False,
                           'B7': '=SUM(B5:B6)', 'D5': date(2026, 9, 1)}.items():
        sheet[address] = value
    sheet['B5'].number_format = '$#,##0.00'
    sheet.add_table(Table(displayName='Invoices', ref='A4:C6'))
    if sparse:
        sheet['XFD1048576'].number_format = '0.00'
    hidden = wb.create_sheet('Payment detail')
    hidden.sheet_state = 'hidden'
    hidden.append(['Reference', 'Received'])
    hidden.append(['000123', 25])
    output = io.BytesIO()
    wb.save(output)
    wb.close()
    return output.getvalue()


def write_workbook(tmp_path, **kwargs):
    path = tmp_path / 'ledger.xlsx'
    path.write_bytes(workbook_bytes(**kwargs))
    return path


def inject_cached_result(data, value='40'):
    output = io.BytesIO()
    with ZipFile(io.BytesIO(data)) as source, ZipFile(output, 'w', ZIP_DEFLATED) as target:
        for name in source.namelist():
            body = source.read(name)
            if name == 'xl/worksheets/sheet1.xml':
                body = body.replace(b'<f>SUM(B5:B6)</f><v></v>',
                                    f'<f>SUM(B5:B6)</f><v>{value}</v>'.encode())
            target.writestr(name, body)
    return output.getvalue()


def test_xlsx_preserves_cells_formulas_types_structure_and_original_bytes(tmp_path):
    path = write_workbook(tmp_path)
    before = path.read_bytes()
    manifest = parser.parse_attachment(path)
    profile = manifest['spreadsheet']
    sheet = profile['sheets'][0]
    assert path.read_bytes() == before
    assert profile['format'] == 'xlsx' and profile['sheet_count'] == 2
    assert sheet['used_range'] == 'A1:D7'
    assert sheet['header_candidates'][0]['row'] == 4
    assert sheet['named_tables'] == [{'name': 'Invoices', 'range': 'A4:C6'}]
    assert sheet['merged_ranges'] == ['A1:C1']
    assert profile['sheets'][1]['state'] == 'hidden'
    for expected in ('000123', '50.25', '$#,##0.00', '2026-09-01', '=SUM(B5:B6)',
                     '"cache_present":false', 'Payment detail!A2:B2'):
        assert expected in manifest['text']
    assert sheet['formula_count'] == sheet['missing_formula_cache_count'] == 1
    assert manifest['extraction']['gaps'][0]['code'] == 'formula_cache_missing'
    row = next(block for block in manifest['blocks'] if block.get('sheet') == 'Invoice ledger' and block.get('row') == 5)
    assert row['range'] == 'A5:D5'
    assert manifest['text'][row['start']:row['end']].startswith('Invoice ledger!A5:D5')


def test_cached_formula_result_is_preserved_without_recalculation(tmp_path):
    path = tmp_path / 'cached.xlsx'
    # Deliberately stale value, to prove parsing does not claim to recalculate.
    path.write_bytes(inject_cached_result(workbook_bytes(), '999'))
    manifest = parser.parse_attachment(path)
    assert '"cached_value":999' in manifest['text']
    sheet = manifest['spreadsheet']['sheets'][0]
    assert sheet['missing_formula_cache_count'] == 0
    assert 'not recalculated' in sheet['cached_formula_results']
    assert manifest['extraction']['status'] == 'complete'


def test_formatting_far_beyond_data_does_not_expand_the_cell_rectangle(tmp_path, monkeypatch):
    path = write_workbook(tmp_path, sparse=True)
    from openpyxl.worksheet.worksheet import Worksheet
    monkeypatch.setattr(Worksheet, 'iter_rows', lambda *a, **kw: pytest.fail('Padded rectangular iteration'))
    manifest = parser.parse_attachment(path)
    assert manifest['spreadsheet']['sheets'][0]['used_range'] == 'A1:D7'
    assert manifest['spreadsheet']['sheets'][0]['declared_range'] == 'A1:XFD1048576'
    assert len(manifest['text']) < 5000


def test_csv_preserves_leading_zeros_quotes_multiline_blanks_and_formula_like_text(tmp_path):
    path = tmp_path / 'payments.csv'
    path.write_text('Invoice;Amount;Note\r\n000123;25;"first; second\nnext line"\r\n\r\n000124;;=SUM(B2:B3)\r\n', encoding='utf-8-sig')
    manifest = parser.parse_attachment(path)
    profile = manifest['spreadsheet']
    assert profile['encoding'] == 'utf-8-sig' and profile['delimiter'] == ';'
    assert profile['sheets'][0]['row_count'] == 4
    assert profile['sheets'][0]['formula_count'] == 0
    for value in ('000123', 'first; second\\nnext line', 'CSV!A3', '=SUM(B2:B3)'):
        assert value in manifest['text']
    assert '"type":"string","value":"=SUM(B2:B3)"' in manifest['text']
    assert manifest['extraction']['status'] == 'complete'


@pytest.mark.parametrize('encoding', ['utf-16', 'cp1252'])
def test_csv_encoding_is_recorded_and_assumptions_are_visible(tmp_path, encoding):
    path = tmp_path / 'encoded.csv'
    path.write_bytes('Name,Amount\nCafé,12\n'.encode(encoding))
    manifest = parser.parse_attachment(path)
    assert 'Café' in manifest['text']
    assert manifest['spreadsheet']['encoding'] == encoding
    assert bool(manifest['extraction']['gaps']) is (encoding == 'cp1252')


def test_csv_ragged_rows_and_title_still_detect_tab_delimiter(tmp_path):
    path = tmp_path / 'ragged.csv'
    path.write_text('Report title\nID\tAmount\n00001\t20\n00002\t30\textra\n')
    profile = parser.parse_attachment(path)['spreadsheet']
    assert profile['delimiter'] == '\t'
    assert profile['sheets'][0]['header_candidates'][0]['row'] == 2
    assert profile['sheets'][0]['column_count'] == 3


def test_headerless_numeric_csv_keeps_unknown_schema_and_original_strings(tmp_path):
    path = tmp_path / 'headerless.csv'
    path.write_text('000001,25\n000002,30\n')
    manifest = parser.parse_attachment(path)
    sheet = manifest['spreadsheet']['sheets'][0]
    assert sheet['header_candidates'] == []
    assert sheet['samples'][0]['cells'][0]['value'] == '000001'
    assert sheet['row_count'] == 2


@pytest.mark.parametrize('source, expected_row', [('\n\nID,Amount\n0001,20\n', 3), ('\n\n', None)])
def test_blank_csv_rows_and_empty_workbooks_do_not_invent_a_header(tmp_path, source, expected_row):
    path = tmp_path / 'sparse.csv'
    path.write_text(source)
    sheet = parser.parse_attachment(path)['spreadsheet']['sheets'][0]
    assert ([row['row'] for row in sheet['header_candidates']] == [expected_row]
            if expected_row else sheet['header_candidates'] == [])


def test_compact_profile_labels_omissions_and_does_not_mutate_full_profile(tmp_path):
    full = parser.parse_attachment(write_workbook(tmp_path))['spreadsheet']
    before = json.dumps(full, sort_keys=True)
    compact = compact_profile(full, max_sheets=1, max_samples=1, max_columns=1, max_value_chars=4)
    assert compact['sheets_omitted'] == 1
    assert compact['sheets'][0]['samples_complete'] is False
    assert compact['sheets'][0]['header_candidates'][0]['cells_omitted'] == 2
    assert compact['sheets'][0]['header_candidates'][0]['cells'][0]['value'] == 'Invo…'
    assert json.dumps(full, sort_keys=True) == before


def test_spreadsheet_summary_uses_profile_only_and_reuses_checkpoint(tmp_path, monkeypatch):
    wb = Workbook()
    wb.active.append(['ID', 'Amount'])
    for number in range(100):
        wb.active.append([f'identifier-{number}', number])
    wb.active['A100'] = 'LATE_SOURCE_VALUE_NOT_IN_NAVIGATION_SAMPLES'
    path = tmp_path / 'large.xlsx'
    wb.save(path)
    wb.close()
    manifest = parser.parse_attachment(path)
    captured, saved = [], {}
    def provider(prompt, schema, **kwargs):
        captured.append(prompt)
        return prepared_provider(prompt, schema)
    def checkpoint(key, value=None):
        if value is not None and '_work' not in value:
            saved[key] = value
        return saved.get(key)
    monkeypatch.setattr(summaries, '_call', provider)
    metadata, coverage = summaries.summarize_document(manifest['text'], path.name, checkpoint,
        source_hash=hashlib.sha256(path.read_bytes()).hexdigest(), manifest=manifest, max_calls=1)
    assert len(captured) == 1
    assert 'LATE_SOURCE_VALUE_NOT_IN_NAVIGATION_SAMPLES' not in captured[0]
    assert 'LATE_SOURCE_VALUE_NOT_IN_NAVIGATION_SAMPLES' in manifest['text']
    assert metadata['schema_version'] == 2 and metadata['document_kind'] == 'spreadsheet'
    assert metadata['summary_exhaustive'] is False and metadata['section_keys'] == []
    assert coverage['summary_basis'] == 'structural_profile_and_samples'
    assert coverage['source_characters'] == len(manifest['text'])
    assert coverage['covered_characters'] == 0
    assert coverage['extraction']['status'] == 'complete'
    assert summaries.summarize_document(manifest['text'], path.name, checkpoint,
        source_hash=hashlib.sha256(path.read_bytes()).hexdigest(), manifest=manifest, max_calls=0)[0] == metadata
    assert len(captured) == 1


def test_spreadsheet_navigation_sends_existing_key_point_bound_through_sdk(tmp_path, monkeypatch):
    from backend.ai import base

    path = write_workbook(tmp_path)
    manifest = parser.parse_attachment(path)
    value = {**prepared_provider('', summaries.PreparedSummary),
             'headline': 'Invoice ledger with payment detail', 'document_kind': 'spreadsheet',
             'preview': 'Invoice ledger and hidden payment detail; formulas have no cached results.',
             'detailed_digest': 'The workbook contains invoice and payment sheets. Header candidates are hints.',
             'key_points': [{'text': text} for text in (
                 'Invoice ledger has a title above the candidate header.',
                 'Invoice identifiers are text with leading zeroes.',
                 'The invoice sheet contains a named table.',
                 'Payment detail is hidden.',
                 'The formula result cache is missing.')]}
    with pytest.raises(ValidationError, match='at most 5 items'):
        summaries.PreparedSummary(**{**value, 'key_points': value['key_points'] + [{'text': 'Sixth point'}]})
    requests, saved = [], {}

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        output_schema = body['text']['format']
        assert output_schema['type'] == 'json_schema' and output_schema['strict'] is True
        assert output_schema['schema']['properties']['key_points']['maxItems'] == 5
        assert 'at most five key points' in body['input'][1]['content']
        return httpx.Response(200, json={
            'id': 'resp_navigation', 'object': 'response', 'created_at': 0,
            'model': summaries.MODEL, 'status': 'completed',
            'output': [{'id': 'msg_navigation', 'type': 'message', 'role': 'assistant',
                        'status': 'completed', 'content': [{'type': 'output_text',
                        'text': json.dumps(value), 'annotations': []}]}],
            'usage': {'input_tokens': 20, 'output_tokens': 20, 'total_tokens': 40}})

    def checkpoint(key, record=None):
        if record is not None and '_work' not in record:
            saved[key] = record
        return saved.get(key)

    monkeypatch.setattr(base, 'record_scoped_llm_response', lambda *a, **kw: None)
    monkeypatch.setattr(base, 'record_guided_llm_response', lambda *a, **kw: None)
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = OpenAI(api_key='test-only', base_url='http://127.0.0.1/v1', http_client=transport, max_retries=0)
        monkeypatch.setattr(base, 'get_openai_client', lambda: client)
        metadata, coverage = summaries.summarize_document(manifest['text'], path.name, checkpoint,
            source_hash=hashlib.sha256(path.read_bytes()).hexdigest(), manifest=manifest, max_calls=1)
        assert metadata['key_points'] == value['key_points']
        assert metadata['summary'] == value['preview']
        assert metadata['spreadsheet']['sheet_count'] == 2
        assert metadata['summary_basis'] == coverage['summary_basis'] == 'structural_profile_and_samples'
        assert metadata['summary_exhaustive'] is False
        assert coverage['usage'] == {'calls': 1, 'total_tokens': 40}
        assert summaries.summarize_document(manifest['text'], path.name, checkpoint,
            source_hash=hashlib.sha256(path.read_bytes()).hexdigest(), manifest=manifest, max_calls=0) == (metadata, coverage)
    assert len(requests) == len(saved) == 1


def test_spreadsheet_parse_checkpoint_is_reused(tmp_path, monkeypatch):
    from backend.util import spreadsheet_ingestion
    path = write_workbook(tmp_path)
    saved = {}
    def checkpoint(key, value=None):
        if value is not None:
            saved[key] = value
        return saved.get(key)
    first = parser.parse_attachment(path, checkpoint=checkpoint, max_units=1)
    monkeypatch.setattr(spreadsheet_ingestion, 'parse_spreadsheet', lambda *a: pytest.fail('Parsed spreadsheet repeated'))
    assert parser.parse_attachment(path, checkpoint=checkpoint, max_units=0) == first


def test_full_spreadsheet_preparation_persists_profile_and_cell_evidence(app, monkeypatch):
    monkeypatch.setattr(tasks, 'parse_attachment', parser.parse_attachment)
    data = workbook_bytes()
    group = prep.admit_group(1, 'chat', 'draft-1', 'spreadsheet', [
        {'client_upload_id': 'sheet', 'filename': 'ledger.xlsx', 'file_size': len(data)}])
    entry = prep.stage_transfer(prep.owned_group(group['group_id'], 1), 'sheet',
                               FileStorage(stream=io.BytesIO(data), filename='ledger.xlsx'))
    assert run(entry)['ok']
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    prepared = db.session.get(DocumentPreparedArtifact, doc.artifact_id)
    metadata = json.loads(prepared.result_json)
    manifest = json.loads(prepared.manifest_json)
    assert manifest['spreadsheet']['sheets'][0]['formula_count'] == 1
    assert metadata['spreadsheet']['format'] == 'xlsx'
    assert json.loads(prepared.card_json)['summary_exhaustive'] is False
    assert json.loads(prepared.card_json)['spreadsheet']['sheet_count'] == 2
    assert Path(doc.source_key).read_bytes() == data
    assert db.session.get(UploadedFiles, doc.uploaded_file_id).page_count == 0
    assert '000123' in db.session.get(UploadedFiles, doc.uploaded_file_id).content
    material = summaries.source_material({**manifest, 'text': db.session.get(UploadedFiles, doc.uploaded_file_id).content},
                                        {'start': 0, 'end': len(db.session.get(UploadedFiles, doc.uploaded_file_id).content)})
    assert any(block['location'].get('sheet') == 'Invoice ledger' and block['location'].get('range') == 'A5:D5'
               for block in material)
