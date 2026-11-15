"""Optional workbook formula evaluation inside the analysis container."""
from pathlib import Path
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from zipfile import ZipFile


def _fit_text(document, selections):
    """Measure selected plain-text cells at their existing widths with Calc.

    A temporary unmerged measurement cell also handles merged headings. Only
    wrapping and insufficient row heights change; formulas/widths/styles stay put.
    """
    from com.sun.star.table.CellContentType import TEXT
    sheets = document.getSheets()
    name = '__workspace_fit_' + uuid.uuid4().hex[:12]
    sheets.insertNewByName(name, sheets.getCount())
    measure = sheets.getByName(name)
    try:
        probe = measure.getCellByPosition(0, 0)
        seen = set()
        for sheet_name, ranges in selections.items():
            sheet = sheets.getByName(sheet_name)
            for selection in ranges:
                area = sheet.getCellRangeByName(selection).getRangeAddress()
                for row in range(area.StartRow, area.EndRow + 1):
                    for col in range(area.StartColumn, area.EndColumn + 1):
                        cell = sheet.getCellByPosition(col, row)
                        cursor = sheet.createCursorByRange(cell)
                        cursor.collapseToMergedArea()
                        merged = cursor.getRangeAddress()
                        key = (sheet_name, merged.StartColumn, merged.StartRow)
                        anchor = sheet.getCellByPosition(merged.StartColumn, merged.StartRow)
                        if key in seen or anchor.getType() != TEXT:
                            continue
                        seen.add(key)
                        probe.String = anchor.String
                        for prop in ('CharFontName', 'CharHeight', 'CharWeight', 'CharPosture', 'ParaIndent'):
                            value = anchor.getPropertyValue(prop)
                            if value is not None:
                                probe.setPropertyValue(prop, value)
                        width = sum(sheet.getColumns().getByIndex(i).Width
                                    for i in range(merged.StartColumn, merged.EndColumn + 1))
                        measure.getColumns().getByIndex(0).Width = max(width, 1)
                        probe.IsTextWrapped = True
                        probe.Rows.OptimalHeight = True
                        required = probe.Rows.Height + 100  # 1 mm breathing room.
                        anchor.IsTextWrapped = True
                        current = sum(sheet.getRows().getByIndex(i).Height
                                      for i in range(merged.StartRow, merged.EndRow + 1))
                        if required > current:
                            first = sheet.getRows().getByIndex(merged.StartRow)
                            first.Height += required - current
    finally:
        sheets.removeByName(name)


def recalculate_workbook(source, destination=None, *, fit_text_ranges=None):
    """Recalculate an XLSX working copy and return its path with cached values.

    Original uploads are never saved. External links and macros are not run.
    Call only when fresh workbook-formula values are needed for the analysis.
    fit_text_ranges optionally maps sheet names to heading/summary/note ranges
    whose plain text should wrap and fit vertically at the existing widths.
    """
    import uno
    from com.sun.star.beans import PropertyValue
    from com.sun.star.document.MacroExecMode import NEVER_EXECUTE
    from com.sun.star.document.UpdateDocMode import NO_UPDATE

    source = Path(source).resolve(strict=True)
    if source.suffix.lower() != '.xlsx':
        raise ValueError('Workbook recalculation currently accepts XLSX files.')
    with ZipFile(source) as archive:
        if any(name.startswith('xl/externalLinks/externalLink') and name.endswith('.xml') for name in archive.namelist()):
            raise ValueError('Workbook formulas depend on external workbooks. Supply those dependencies before recalculating; saved external values are not refreshed.')
    work = Path('/scratch') / ('recalculate-' + uuid.uuid4().hex)
    work.mkdir()
    copied = work / 'source.xlsx'
    shutil.copyfile(source, copied)
    destination = Path(destination) if destination is not None else work / 'recalculated.xlsx'
    destination = destination.resolve()
    if destination == source or destination == copied:
        raise ValueError('Choose a new path for the recalculated workbook.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    pipe_name = 'zyx-agentic-llms-' + uuid.uuid4().hex
    command = ['soffice', '-env:UserInstallation=' + (work / 'profile').as_uri(),
               '--headless', '--nologo', '--nodefault', '--norestore', '--nofirststartwizard',
               '--accept=pipe,name=' + pipe_name + ';urp;StarOffice.ServiceManager']
    document, process = None, None

    def prop(name, value):
        item = PropertyValue()
        item.Name, item.Value = name, value
        return item

    with (work / 'libreoffice.log').open('wb') as log:
        try:
            process = subprocess.Popen(command, stdout=log, stderr=log)
            local = uno.getComponentContext()
            resolver = local.ServiceManager.createInstanceWithContext('com.sun.star.bridge.UnoUrlResolver', local)
            deadline = time.monotonic() + 20
            while True:
                try:
                    context = resolver.resolve('uno:pipe,name=' + pipe_name + ';urp;StarOffice.ComponentContext')
                    break
                except Exception:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('LibreOffice did not start for workbook recalculation.')
                    time.sleep(0.1)
            desktop = context.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop', context)
            document = desktop.loadComponentFromURL(copied.as_uri(), '_blank', 0, (
                prop('Hidden', True), prop('MacroExecutionMode', NEVER_EXECUTE), prop('UpdateDocMode', NO_UPDATE)))
            if document is None or not hasattr(document, 'calculateAll'):
                raise ValueError('LibreOffice could not load this workbook for calculation.')
            document.calculateAll()
            if fit_text_ranges:
                _fit_text(document, fit_text_ranges)
            document.storeAsURL(destination.as_uri(), (prop('FilterName', 'Calc MS Excel 2007 XML'), prop('Overwrite', False)))
            from openpyxl import load_workbook
            calculated = load_workbook(destination, data_only=True, read_only=True)
            try:
                errors = [f'{sheet.title}!{cell.coordinate}: {cell.value}' for sheet in calculated
                          for row in sheet for cell in row if cell.data_type == 'e']
            finally:
                calculated.close()
            if errors:
                print('Workbook calculation errors (use unaffected values only when appropriate): '
                      + '; '.join(errors[:50]) + (f'; {len(errors)} errors in total' if len(errors) > 50 else ''), file=sys.stderr)
            return destination
        finally:
            if document is not None:
                try:
                    document.close(True)
                except Exception:
                    document.dispose()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def _workbook_cell(workbook, reference):
    """Resolve an explicit Sheet!A1 cell, including quoted sheet names."""
    if not isinstance(reference, str) or '!' not in reference:
        raise ValueError('Use an explicit Sheet!A1 cell reference.')
    sheet, coordinate = reference.rsplit('!', 1)
    if sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    if not re.fullmatch(r'\$?[A-Za-z]{1,3}\$?[1-9][0-9]*', coordinate):
        raise ValueError('Workbook checks accept individual cells, not ranges or external references.')
    return workbook[sheet][coordinate.replace('$', '')]


def check_workbook(source, checks, *, tolerance=1e-8):
    """Check nominated input/output relationships on disposable recalculated copies.

    Each check has changes={"Inputs!B2": value} and expected={"Summary!C4": value}.
    The caller chooses expected business results; this helper never infers them,
    edits formulas to force a pass, or changes the source/delivered workbook.
    """
    from openpyxl import load_workbook
    if not isinstance(checks, list) or not checks:
        raise ValueError('Supply at least one workbook check.')
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError('Use a finite nonnegative comparison tolerance.')
    source = Path(source).resolve(strict=True)
    results = []
    for case in checks:
        if (not isinstance(case, dict) or set(case) - {'changes', 'expected'}
                or not isinstance(case.get('changes'), dict)
                or not isinstance(case.get('expected'), dict) or not case['expected']):
            raise ValueError('Each workbook check needs changes and nonempty expected cell dictionaries.')
        with tempfile.TemporaryDirectory(prefix='workbook-check-', dir='/scratch') as temp:
            working = Path(temp) / 'probe.xlsx'
            book = load_workbook(source)
            try:
                for reference, value in case['changes'].items():
                    if isinstance(value, str) and value.startswith('='):
                        raise ValueError('Workbook checks change input values, not formulas.')
                    cell = _workbook_cell(book, reference)
                    if cell.data_type == 'f':
                        raise ValueError('Workbook checks must not overwrite formula cells.')
                    cell.value = value
                # Validate all requested outputs before doing a recalculation.
                for reference in case['expected']:
                    _workbook_cell(book, reference)
                book.save(working)
            finally:
                book.close()
            calculated = recalculate_workbook(working, Path(temp) / 'calculated.xlsx')
            values = load_workbook(calculated, data_only=True)
            try:
                errors = [f'{s.title}!{c.coordinate}: {c.value}' for s in values for row in s
                          for c in row if c.data_type == 'e']
                comparisons = []
                for reference, expected in case['expected'].items():
                    actual = _workbook_cell(values, reference).value
                    numeric = all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                  for x in (actual, expected))
                    passed = (math.isfinite(actual) and math.isfinite(expected)
                              and abs(actual - expected) <= tolerance) if numeric else (
                                  type(actual) is type(expected) and actual == expected)
                    comparisons.append({'cell': reference, 'actual': actual, 'expected': expected,
                                        'passed': passed})
                results.append({'changes': case['changes'], 'comparisons': comparisons,
                                'formula_errors': errors[:50], 'formula_error_count': len(errors),
                                'passed': not errors and all(x['passed'] for x in comparisons)})
            finally:
                values.close()
    return {'passed': all(x['passed'] for x in results), 'checks': results}
