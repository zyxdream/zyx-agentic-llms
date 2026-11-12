import logging
from docx.enum.text import WD_LINE_SPACING
from docx.shared import Pt
from docx.oxml.ns import qn
from backend.util.word_sanitize import sanitize_word_text
logger = logging.getLogger(__name__)

def remove_list_style_stubs(doc):
    paras_to_remove = []
    for para in doc.paragraphs:
        logger.info('remove_list_style_stubs paragraph=%s', para.text)
        stripped = para.text.strip()
        if stripped.lower() in ['first numbered paragraph', 'second numbered paragraph']:
            paras_to_remove.append(para)
        elif stripped.upper().startswith('DATED:'):
            paras_to_remove.append(para)
    for para in paras_to_remove:
        p = para._element
        p.getparent().remove(p)
        p._p = p._element = None
    logger.info('remove_list_style_stubs removed_count=%s', len(paras_to_remove))

def _delete_paragraph(paragraph) -> None:
    element = paragraph._element
    element.getparent().remove(element)

def _paragraph_element_text(p_elem) -> str:
    return ''.join(p_elem.itertext())

def _find_caption_end_body_index(body_children) -> int | None:
    """Return body index of last caption element (pleading title para or Case No. table)."""
    table_end_idx = None
    for (i, child) in enumerate(body_children):
        if child.tag != qn('w:p'):
            if child.tag == qn('w:tbl') and 'Case No.:' in ''.join(child.itertext()):
                table_end_idx = i
            continue
        text = _paragraph_element_text(child).strip()
        if text.startswith('Case No.:'):
            for j in range(i + 1, len(body_children)):
                if body_children[j].tag == qn('w:p'):
                    return j
            return i
    return table_end_idx

def _trim_caption_template_tail(doc) -> None:
    """Remove sample body content, tables, and empty paras after pleading title."""
    body = doc.element.body
    children = list(body)
    end_idx = _find_caption_end_body_index(children)
    if end_idx is None:
        return
    for child in children[end_idx + 1:]:
        if child.tag == qn('w:sectPr'):
            continue
        body.remove(child)

def _single_line_text(text: str) -> str:
    """Collapse newlines so placeholder substitution does not add extra paragraphs."""
    text = sanitize_word_text(text or '')
    return ' '.join((line.strip() for line in text.splitlines() if line.strip()))

def _clear_paragraph_runs(paragraph) -> None:
    for run in list(paragraph.runs):
        run._element.getparent().remove(run._element)

def _set_paragraph_lines_preserve_font(paragraph, lines: list[str]) -> None:
    """Write multiple lines in one paragraph using soft line breaks."""
    snap = _font_snapshot(paragraph.runs[0]) if paragraph.runs else {}
    _clear_paragraph_runs(paragraph)
    if not lines:
        return
    first = paragraph.add_run(lines[0])
    _apply_font_snapshot(first, snap)
    for line in lines[1:]:
        break_run = paragraph.add_run()
        break_run.add_break()
        text_run = paragraph.add_run(line)
        _apply_font_snapshot(text_run, snap)

def _apply_caption_line_spacing(paragraph, *, double: bool=False) -> None:
    pf = paragraph.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    spacing_pt = _CAPTION_DOUBLE_LINE_SPACING_PT if double else _CAPTION_SINGLE_LINE_SPACING_PT
    pf.line_spacing = Pt(spacing_pt)
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)

def _court_name_paragraph_indices(doc) -> set[int]:
    """Return paragraph indices for the two court-name lines on the caption page."""
    indices: set[int] = set()
    for (i, para) in enumerate(doc.paragraphs):
        text = para.text.strip().upper()
        if 'DISTRICT COURT' in text:
            indices.add(i)
            if i + 1 < len(doc.paragraphs) and doc.paragraphs[i + 1].text.strip():
                indices.add(i + 1)
            break
    return indices

def _normalize_caption_page_layout(doc, *, blank_lines_before_court: int=2) -> None:
    """Double spacing on court-name lines only; single spacing elsewhere on caption page."""
    attorney_idx = None
    court_idx = None
    for (i, para) in enumerate(doc.paragraphs):
        text = para.text.strip()
        if attorney_idx is None and text.startswith('Attorney for'):
            attorney_idx = i
        if attorney_idx is not None and i > attorney_idx and ('DISTRICT COURT' in text.upper()):
            court_idx = i
            break
    if attorney_idx is not None and court_idx is not None:
        empty_between = []
        for i in range(attorney_idx + 1, court_idx):
            if not doc.paragraphs[i].text.strip():
                empty_between.append(doc.paragraphs[i])
        while len(empty_between) > blank_lines_before_court:
            _delete_paragraph(empty_between.pop())
    court_indices = _court_name_paragraph_indices(doc)
    for (i, para) in enumerate(doc.paragraphs):
        _apply_caption_line_spacing(para, double=i in court_indices)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    _apply_caption_line_spacing(para, double=False)
replacements = {'(Name&Contact)': '(your name & contact info)', 'Attorneys for Plaintiff': 'Attorney for Plaintiff', 'United States District Court': '\tUnited States District Court', 'Southern District of California': '\tSouthern District of California', 'UNITED STATES DISTRICT COURT': 'UNITED STATES DISTRICT COURT', 'SOUTHERN DISTRICT OF CALIFORNIA': 'SOUTHERN DISTRICT OF CALIFORNIA', 'Plaintiff_str': 'Plaintiff/Plaintiffs', 'Defendant_str': 'Defendant/Defendants', 'Plaintiff(s)': 'Plaintiff(s)', 'Defendant(s)': 'Defendant(s)', '(Plaintiff’s Name)': '(Plaintiff’s Name)', '(Defendant’s Name)': '(Defendant’s Name)', '(Insert Case Number)': '___________', 'TITLE OF PLEADING': '\tTITLE OF PLEADING'}
_CAPTION_MULTILINE_PLACEHOLDERS = frozenset({'(Name&Contact)'})
_CAPTION_SINGLE_LINE_SPACING_PT = 12
_CAPTION_DOUBLE_LINE_SPACING_PT = 24

def _font_snapshot(run):
    """Capture run font properties for reuse after placeholder substitution."""
    return {'name': run.font.name, 'size': run.font.size, 'bold': run.font.bold, 'italic': run.font.italic, 'underline': run.font.underline}

def _apply_font_snapshot(run, snap: dict) -> None:
    if snap.get('name'):
        run.font.name = snap['name']
    if snap.get('size') is not None:
        run.font.size = snap['size']
    if snap.get('bold') is not None:
        run.font.bold = snap['bold']
    if snap.get('italic') is not None:
        run.font.italic = snap['italic']
    if snap.get('underline') is not None:
        run.font.underline = snap['underline']

def _replace_in_paragraph_preserve_font(paragraph, placeholder: str, replacement: str, *, multiline: bool=False) -> bool:
    """Replace placeholder text in a paragraph without changing run font/size."""
    if placeholder not in paragraph.text:
        return False
    replacement = sanitize_word_text(replacement)
    if multiline and '\n' in replacement and (paragraph.text.strip() == placeholder.strip()):
        lines = [line.strip() for line in replacement.splitlines() if line.strip()]
        if lines:
            _set_paragraph_lines_preserve_font(paragraph, lines)
            return True
    if '\n' in replacement:
        replacement = _single_line_text(replacement)
    run_starts: list[tuple] = []
    combined = ''
    for run in paragraph.runs:
        run_starts.append((run, len(combined)))
        combined += run.text
    start_idx = combined.index(placeholder)
    end_idx = start_idx + len(placeholder)
    start_run = end_run = None
    start_off = end_off = 0
    for (run, run_start) in run_starts:
        run_end = run_start + len(run.text)
        if start_run is None and run_start <= start_idx < run_end:
            start_run = run
            start_off = start_idx - run_start
        if run_start < end_idx <= run_end:
            end_run = run
            end_off = end_idx - run_start
            break
    if start_run is None or end_run is None:
        return False
    snap = _font_snapshot(start_run)
    if start_run is end_run:
        text = start_run.text
        start_run.text = text[:start_off] + replacement + text[end_off:]
        _apply_font_snapshot(start_run, snap)
        return True
    start_run.text = start_run.text[:start_off] + replacement
    _apply_font_snapshot(start_run, snap)
    clearing = False
    for (run, _run_start) in run_starts:
        if run is start_run:
            clearing = True
            continue
        if run is end_run:
            run.text = run.text[end_off:]
            break
        if clearing:
            run.text = ''
    return True

def replace_placeholders(doc, replacements):
    """
    Replace placeholders in the document with actual values.

    Args:
        doc: The loaded Document object.
        replacements: A dictionary containing placeholders as keys and replacement text as values.
    """
    for paragraph in doc.paragraphs:
        for (placeholder, replacement) in replacements.items():
            if placeholder not in paragraph.text:
                continue
            logger.info('replace_placeholders paragraph=%s', paragraph.text)
            sanitized_replacement = sanitize_word_text(replacement)
            multiline = placeholder in _CAPTION_MULTILINE_PLACEHOLDERS
            replaced = _replace_in_paragraph_preserve_font(paragraph, placeholder, sanitized_replacement, multiline=multiline)
            if replaced:
                logger.info('replace_placeholders paragraph placeholder=%s replacement=%s', placeholder, sanitized_replacement)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for (placeholder, replacement) in replacements.items():
                        if placeholder not in paragraph.text:
                            continue
                        logger.info('replace_placeholders cell=%s', cell.text)
                        sanitized_replacement = sanitize_word_text(replacement)
                        if placeholder in _CAPTION_MULTILINE_PLACEHOLDERS:
                            sanitized_replacement = _single_line_text(sanitized_replacement)
                        _replace_in_paragraph_preserve_font(paragraph, placeholder, sanitized_replacement)
    return doc

def _build_cover_page_replacements(*, attorneys_contact_info: str, venue: str, plaintiff: str, defendant: str, title: str, is_defendant: bool, case_number: str | None=None, use_new_template: bool=False) -> dict:
    """Build placeholder map for legacy or new federal pleading caption templates."""
    if title == 'COMPLAINT':
        n_plaintiffs = len(plaintiff.split(','))
        n_defendents = len(defendant.split(','))
    else:
        n_plaintiffs = len(plaintiff.split(';'))
        n_defendents = len(defendant.split(';'))
    local = dict(replacements)
    local['(Name&Contact)'] = attorneys_contact_info
    if is_defendant:
        if n_defendents > 1:
            local['Attorneys for Plaintiff'] = 'Attorney for Defendants'
        else:
            local['Attorneys for Plaintiff'] = 'Attorney for Defendant'
    elif n_plaintiffs > 1:
        local['Attorneys for Plaintiff'] = 'Attorney for Plaintiffs'
    else:
        local['Attorneys for Plaintiff'] = 'Attorney for Plaintiff'
    court_line_1 = venue.split('\n')[0].upper()
    court_line_2 = venue.split('\n')[1].upper() if len(venue.split('\n')) > 1 else ''
    plaintiff_label = 'Plaintiffs' if n_plaintiffs > 1 else 'Plaintiff'
    defendant_label = 'Defendants' if n_defendents > 1 else 'Defendant'
    if use_new_template:
        local['UNITED STATES DISTRICT COURT'] = court_line_1
        local['SOUTHERN DISTRICT OF CALIFORNIA'] = court_line_2
        local['Plaintiff(s)'] = plaintiff_label
        local['Defendant(s)'] = defendant_label
    else:
        local['United States District Court'] = court_line_1
        local['Southern District of California'] = court_line_2
        local['Plaintiff_str'] = plaintiff_label
        local['Defendant_str'] = defendant_label
    local['(Plaintiff’s Name)'] = plaintiff
    local['(Defendant’s Name)'] = defendant
    local['TITLE OF PLEADING'] = title
    if case_number is not None:
        local['(Insert Case Number)'] = case_number
    return local

def create_cover_page(doc, attorneys_contact_info: str, venue: str, plaintiff: str, defendant: str, title='TITLE OF PLEADING', is_defendant=True, *, case_number: str | None=None, use_new_template: bool=False):
    logger.info('create_cover_page title=%s', title)
    remove_list_style_stubs(doc)
    logger.info('create_cover_page venue=%r', venue)
    page_replacements = _build_cover_page_replacements(attorneys_contact_info=attorneys_contact_info, venue=venue, plaintiff=plaintiff, defendant=defendant, title=title, is_defendant=is_defendant, case_number=case_number, use_new_template=use_new_template)
    doc = replace_placeholders(doc, page_replacements)
    if use_new_template:
        _normalize_caption_page_layout(doc, blank_lines_before_court=2)
        _trim_caption_template_tail(doc)
    doc.add_page_break()
    return doc
