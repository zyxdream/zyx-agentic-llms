"""Optional workbook formula evaluation inside the analysis container."""
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from zipfile import ZipFile


def recalculate_workbook(source, destination=None):
    """Recalculate an XLSX working copy and return its path with cached values.

    Original uploads are never saved. External links and macros are not run.
    Call only when fresh workbook-formula values are needed for the analysis.
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
    pipe_name = 'lexintel-' + uuid.uuid4().hex
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
