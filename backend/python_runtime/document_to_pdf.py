"""Trusted DOCX-to-PDF entrypoint; never evaluates model-written Python."""
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid


def convert_document(source, destination):
    import uno
    from com.sun.star.beans import PropertyValue
    from com.sun.star.document.MacroExecMode import NEVER_EXECUTE
    from com.sun.star.document.UpdateDocMode import NO_UPDATE

    source, destination = Path(source).resolve(strict=True), Path(destination).resolve()
    if source.suffix.lower() != '.docx' or destination.suffix.lower() != '.pdf':
        raise ValueError('The document converter accepts DOCX input and PDF output.')
    work = Path('/scratch') / ('writer-' + uuid.uuid4().hex)
    work.mkdir()
    copied = work / 'source.docx'
    shutil.copyfile(source, copied)
    pipe_name = 'lexintel-writer-' + uuid.uuid4().hex
    command = ['soffice', '-env:UserInstallation=' + (work / 'profile').as_uri(),
               '--headless', '--nologo', '--nodefault', '--norestore', '--nofirststartwizard',
               '--accept=pipe,name=' + pipe_name + ';urp;StarOffice.ServiceManager']
    process, document = None, None

    def prop(name, value):
        item = PropertyValue()
        item.Name, item.Value = name, value
        return item

    destination.parent.mkdir(parents=True, exist_ok=True)
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
                        raise RuntimeError('LibreOffice Writer did not start for PDF conversion.')
                    time.sleep(0.1)
            desktop = context.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop', context)
            document = desktop.loadComponentFromURL(copied.as_uri(), '_blank', 0, (
                prop('Hidden', True), prop('ReadOnly', True), prop('MacroExecutionMode', NEVER_EXECUTE), prop('UpdateDocMode', NO_UPDATE)))
            if document is None or not document.supportsService('com.sun.star.text.TextDocument'):
                raise ValueError('LibreOffice Writer could not load the DOCX document.')
            document.storeToURL(destination.as_uri(), (prop('FilterName', 'writer_pdf_Export'), prop('Overwrite', False)))
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


if __name__ == '__main__':
    convert_document(sys.argv[1], sys.argv[2])
