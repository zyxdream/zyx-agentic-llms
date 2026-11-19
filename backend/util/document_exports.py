"""Deterministic exports of saved Markdown revisions; no model calls at download time."""
from __future__ import annotations

from html import escape
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
import uuid

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Inches, Pt, RGBColor
from markdown_it import MarkdownIt

from backend.domains.legal.pleading_caption import create_document_with_caption
from backend.domains.legal.pleading_format import (
    STYLE_BLOCK_QUOTE, STYLE_BODY, STYLE_NUMBERED_PARAGRAPH, STYLE_TITLE,
    STYLE_TITLE1, STYLE_TITLE2, STYLE_TITLE3, apply_pleading_format_to_document,
    finalize_export_spec, get_structural_spec,
)
from backend.util.word_sanitize import sanitize_word_text


CONTENT_TYPES = {
    'markdown': 'text/markdown; charset=utf-8',
    'html': 'text/html; charset=utf-8',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'pdf': 'application/pdf',
}
EXTENSIONS = {'markdown': '.md', 'html': '.html', 'docx': '.docx', 'pdf': '.pdf'}


class DocumentExportError(RuntimeError):
    pass


def _markdown(draft):
    # The source utility also adapts legacy section drafts, without rewriting a
    # v2 revision's authoritative Markdown or inserting a metadata title.
    from backend.util.document_drafts import draft_to_markdown
    return draft_to_markdown(draft)


def _parser():
    return MarkdownIt('commonmark', {'html': False}).enable('table').enable('strikethrough')


def render_html(draft):
    title = escape(str(draft.get('title') or 'Document'))
    body = _parser().render(_markdown(draft))
    return ('<!doctype html>\n<html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title}</title><style>'
            'body{font:11pt/1.45 Arial,sans-serif;max-width:8in;margin:2rem auto;padding:0 1rem;color:#111}'
            'h1,h2,h3,h4,h5,h6{color:#111;break-after:avoid}p,li{overflow-wrap:anywhere}'
            'table{border-collapse:collapse;width:100%;table-layout:fixed;margin:1rem 0}'
            'th,td{border:1px solid #bbb;padding:.4rem;vertical-align:top;overflow-wrap:anywhere}'
            'th{background:#eee;text-align:left}thead{display:table-header-group}'
            'tr{break-inside:auto}blockquote{margin-left:1.5rem;border-left:3px solid #ccc;padding-left:1rem}'
            'pre,code{font-family:Consolas,monospace}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f5f5;padding:.75rem}'
            'a{color:#174ea6}@page{size:letter;margin:1in}@media print{body{max-width:none;margin:0;padding:0}}'
            f'</style></head><body>\n{body}</body></html>\n')


def _property(parent, tag, **attributes):
    element = OxmlElement(tag)
    for name, value in attributes.items():
        element.set(qn('w:' + name), str(value))
    parent.append(element)
    return element


def _safe_link(value):
    return bool(value) and (value.startswith('#') or urlsplit(value).scheme.lower() in {'http', 'https', 'mailto'})


def _inline(paragraph, tokens, *, bold=False):
    strong, emphasis, strike = int(bold), 0, 0
    links = []
    for token in tokens or []:
        kind = token.type
        if kind in {'strong_open', 'strong_close', 'em_open', 'em_close', 's_open', 's_close'}:
            delta = 1 if token.nesting == 1 else -1
            if kind.startswith('strong_'):
                strong += delta
            elif kind.startswith('em_'):
                emphasis += delta
            else:
                strike += delta
            continue
        if kind == 'link_open':
            address = token.attrGet('href') or ''
            hyperlink = None
            if _safe_link(address):
                hyperlink = OxmlElement('w:hyperlink')
                if address.startswith('#'):
                    hyperlink.set(qn('w:anchor'), address[1:])
                else:
                    hyperlink.set(qn('r:id'), paragraph.part.relate_to(address, RT.HYPERLINK, is_external=True))
                paragraph._p.append(hyperlink)
            links.append(hyperlink)
            continue
        if kind == 'link_close':
            if links:
                links.pop()
            continue
        if kind in {'softbreak', 'hardbreak'}:
            run = paragraph.add_run(' ' if kind == 'softbreak' else '')
            if kind == 'hardbreak':
                run.add_break()
        else:
            run = paragraph.add_run(sanitize_word_text(token.content))
        run.bold = True if strong else None
        run.italic = True if emphasis else None
        run.font.strike = True if strike else None
        if kind == 'code_inline':
            run.font.name = 'Consolas'
            run.font.size = Pt(10)
            _property(run._r.get_or_add_rPr(), 'w:shd', fill='F3F3F3')
        if links and links[-1] is not None:
            run.style = 'Hyperlink'
            run.font.color.rgb = RGBColor.from_string('174EA6')
            run.underline = True
            links[-1].append(run._r)


def _numbering(doc, *, ordered, start, depth):
    numbering = doc.part.numbering_part.element
    abstract_id = max((int(node.get(qn('w:abstractNumId'))) for node in numbering.findall(qn('w:abstractNum'))), default=-1) + 1
    num_id = max((int(node.get(qn('w:numId'))) for node in numbering.findall(qn('w:num'))), default=0) + 1
    abstract = _property(numbering, 'w:abstractNum', abstractNumId=abstract_id)
    first_number = numbering.find(qn('w:num'))
    if first_number is not None:
        numbering.remove(abstract)
        numbering.insert(numbering.index(first_number), abstract)
    level = _property(abstract, 'w:lvl', ilvl=0)
    _property(level, 'w:start', val=start)
    _property(level, 'w:numFmt', val='decimal' if ordered else 'bullet')
    _property(level, 'w:lvlText', val='%1.' if ordered else '•')
    _property(level, 'w:lvlJc', val='left')
    properties = _property(level, 'w:pPr')
    _property(properties, 'w:ind', left=720 + depth * 360, hanging=360)
    number = _property(numbering, 'w:num', numId=num_id)
    _property(number, 'w:abstractNumId', val=abstract_id)
    return num_id


def _table(doc, tokens):
    rows, cells, cell_tokens, header = [], None, None, False
    for token in tokens:
        if token.type == 'tr_open':
            cells = []
        elif token.type in {'th_open', 'td_open'}:
            cell_tokens = []
            header = token.type == 'th_open'
            alignment = token.attrGet('style') or ''
        elif token.type == 'inline' and cell_tokens is not None:
            cell_tokens.extend(token.children or [])
        elif token.type in {'th_close', 'td_close'}:
            cells.append((cell_tokens, header, alignment))
            cell_tokens = None
        elif token.type == 'tr_close':
            rows.append(cells)
    if not rows:
        return
    column_count = max(len(row) for row in rows)
    table = doc.add_table(rows=0, cols=column_count)
    table.style = 'Table Grid'
    table.autofit = False
    section = doc.sections[-1]
    width = (section.page_width - section.left_margin - section.right_margin) // column_count
    for column in table.columns:
        column.width = width
    for row_data in rows:
        row = table.add_row()
        _property(row._tr.get_or_add_trPr(), 'w:cantSplit', val='true')
        if any(is_header for _, is_header, _ in row_data):
            _property(row._tr.get_or_add_trPr(), 'w:tblHeader', val='true')
        for index, (children, is_header, alignment) in enumerate(row_data):
            cell = row.cells[index]
            cell.width = width
            properties = cell._tc.get_or_add_tcPr()
            _property(properties, 'w:vAlign', val='top')
            if is_header:
                _property(properties, 'w:shd', fill='EFEFEF')
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(4)
            paragraph.paragraph_format.space_before = Pt(4)
            paragraph.paragraph_format.line_spacing = 1.05
            paragraph.paragraph_format.first_line_indent = Inches(0)
            paragraph.paragraph_format.keep_with_next = False
            paragraph.paragraph_format.keep_together = False
            if 'right' in alignment:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            elif 'center' in alignment:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _inline(paragraph, children, bold=is_header)


def render_docx(draft, output_path, *, preset='general'):
    if preset not in {'general', 'court'}:
        raise ValueError('Choose General or Court formatting.')
    markdown = _markdown(draft)
    tokens = _parser().parse(markdown)
    structural, caption = {}, False
    if preset == 'court':
        saved = (draft.get('export_settings') or {}).get('court')
        if not isinstance(saved, dict) or not saved:
            raise ValueError('Court formatting is unavailable for this revision. Choose General formatting.')
        spec = finalize_export_spec(saved, draft_title=draft.get('title') or '')
        structural = get_structural_spec(spec)
        caption = structural.get('require_caption_page', False)
        doc = create_document_with_caption(spec, draft_title=draft.get('title') or '') if caption else Document()
        apply_pleading_format_to_document(doc, spec, preserve_page_layout=caption and structural.get('use_vertical_lines', False))
    else:
        doc = Document()
        normal = doc.styles['Normal']
        normal.font.name, normal.font.size = 'Arial', Pt(11)
        normal.paragraph_format.line_spacing = 1.15
        normal.paragraph_format.space_after = Pt(8)
        for section in doc.sections:
            section.top_margin = section.bottom_margin = section.left_margin = section.right_margin = Inches(1)
        for level in range(1, 7):
            style = doc.styles[f'Heading {level}']
            style.font.name = 'Arial'
            style.font.color.rgb = RGBColor(0, 0, 0)
            style.paragraph_format.keep_with_next = True
    if 'Hyperlink' not in doc.styles:
        # Writer 7.4 retains PDF link annotations when hyperlink runs carry
        # the standard character style, in addition to their relationship.
        hyperlink_style = doc.styles.add_style('Hyperlink', WD_STYLE_TYPE.CHARACTER)
        hyperlink_style.font.color.rgb = RGBColor.from_string('174EA6')
        hyperlink_style.font.underline = True
    doc.core_properties.title = str(draft.get('title') or '')
    index, paragraph, quote_depth, counter = 0, None, 0, 1
    lists = []
    while index < len(tokens):
        token = tokens[index]
        kind = token.type
        if kind == 'table_open':
            end = next(i for i in range(index + 1, len(tokens)) if tokens[i].type == 'table_close')
            _table(doc, tokens[index:end + 1])
            index = end + 1
            continue
        if kind == 'blockquote_open':
            quote_depth += 1
        elif kind == 'blockquote_close':
            quote_depth -= 1
        elif kind in {'bullet_list_open', 'ordered_list_open'}:
            lists.append({'num_id': _numbering(doc, ordered=kind == 'ordered_list_open', start=int(token.attrGet('start') or 1), depth=len(lists)), 'pending': False})
        elif kind in {'bullet_list_close', 'ordered_list_close'}:
            lists.pop()
        elif kind == 'list_item_open':
            lists[-1]['pending'] = True
        elif kind == 'heading_open':
            level = int(token.tag[1:])
            next_inline = tokens[index + 1]
            # A formatted caption already carries the same document title.
            if caption and index == 0 and next_inline.content.strip().casefold() == str(draft.get('title') or '').strip().casefold():
                index += 3
                continue
            style = ({1: STYLE_TITLE, 2: STYLE_TITLE1, 3: STYLE_TITLE2}.get(level, STYLE_TITLE3)
                     if preset == 'court' else f'Heading {min(level, 6)}')
            paragraph = doc.add_paragraph(style=style)
            paragraph.paragraph_format.keep_with_next = True
        elif kind == 'paragraph_open':
            style = (STYLE_BLOCK_QUOTE if quote_depth else STYLE_BODY) if preset == 'court' else 'Normal'
            paragraph = doc.add_paragraph(style=style)
            if quote_depth and preset != 'court':
                paragraph.paragraph_format.left_indent = Inches(0.35 * quote_depth)
            if lists:
                paragraph.paragraph_format.left_indent = Inches(0.5 + (len(lists) - 1) * 0.25)
                if lists[-1]['pending']:
                    properties = paragraph._p.get_or_add_pPr().get_or_add_numPr()
                    properties.get_or_add_ilvl().val = 0
                    properties.get_or_add_numId().val = lists[-1]['num_id']
                    lists[-1]['pending'] = False
            elif preset == 'court' and not quote_depth and structural.get('require_numbered_paragraphs'):
                from backend.util.markdown_docx import _should_auto_number_paragraph
                if _should_auto_number_paragraph(tokens[index + 1].content):
                    paragraph.style = STYLE_NUMBERED_PARAGRAPH
                    paragraph.add_run(f'{counter}. ')
                    counter += 1
        elif kind == 'inline' and paragraph is not None:
            _inline(paragraph, token.children)
        elif kind in {'paragraph_close', 'heading_close'}:
            paragraph = None
        elif kind in {'fence', 'code_block'}:
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.first_line_indent = Inches(0)
            paragraph.paragraph_format.line_spacing = 1
            run = paragraph.add_run(sanitize_word_text(token.content))
            run.font.name, run.font.size = 'Consolas', Pt(10)
            paragraph = None
        elif kind == 'hr':
            properties = doc.add_paragraph()._p.get_or_add_pPr()
            border = _property(properties, 'w:pBdr')
            _property(border, 'w:bottom', val='single', sz=4, color='BBBBBB')
        index += 1
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)
    return output_path


def convert_docx_to_pdf(source_path, output_path, *, timeout_seconds=120):
    """Run only the trusted Writer converter in the existing restricted image."""
    from backend.util.python_execution import IMAGE, MAX_OUTPUT_BYTES, _docker, _remove_container

    source_path, output_path = Path(source_path).resolve(strict=True), Path(output_path)
    if source_path.suffix.lower() != '.docx':
        raise ValueError('PDF conversion requires a DOCX document.')
    if timeout_seconds <= 0:
        raise ValueError('PDF conversion timeout must be positive.')
    docker, name = _docker(), 'zyx-agentic-llms-document-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='zyx-agentic-llms-document-export-') as temporary:
        work = Path(temporary)
        for directory in ('scratch', 'output'):
            (work / directory).mkdir(mode=0o777)
            (work / directory).chmod(0o777)
        args = [*docker, 'run', '--name', name, '--init', '--network', 'none', '--read-only',
                '--user', '65532:65532', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--pids-limit', '128', '--memory', '1g', '--memory-swap', '1g', '--cpus', '1',
                '--tmpfs', '/tmp:rw,noexec,nosuid,size=256m', '--env', 'HOME=/tmp',
                '--mount', f'type=bind,src={source_path},dst=/inputs/source.docx,readonly',
                '--mount', f'type=bind,src={work / "scratch"},dst=/scratch',
                '--mount', f'type=bind,src={work / "output"},dst=/output',
                os.environ.get('AGENT_PYTHON_IMAGE', IMAGE), f'{timeout_seconds:g}s',
                '/usr/bin/python3', '-B', '/opt/runtime/document_to_pdf.py',
                '/inputs/source.docx', '/output/document.pdf']
        try:
            try:
                completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout_seconds + 8)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DocumentExportError('PDF conversion is unavailable or timed out. Check Docker and the document analysis image.') from exc
            if completed.returncode:
                raise DocumentExportError('PDF conversion failed: ' + (completed.stderr or completed.stdout or 'Writer runtime unavailable.')[-4000:])
            rendered = work / 'output/document.pdf'
            if not rendered.is_file() or rendered.stat().st_size > MAX_OUTPUT_BYTES:
                raise DocumentExportError('PDF conversion did not produce a supported output file.')
            with rendered.open('rb') as stream:
                if stream.read(5) != b'%PDF-':
                    raise DocumentExportError('Writer did not return a PDF document.')
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(rendered, output_path)
        finally:
            _remove_container(docker, name)
    return output_path


def export_document(draft, format, output_path, *, preset='general', pdf_converter=None):
    """Export a pinned saved revision. The caller owns authorization and storage."""
    format = 'markdown' if format == 'md' else format
    if format not in CONTENT_TYPES:
        raise ValueError('Choose Markdown, HTML, DOCX, or PDF.')
    if preset not in {'general', 'court'}:
        raise ValueError('Choose General or Court formatting.')
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if format == 'markdown':
        output_path.write_bytes(_markdown(draft).encode('utf-8'))
    elif format == 'html':
        output_path.write_bytes(render_html(draft).encode('utf-8'))
    elif format == 'docx':
        render_docx(draft, output_path, preset=preset)
    else:
        with tempfile.TemporaryDirectory(prefix='zyx-agentic-llms-document-source-') as temporary:
            source = render_docx(draft, Path(temporary) / 'document.docx', preset=preset)
            (pdf_converter or convert_docx_to_pdf)(source, output_path)
    return {'format': format, 'content_type': CONTENT_TYPES[format], 'filename': output_path.name,
            'file_size': output_path.stat().st_size}
