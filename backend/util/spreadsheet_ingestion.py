"""Spreadsheet source extraction and bounded navigation profiles.

Original bytes remain the analytical input. The text below is a searchable,
cell-addressed representation, not a recalculated workbook or a normalized table.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import copy
import csv
from datetime import date, datetime, time, timedelta
from io import StringIO
import json
from pathlib import Path
import posixpath
from zipfile import ZipFile

from lxml import etree as ET

VERSION = 'spreadsheet-source-v1'
PROFILE_BASIS = 'Structural metadata and bounded samples; samples are not an exhaustive analysis.'
MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
DOCUMENT_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def _json_value(value):
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _type(value, data_type=None):
    if data_type == 'e':
        return 'error'
    if value is None:
        return 'blank'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, (datetime, date, time, timedelta)):
        return 'date'
    if isinstance(value, (int, float)):
        return 'number'
    return 'string'


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _short(value, limit):
    return value[:limit] + '…' if isinstance(value, str) and len(value) > limit else value


def compact_profile(profile, *, max_sheets=12, max_samples=3, max_columns=8, max_value_chars=100):
    """A navigation card; full profiles stay in the immutable parse manifest."""
    result = {key: profile[key] for key in ('format', 'sheet_count', 'encoding', 'delimiter', 'profile_basis')
              if key in profile}
    result['sheets'] = []
    sheets = profile.get('sheets', [])
    for sheet in sheets[:max_sheets]:
        item = {key: copy.deepcopy(sheet[key]) for key in (
            'name', 'state', 'used_range', 'row_count', 'column_count', 'nonempty_row_count',
            'formula_count', 'missing_formula_cache_count', 'cached_formula_results') if key in sheet}
        item['named_tables'] = copy.deepcopy(sheet.get('named_tables', [])[:8])
        item['column_types'] = copy.deepcopy(sheet.get('column_types', [])[:max_columns])
        for key, count in (('header_candidates', 2), ('samples', max_samples)):
            rows = []
            for row in sheet.get(key, [])[:count]:
                cells = [{k: _short(v, max_value_chars) for k, v in cell.items()}
                         for cell in row.get('cells', [])[:max_columns]]
                rows.append({'row': row['row'], 'cells': cells,
                             'cells_omitted': max(0, len(row.get('cells', [])) - len(cells))})
            item[key] = rows
        item['samples_complete'] = False
        result['sheets'].append(item)
    result['sheets_omitted'] = max(0, len(sheets) - len(result['sheets']))
    return result


def _profile_sheet(name, rows, *, state='visible', tables=None, merges=None, declared_range=None):
    """Consume sparse row records once; gather exact text and modest samples."""
    from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter

    items, sample_rows, observed_types = [], [], defaultdict(Counter)
    min_row = min_col = None
    max_row = max_col = 0
    nonempty_rows = formula_count = missing_cache_count = 0
    missing_cache_cells = []
    for number, cells in rows:
        if not cells:
            continue
        nonempty_rows += int(any(cell['type'] != 'blank' and cell.get('value') != '' for cell in cells))
        if len(sample_rows) < 32:
            sample_rows.append({'row': number, 'cells': copy.deepcopy(cells)})
        for cell in cells:
            row, column = coordinate_to_tuple(cell['cell'])
            min_row = row if min_row is None else min(min_row, row)
            min_col = column if min_col is None else min(min_col, column)
            max_row, max_col = max(max_row, row), max(max_col, column)
            observed_types[column][cell['type']] += 1
            if cell['type'] == 'formula':
                formula_count += 1
                if not cell['cache_present']:
                    missing_cache_count += 1
                    if len(missing_cache_cells) < 8:
                        missing_cache_cells.append(cell['cell'])
        first, last = cells[0]['cell'], cells[-1]['cell']
        cell_range = first if first == last else first + ':' + last
        # JSON escapes cell linebreaks without discarding source characters.
        text = '\n'.join(cell['cell'] + ' ' + _dump({k: v for k, v in cell.items() if k != 'cell'})
                         for cell in cells)
        items.append({'text': f'{name}!{cell_range}\n{text}', 'sheet': name, 'range': cell_range,
                      'row': number, 'origin': 'spreadsheet',
                      **({'cell': first} if len(cells) == 1 else {})})
    candidate_rows = []
    for row in sample_rows:
        texts = [cell['value'] for cell in row['cells']
                 if cell['type'] == 'string' and any(char.isalpha() for char in str(cell['value']))
                 and not str(cell['value']).lstrip().startswith('=')]
        # These are explicitly candidates, never a declaration of a table schema.
        if len(texts) >= 2:
            candidate_rows.append((len(set(texts)), row))
    candidates = [row for _, row in sorted(candidate_rows, key=lambda pair: (-pair[0], pair[1]['row']))[:3]]
    samples = sample_rows[:5]
    for candidate in candidates[:1]:
        for row in sample_rows:
            if row['row'] >= candidate['row'] and row not in samples:
                samples.append(row)
                if len(samples) >= 8:
                    break
    used = (f'{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{max_row}'
            if min_row is not None else None)
    profile = {'name': name, 'state': state, 'used_range': used, 'declared_range': declared_range,
               'row_count': max_row, 'column_count': max_col, 'nonempty_row_count': nonempty_rows,
               'named_tables': tables or [], 'merged_ranges': merges or [],
               'header_candidates': candidates, 'samples': samples,
               'column_types': [{'column': get_column_letter(column), 'observed_types': dict(counts)}
                                for column, counts in sorted(observed_types.items())],
               'formula_count': formula_count, 'missing_formula_cache_count': missing_cache_count,
               'cached_formula_results': 'Original cached values; not recalculated or verified fresh.'}
    heading = {'text': f'Sheet: {name}\nState: {state}\nUsed range: {used or "empty"}'
                       + ('\nMerged ranges: ' + ', '.join(merges) if merges else ''),
               'heading': name, 'level': 1, 'sheet': name, 'origin': 'spreadsheet'}
    gaps = ([{'code': 'formula_cache_missing', 'sheet': name, 'cells': missing_cache_cells,
              'count': missing_cache_count,
              'message': 'Some formulas have no saved result. Formula text is retained; evaluate formulas when needed.'}]
            if missing_cache_count else [])
    return [heading, *items], profile, gaps


def _worksheet_paths(archive):
    relationships = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'),
                                  parser=ET.XMLParser(resolve_entities=False, no_network=True))
    targets = {r.get('Id'): r.get('Target') for r in relationships
               if not r.get('TargetMode') == 'External'}
    workbook = ET.fromstring(archive.read('xl/workbook.xml'),
                            parser=ET.XMLParser(resolve_entities=False, no_network=True))
    result = {}
    for sheet in workbook.findall(f'{{{MAIN}}}sheets/{{{MAIN}}}sheet'):
        target = targets.get(sheet.get(f'{{{DOCUMENT_REL}}}id'))
        if target:
            name = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
            if name.startswith('xl/'):
                result[sheet.get('name')] = name
    return result


def _cached_value(raw, kind, cell, epoch):
    from openpyxl.styles.numbers import is_date_format, is_timedelta_format
    from openpyxl.utils.datetime import from_excel

    if raw is None:
        return '' if kind == 'str' else None
    if kind == 'b':
        return raw == '1'
    if kind in {'str', 'e', 'inlineStr'}:
        return raw
    if kind == 'd':
        return raw
    try:
        value = float(raw) if any(char in raw for char in '.Ee') else int(raw)
        if is_date_format(cell.number_format):
            return _json_value(from_excel(value, epoch, timedelta=is_timedelta_format(cell.number_format)))
        return value
    except (ValueError, OverflowError):
        return raw


def _xlsx_sheet_rows(archive, xml_path, sheet, epoch):
    """Visit stored XML cells, not max_row × max_column padded rectangles."""
    with archive.open(xml_path) as source:
        for _, row_node in ET.iterparse(source, events=('end',), tag=f'{{{MAIN}}}row',
                                       resolve_entities=False, no_network=True):
            cells = []
            for node in row_node.findall(f'{{{MAIN}}}c'):
                address = node.get('r')
                if not address:
                    continue
                cell = sheet[address]
                formula_node = node.find(f'{{{MAIN}}}f')
                if formula_node is not None:
                    formula = cell.value
                    if not isinstance(formula, str):
                        formula = getattr(formula, 'text', None) or formula_node.text or '[special formula]'
                    if not formula.startswith(('=', '[')):
                        formula = '=' + formula
                    cached = node.find(f'{{{MAIN}}}v')
                    # Empty <v/> means no cache, whereas a cached string can be empty.
                    present = cached is not None and (cached.text is not None or node.get('t') == 'str')
                    record = {'cell': address, 'type': 'formula', 'formula': formula,
                              'cached_value': _cached_value(cached.text, node.get('t'), cell, epoch) if present else None,
                              'cached_storage_type': node.get('t', 'n'), 'cache_present': present}
                else:
                    value = cell.value
                    if value is None:
                        continue
                    record = {'cell': address, 'type': _type(value, cell.data_type), 'value': _json_value(value)}
                if cell.number_format != 'General':
                    record['number_format'] = cell.number_format
                cells.append(record)
            if cells:
                yield int(row_node.get('r') or sheet[cells[0]['cell']].row), cells
            row_node.clear()
            while row_node.getprevious() is not None:
                del row_node.getparent()[0]


def _xlsx(path):
    from openpyxl import load_workbook

    # Normal loading exposes named tables, merged ranges, and decoded typed cells.
    # The XML traversal below determines which coordinates to access, avoiding a
    # huge rectangular iteration caused by formatting far beyond the actual data.
    workbook = load_workbook(path, read_only=False, data_only=False, keep_links=False)
    items, sheets, gaps, outcomes = [], [], [], []
    try:
        with ZipFile(path) as archive:
            paths = _worksheet_paths(archive)
            for sheet in workbook.worksheets:
                if sheet.title not in paths:
                    raise ValueError('A worksheet source could not be located')
                table_info = [{'name': name, 'range': sheet.tables[name].ref} for name in sheet.tables]
                sheet_items, profile, sheet_gaps = _profile_sheet(
                    sheet.title, _xlsx_sheet_rows(archive, paths[sheet.title], sheet, workbook.epoch),
                    state=sheet.sheet_state, tables=table_info,
                    merges=[str(value) for value in sheet.merged_cells.ranges],
                    declared_range=sheet.calculate_dimension())
                items.extend(sheet_items)
                sheets.append(profile)
                gaps.extend(sheet_gaps)
                outcomes.append({'sheet': sheet.title, 'status': 'partial' if sheet_gaps else 'native'})
            if any(name.startswith(('xl/media/', 'xl/embeddings/')) for name in archive.namelist()):
                gaps.append({'code': 'spreadsheet_embedded_content',
                             'message': 'Embedded images and objects are not interpreted; worksheet cells are retained.'})
    finally:
        workbook.close()
    return items, {'format': 'xlsx', 'sheet_count': len(sheets), 'sheets': sheets,
                   'profile_basis': PROFILE_BASIS}, outcomes, gaps


def _csv(path):
    from openpyxl.utils import get_column_letter

    raw = Path(path).read_bytes()
    gaps = []
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        encoding = 'utf-16'
    else:
        encoding = 'utf-8-sig'
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = 'cp1252'
        text = raw.decode(encoding)
        gaps.append({'code': 'encoding_assumed', 'message': 'CSV encoding was inferred as Windows-1252.'})
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=',;\t|')
        delimiter = dialect.delimiter
    except csv.Error:
        # A title above a table or ragged records can defeat Sniffer. Compare
        # decoded record widths rather than counting separators inside quotes.
        scores = {}
        for candidate in ',;\t|':
            widths = [len(row) for _, row in zip(range(32), csv.reader(StringIO(text[:65536]), delimiter=candidate))]
            scores[candidate] = sum(width > 1 for width in widths)
        delimiter = max(scores, key=scores.get)
        dialect = csv.excel
    reader = csv.reader(StringIO(text, newline=''), dialect=dialect, delimiter=delimiter)
    def rows():
        for number, row in enumerate(reader, 1):
            # CSV values stay strings, including leading zeros and formula-like text.
            cells = [{'cell': f'{get_column_letter(column)}{number}', 'type': 'string', 'value': value}
                     for column, value in enumerate(row, 1)]
            if not cells:
                cells = [{'cell': f'A{number}', 'type': 'blank', 'value': ''}]
            yield number, cells
    items, sheet, _ = _profile_sheet('CSV', rows())
    return items, {'format': 'csv', 'sheet_count': 1, 'sheets': [sheet], 'encoding': encoding,
                   'delimiter': delimiter, 'profile_basis': PROFILE_BASIS}, [{'sheet': 'CSV', 'status': 'native'}], gaps


def parse_spreadsheet(path, filename=None):
    suffix = Path(filename or path).suffix.lower()
    if suffix == '.xlsx':
        return _xlsx(path)
    if suffix == '.csv':
        return _csv(path)
    raise ValueError('Unsupported spreadsheet format')
