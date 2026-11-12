"""Saved Markdown exports retain structure and use only the trusted PDF runtime."""
import copy
import hashlib
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

from docx import Document
from docx.oxml.ns import qn
import pytest

from backend.util import document_exports as exports
from backend.domains.legal.pleading_format import DEFAULT_EXPORT_SPEC, STYLE_NUMBERED_PARAGRAPH


MARKDOWN = '''# Reconciliation report

Use **bold**, *italic*, ***both***, and [the source](https://example.test/source).
Invoice `000001` stays text.

| Invoice | Amount | Note |
| :--- | ---: | :--- |
| 000001 | 12.50 | A long explanation wraps inside its cell without losing content. |
| 000002 | 0.00 | **Unpaid** and *reviewed* |

7. First ordered item
8. Second ordered item
   - Nested bullet

> Quoted **material**.

```python
invoice_id = "000001"
print(invoice_id)
```
'''

CASE_URL = 'https://sources.example.test/opinions/347%20U.S.%20483&105221'
STATUTE_URL = 'https://uscode.house.gov/us/usc/t28/s1331/'
ACT_URL = ('https://palrb.gov/getfile.cfm?file=resources%2Fpreservation-project%2Fpltiftopdf'
           '%2F20002099%2F2004%2F0%2Fact%2F0152.pdf&view=true')
AUTHORITY_MARKDOWN = f'''# Authority links

The **[Brown *holding*]({CASE_URL})** is linked for review.

- Review [opinion **and** *reasoning*]({CASE_URL}).
  - Read [the `official section`]({STATUTE_URL}).

| Authority | Source |
| --- | --- |
| Federal statute | [Section **1331**]({STATUTE_URL}) |
| Pennsylvania act | [Act *152* PDF]({ACT_URL}) |
'''


def draft(markdown=MARKDOWN):
    return {'format_version': 2, 'source_format': 'markdown', 'title': 'Reconciliation report',
            'markdown': markdown, 'revision_id': 'revision-one', 'parent_revision_id': None,
            'export_settings': {}}


def test_markdown_export_is_exact_source_and_does_not_add_metadata_title(tmp_path):
    source = 'No added title.\r\n\r\nID 000001\r\n'
    value = draft(source)
    before = copy.deepcopy(value)
    metadata = exports.export_document(value, 'markdown', tmp_path / 'source.md')
    assert (tmp_path / 'source.md').read_bytes() == source.encode()
    assert value == before
    assert metadata == {'format': 'markdown', 'content_type': exports.CONTENT_TYPES['markdown'],
                        'filename': 'source.md', 'file_size': len(source.encode())}


def test_html_retains_tables_inline_structure_and_escapes_raw_html():
    value = draft(MARKDOWN + '\n<script>alert("no")</script>\n')
    value['title'] = '<unsafe metadata>'
    html = exports.render_html(value)
    assert '<title>&lt;unsafe metadata&gt;</title>' in html
    assert '<table>' in html and '<thead>' in html and '>000001</td>' in html
    assert '<strong>bold</strong>' in html and '<em>italic</em>' in html
    assert '<ol start="7">' in html and '<blockquote>' in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert '<h1>Reconciliation report</h1>' in html
    assert html.count('<h1>') == 1


def test_docx_has_true_wrapping_table_repeat_headers_lists_links_and_code(tmp_path):
    path = exports.render_docx(draft(), tmp_path / 'report.docx')
    document = Document(path)
    assert document.core_properties.title == 'Reconciliation report'
    assert document.paragraphs[0].text == 'Reconciliation report'
    assert document.paragraphs[0].style.name == 'Heading 1'
    assert len(document.tables) == 1
    table = document.tables[0]
    assert table.cell(1, 0).text == '000001'
    assert table.cell(1, 1).text == '12.50'
    assert table.rows[0]._tr.find('.//' + qn('w:tblHeader')) is not None
    assert table.autofit is False
    assert not table._tbl.findall('.//' + qn('w:noWrap'))
    assert len(table._tbl.findall('.//' + qn('w:cantSplit'))) == len(table.rows)
    assert all(not p.paragraph_format.keep_with_next for row in table.rows for cell in row.cells for p in cell.paragraphs)
    assert any(run.text == 'bold' and run.bold for p in document.paragraphs for run in p.runs)
    assert any(run.text == 'italic' and run.italic for p in document.paragraphs for run in p.runs)
    assert any(run.text == 'both' and run.bold and run.italic for p in document.paragraphs for run in p.runs)
    assert len(document._element.findall('.//' + qn('w:hyperlink'))) == 1
    assert any(relationship.target_ref == 'https://example.test/source' for relationship in document.part.rels.values())
    assert 'invoice_id = "000001"\nprint(invoice_id)' in '\n'.join(p.text for p in document.paragraphs)
    assert len(document._element.findall('.//' + qn('w:numPr'))) == 3
    assert any(node.get(qn('w:val')) == '7' for node in document.part.numbering_part.element.findall('.//' + qn('w:start')))
    assert not any('| Invoice |' in p.text for p in document.paragraphs)


@pytest.mark.parametrize('preset', ['general', 'court'])
def test_docx_hyperlinks_define_and_apply_character_style_without_losing_inline_formatting(tmp_path, preset):
    value = draft(AUTHORITY_MARKDOWN)
    if preset == 'court':
        value['export_settings']['court'] = copy.deepcopy(DEFAULT_EXPORT_SPEC)
        value['export_settings']['court']['structural']['require_caption_page'] = False
    before = copy.deepcopy(value)
    document = Document(exports.render_docx(value, tmp_path / 'linked.docx', preset=preset))
    style = document.styles['Hyperlink']
    assert style.element.get(qn('w:type')) == 'character'
    assert str(style.font.color.rgb) == '174EA6'
    assert style.font.underline is True
    links = document._element.findall('.//' + qn('w:hyperlink'))
    assert len(links) == 5
    assert {document.part.rels[link.get(qn('r:id'))].target_ref for link in links} == {CASE_URL, STATUTE_URL, ACT_URL}
    for link in links:
        for run in link.findall(qn('w:r')):
            properties = run.find(qn('w:rPr'))
            assert properties.find(qn('w:rStyle')).get(qn('w:val')) == style.style_id
            assert properties.find(qn('w:color')).get(qn('w:val')) == '174EA6'
            assert properties.find(qn('w:u')).get(qn('w:val')) == 'single'
    holding = next(run for run in links[0].findall(qn('w:r'))
                   if ''.join(node.text or '' for node in run.findall(qn('w:t'))) == 'holding')
    assert holding.find(qn('w:rPr')).find(qn('w:b')) is not None
    assert holding.find(qn('w:rPr')).find(qn('w:i')) is not None
    assert len(document.tables[0]._tbl.findall('.//' + qn('w:hyperlink'))) == 2
    assert value == before


@pytest.mark.skipif(os.environ.get('ZYX_PYTHON_LIVE') != '1', reason='Set ZYX_PYTHON_LIVE=1 for Docker integration.')
def test_real_pdf_runtime_retains_case_and_official_links_in_nested_formatting_and_tables(tmp_path):
    import fitz

    value = draft(AUTHORITY_MARKDOWN)
    before = copy.deepcopy(value)
    source = exports.render_docx(value, tmp_path / 'source.docx')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = exports.convert_docx_to_pdf(source, tmp_path / 'linked.pdf')
    with fitz.open(destination) as pdf:
        links = [link for page in pdf for link in page.get_links()]
        assert len(links) >= 5
        assert {link.get('uri') for link in links} == {CASE_URL, STATUTE_URL, ACT_URL}
        text = ' '.join(' '.join(page.get_text() for page in pdf).split())
        assert all(phrase in text for phrase in ['Brown holding', 'Federal statute', 'Act 152 PDF'])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert value == before


def test_court_uses_saved_spec_without_resolving_or_mutating_it(tmp_path, monkeypatch):
    from backend.domains.legal import pleading_format_resolver
    monkeypatch.setattr(pleading_format_resolver, 'resolve_format_spec_for_export', lambda *a, **kw: pytest.fail('Export called a model resolver'))
    spec = copy.deepcopy(DEFAULT_EXPORT_SPEC)
    spec['structural']['require_caption_page'] = False
    spec['layout']['page_margins_in']['left'] = 1.25
    value = draft('# Motion\n\nFirst factual paragraph.\n\nSecond factual paragraph.\n')
    value['export_settings']['court'] = spec
    before = copy.deepcopy(value)
    document = Document(exports.render_docx(value, tmp_path / 'court.docx', preset='court'))
    assert document.sections[0].left_margin.inches == 1.25
    assert [p.text for p in document.paragraphs if p.style.name == STYLE_NUMBERED_PARAGRAPH] == ['1. First factual paragraph.', '2. Second factual paragraph.']
    assert value == before
    with pytest.raises(ValueError, match='Court formatting is unavailable'):
        exports.render_docx(draft(), tmp_path / 'missing.docx', preset='court')


def test_court_caption_reuses_existing_template_and_does_not_duplicate_title(tmp_path):
    value = draft('# Reconciliation report\n\nThe body remains present.\n')
    spec = copy.deepcopy(DEFAULT_EXPORT_SPEC)
    spec['structural'].update(require_caption_page=True, caption_ready=True, require_numbered_paragraphs=False)
    spec['structural']['caption'].update(plaintiff='Alpha LLC', defendant='Beta LLC', case_number='26-123', pleading_title='Reconciliation report')
    value['export_settings']['court'] = spec
    document = Document(exports.render_docx(value, tmp_path / 'caption.docx', preset='court'))
    assert document.tables
    xml = document._element.xml
    assert 'Alpha LLC' in xml and 'Beta LLC' in xml and '26-123' in xml
    assert not any(p.text == 'Reconciliation report' for p in document.paragraphs)
    assert any('The body remains present.' in p.text for p in document.paragraphs)


def test_pdf_export_converts_the_same_docx_content_without_mutating_source(tmp_path):
    value = draft()
    before = copy.deepcopy(value)
    observed = []
    def converter(source, destination):
        document = Document(source)
        observed.append((document.paragraphs[0].text, document.tables[0].cell(1, 0).text))
        Path(destination).write_bytes(b'%PDF-1.7\ntrusted test output')
    metadata = exports.export_document(value, 'pdf', tmp_path / 'report.pdf', pdf_converter=converter)
    assert observed == [('Reconciliation report', '000001')]
    assert metadata['content_type'] == 'application/pdf'
    assert value == before


def test_pdf_runtime_uses_restricted_container_and_fixed_converter(tmp_path, monkeypatch):
    from backend.util import python_execution
    source = exports.render_docx(draft(), tmp_path / 'input.docx')
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    calls, removed = [], []
    monkeypatch.setattr(python_execution, '_docker', lambda: ['docker'])
    monkeypatch.setattr(python_execution, '_remove_container', lambda command, name: removed.append(name))
    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        output = next(arg for arg in arguments if arg.startswith('type=bind,src=') and arg.endswith(',dst=/output'))
        Path(output.removeprefix('type=bind,src=').removesuffix(',dst=/output'), 'document.pdf').write_bytes(b'%PDF-1.7\nvalid signature')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(exports.subprocess, 'run', run)
    exports.convert_docx_to_pdf(source, tmp_path / 'output.pdf')
    arguments, options = calls[0]
    assert arguments[arguments.index('--network') + 1] == 'none'
    assert '--read-only' in arguments and '--cap-drop' in arguments
    assert '/opt/runtime/document_to_pdf.py' in arguments
    assert '/opt/runtime/bootstrap.py' not in arguments
    assert any(arg.endswith(',dst=/inputs/source.docx,readonly') for arg in arguments)
    assert options['timeout'] == 128 and removed == [arguments[arguments.index('--name') + 1]]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


@pytest.mark.parametrize('failure', ['timeout', 'unavailable', 'invalid-pdf'])
def test_pdf_failures_remove_container_and_return_an_export_error(tmp_path, monkeypatch, failure):
    from backend.util import python_execution
    source = exports.render_docx(draft(), tmp_path / 'input.docx')
    removed = []
    monkeypatch.setattr(python_execution, '_docker', lambda: ['docker'])
    monkeypatch.setattr(python_execution, '_remove_container', lambda *args: removed.append(True))
    def run(arguments, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('docker', 128)
        if failure == 'unavailable':
            return SimpleNamespace(returncode=125, stderr='image unavailable', stdout='')
        output = next(arg for arg in arguments if arg.startswith('type=bind,src=') and arg.endswith(',dst=/output'))
        Path(output.removeprefix('type=bind,src=').removesuffix(',dst=/output'), 'document.pdf').write_bytes(b'not a pdf')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(exports.subprocess, 'run', run)
    with pytest.raises(exports.DocumentExportError):
        exports.convert_docx_to_pdf(source, tmp_path / 'output.pdf')
    assert removed == [True]
    assert not (tmp_path / 'output.pdf').exists()
