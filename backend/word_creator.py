from docx import Document
from docx.enum.text import WD_LINE_SPACING
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.oxml.ns import nsdecls, qn
from docx.oxml import parse_xml, OxmlElement
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
import logging
import re
from datetime import datetime
from backend.pdf_creator import remove_anchor_tags
from backend.util.data_tools import number_to_letter
from backend.util.markdown_docx import (
    render_sections_markdown_word,
    render_simple_markdown_to_docx,
)
from backend.util.word_sanitize import sanitize_word_text  # noqa: F401 — re-exported for callers

logger = logging.getLogger(__name__)

# Load the template
template_path = "backend/util/Federal-Pleading-Template-Google-Doc-Compatible.docx"
analysis_template_path = "backend/util/Complaint-Analysis-Template-Google-Doc-Compatible.docx"


def remove_list_style_stubs(doc):
    paras_to_remove = []
    for para in doc.paragraphs:
        logger.info("remove_list_style_stubs paragraph=%s", para.text)
        stripped = para.text.strip()
        if stripped.lower() in [
            "first numbered paragraph",
            "second numbered paragraph",
        ]:
            paras_to_remove.append(para)
        elif stripped.upper().startswith("DATED:"):
            paras_to_remove.append(para)

    for para in paras_to_remove:
        p = para._element
        p.getparent().remove(p)
        p._p = p._element = None
    
    logger.info("remove_list_style_stubs removed_count=%s", len(paras_to_remove))


def _delete_paragraph(paragraph) -> None:
    element = paragraph._element
    element.getparent().remove(element)


def _paragraph_element_text(p_elem) -> str:
    return "".join(p_elem.itertext())


def _find_caption_end_body_index(body_children) -> int | None:
    """Return body index of last caption element (pleading title para or Case No. table)."""
    table_end_idx = None
    for i, child in enumerate(body_children):
        if child.tag != qn("w:p"):
            if child.tag == qn("w:tbl") and "Case No.:" in "".join(child.itertext()):
                table_end_idx = i
            continue
        text = _paragraph_element_text(child).strip()
        if text.startswith("Case No.:"):
            for j in range(i + 1, len(body_children)):
                if body_children[j].tag == qn("w:p"):
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
    for child in children[end_idx + 1 :]:
        if child.tag == qn("w:sectPr"):
            continue
        body.remove(child)


def _single_line_text(text: str) -> str:
    """Collapse newlines so placeholder substitution does not add extra paragraphs."""
    text = sanitize_word_text(text or "")
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


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


def _apply_caption_line_spacing(paragraph, *, double: bool = False) -> None:
    pf = paragraph.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    spacing_pt = (
        _CAPTION_DOUBLE_LINE_SPACING_PT if double else _CAPTION_SINGLE_LINE_SPACING_PT
    )
    pf.line_spacing = Pt(spacing_pt)
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)


def _court_name_paragraph_indices(doc) -> set[int]:
    """Return paragraph indices for the two court-name lines on the caption page."""
    indices: set[int] = set()
    for i, para in enumerate(doc.paragraphs):
        text = para.text.strip().upper()
        if "DISTRICT COURT" in text:
            indices.add(i)
            if i + 1 < len(doc.paragraphs) and doc.paragraphs[i + 1].text.strip():
                indices.add(i + 1)
            break
    return indices


def _normalize_caption_page_layout(doc, *, blank_lines_before_court: int = 2) -> None:
    """Double spacing on court-name lines only; single spacing elsewhere on caption page."""
    attorney_idx = None
    court_idx = None
    for i, para in enumerate(doc.paragraphs):
        text = para.text.strip()
        if attorney_idx is None and text.startswith("Attorney for"):
            attorney_idx = i
        if attorney_idx is not None and i > attorney_idx and "DISTRICT COURT" in text.upper():
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
    for i, para in enumerate(doc.paragraphs):
        _apply_caption_line_spacing(para, double=(i in court_indices))
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    _apply_caption_line_spacing(para, double=False)


# Define the placeholder replacements on the cover page template
replacements = {
    "(Name&Contact)": "(your name & contact info)",
    "Attorneys for Plaintiff": "Attorney for Plaintiff",
    "United States District Court": "\tUnited States District Court",
    "Southern District of California": "\tSouthern District of California",
    "UNITED STATES DISTRICT COURT": "UNITED STATES DISTRICT COURT",
    "SOUTHERN DISTRICT OF CALIFORNIA": "SOUTHERN DISTRICT OF CALIFORNIA",
    "Plaintiff_str": "Plaintiff/Plaintiffs",
    "Defendant_str": "Defendant/Defendants",
    "Plaintiff(s)": "Plaintiff(s)",
    "Defendant(s)": "Defendant(s)",
    "(Plaintiff’s Name)": "(Plaintiff’s Name)",
    "(Defendant’s Name)": "(Defendant’s Name)",
    "(Insert Case Number)": "___________",
    "TITLE OF PLEADING": "\tTITLE OF PLEADING",
}

_COURT_BOLD_PLACEHOLDERS = frozenset(
    {
        "United States District Court",
        "Southern District of California",
        "UNITED STATES DISTRICT COURT",
        "SOUTHERN DISTRICT OF CALIFORNIA",
    }
)

_CAPTION_MULTILINE_PLACEHOLDERS = frozenset({"(Name&Contact)"})
_CAPTION_SINGLE_LINE_SPACING_PT = 12
_CAPTION_DOUBLE_LINE_SPACING_PT = 24


def strip_leading_num(intext: str) -> str:
    """If the intext starts with an integer plus ., strip it off."""
    all_matches = list(re.finditer(r"^\d+.", intext))
    if len(all_matches) > 0:
        end = all_matches[0].span()[1]
        return intext[end:]
    else:
        return intext


def _font_snapshot(run):
    """Capture run font properties for reuse after placeholder substitution."""
    return {
        "name": run.font.name,
        "size": run.font.size,
        "bold": run.font.bold,
        "italic": run.font.italic,
        "underline": run.font.underline,
    }


def _apply_font_snapshot(run, snap: dict) -> None:
    if snap.get("name"):
        run.font.name = snap["name"]
    if snap.get("size") is not None:
        run.font.size = snap["size"]
    if snap.get("bold") is not None:
        run.font.bold = snap["bold"]
    if snap.get("italic") is not None:
        run.font.italic = snap["italic"]
    if snap.get("underline") is not None:
        run.font.underline = snap["underline"]


def _replace_in_paragraph_preserve_font(
    paragraph,
    placeholder: str,
    replacement: str,
    *,
    multiline: bool = False,
) -> bool:
    """Replace placeholder text in a paragraph without changing run font/size."""
    if placeholder not in paragraph.text:
        return False

    replacement = sanitize_word_text(replacement)
    if multiline and "\n" in replacement and paragraph.text.strip() == placeholder.strip():
        lines = [line.strip() for line in replacement.splitlines() if line.strip()]
        if lines:
            _set_paragraph_lines_preserve_font(paragraph, lines)
            return True

    if "\n" in replacement:
        replacement = _single_line_text(replacement)

    run_starts: list[tuple] = []
    combined = ""
    for run in paragraph.runs:
        run_starts.append((run, len(combined)))
        combined += run.text

    start_idx = combined.index(placeholder)
    end_idx = start_idx + len(placeholder)

    start_run = end_run = None
    start_off = end_off = 0
    for run, run_start in run_starts:
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
    for run, _run_start in run_starts:
        if run is start_run:
            clearing = True
            continue
        if run is end_run:
            run.text = run.text[end_off:]
            break
        if clearing:
            run.text = ""

    return True


def replace_placeholders(doc, replacements):
    """
    Replace placeholders in the document with actual values.
    
    Args:
        doc: The loaded Document object.
        replacements: A dictionary containing placeholders as keys and replacement text as values.
    """
    for paragraph in doc.paragraphs:
        for placeholder, replacement in replacements.items():
            if placeholder not in paragraph.text:
                continue
            logger.info("replace_placeholders paragraph=%s", paragraph.text)
            sanitized_replacement = sanitize_word_text(replacement)
            multiline = placeholder in _CAPTION_MULTILINE_PLACEHOLDERS
            replaced = _replace_in_paragraph_preserve_font(
                paragraph,
                placeholder,
                sanitized_replacement,
                multiline=multiline,
            )
            if replaced:
                logger.info(
                    "replace_placeholders paragraph placeholder=%s replacement=%s",
                    placeholder,
                    sanitized_replacement,
                )
                    
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for placeholder, replacement in replacements.items():
                        if placeholder not in paragraph.text:
                            continue
                        logger.info("replace_placeholders cell=%s", cell.text)
                        sanitized_replacement = sanitize_word_text(replacement)
                        if placeholder in _CAPTION_MULTILINE_PLACEHOLDERS:
                            sanitized_replacement = _single_line_text(sanitized_replacement)
                        _replace_in_paragraph_preserve_font(
                            paragraph,
                            placeholder,
                            sanitized_replacement,
                        )

    return doc

def _build_cover_page_replacements(
    *,
    attorneys_contact_info: str,
    venue: str,
    plaintiff: str,
    defendant: str,
    title: str,
    is_defendant: bool,
    case_number: str | None = None,
    use_new_template: bool = False,
) -> dict:
    """Build placeholder map for legacy or new federal pleading caption templates."""
    if title == "COMPLAINT":
        n_plaintiffs = len(plaintiff.split(","))
        n_defendents = len(defendant.split(","))
    else:
        n_plaintiffs = len(plaintiff.split(";"))
        n_defendents = len(defendant.split(";"))

    local = dict(replacements)
    local["(Name&Contact)"] = attorneys_contact_info
    if is_defendant:
        if n_defendents > 1:
            local["Attorneys for Plaintiff"] = "Attorney for Defendants"
        else:
            local["Attorneys for Plaintiff"] = "Attorney for Defendant"
    else:
        if n_plaintiffs > 1:
            local["Attorneys for Plaintiff"] = "Attorney for Plaintiffs"
        else:
            local["Attorneys for Plaintiff"] = "Attorney for Plaintiff"

    court_line_1 = venue.split("\n")[0].upper()
    court_line_2 = venue.split("\n")[1].upper() if len(venue.split("\n")) > 1 else ""
    plaintiff_label = "Plaintiffs" if n_plaintiffs > 1 else "Plaintiff"
    defendant_label = "Defendants" if n_defendents > 1 else "Defendant"

    if use_new_template:
        local["UNITED STATES DISTRICT COURT"] = court_line_1
        local["SOUTHERN DISTRICT OF CALIFORNIA"] = court_line_2
        local["Plaintiff(s)"] = plaintiff_label
        local["Defendant(s)"] = defendant_label
    else:
        local["United States District Court"] = court_line_1
        local["Southern District of California"] = court_line_2
        local["Plaintiff_str"] = plaintiff_label
        local["Defendant_str"] = defendant_label

    local["(Plaintiff’s Name)"] = plaintiff
    local["(Defendant’s Name)"] = defendant
    local["TITLE OF PLEADING"] = title
    if case_number is not None:
        local["(Insert Case Number)"] = case_number
    return local


def create_cover_page(
    doc,
    attorneys_contact_info: str,
    venue: str,
    plaintiff: str,
    defendant: str,
    title="TITLE OF PLEADING",
    is_defendant=True,
    *,
    case_number: str | None = None,
    use_new_template: bool = False,
):
    logger.info("create_cover_page title=%s", title)
    remove_list_style_stubs(doc)

    logger.info("create_cover_page venue=%r", venue)

    page_replacements = _build_cover_page_replacements(
        attorneys_contact_info=attorneys_contact_info,
        venue=venue,
        plaintiff=plaintiff,
        defendant=defendant,
        title=title,
        is_defendant=is_defendant,
        case_number=case_number,
        use_new_template=use_new_template,
    )

    doc = replace_placeholders(doc, page_replacements)
    if use_new_template:
        _normalize_caption_page_layout(doc, blank_lines_before_court=2)
        _trim_caption_template_tail(doc)
    doc.add_page_break()  # add page break here, main body text on next page
    return doc

def create_analysis_cover_page(doc, plaintiff: str, defendant: str, side: str):
    logger.info("create_analysis_cover_page side=%s", side)
    case_name = f"{plaintiff} vs. {defendant}"
    now = datetime.now()
    date_str = now.strftime("%A, %B %d, %Y")
    # Add paragraph with mixed formatting
    p = doc.add_paragraph()
    p.add_run(sanitize_word_text("Case Overview")).bold = True

    p = doc.add_paragraph()
    p.add_run(sanitize_word_text("Case Name: ")).bold = True
    p.add_run(sanitize_word_text(case_name))

    p = doc.add_paragraph()
    p.add_run(sanitize_word_text("Perspective: ")).bold = True
    p.add_run(sanitize_word_text(side.capitalize()))

   # Paragraph for "Prepared On"
    paragraph = doc.add_paragraph()
    paragraph.add_run(sanitize_word_text("Prepared On: ")).bold = True
    paragraph.add_run(sanitize_word_text(date_str))
    # add bottom border (horizontal line)
    p = paragraph._p
    pPr = p.get_or_add_pPr()
    pBdr = OxmlElement('w:pBdr')
    pPr.insert_element_before(pBdr,
        'w:shd', 'w:tabs', 'w:suppressAutoHyphens', 'w:kinsoku', 'w:wordWrap',
        'w:overflowPunct', 'w:topLinePunct', 'w:autoSpaceDE', 'w:autoSpaceDN',
        'w:bidi', 'w:adjustRightInd', 'w:snapToGrid', 'w:spacing', 'w:ind',
        'w:contextualSpacing', 'w:mirrorIndents', 'w:suppressOverlap', 'w:jc',
        'w:textDirection', 'w:textAlignment', 'w:textboxTightWrap',
        'w:outlineLvl', 'w:divId', 'w:cnfStyle', 'w:rPr', 'w:sectPr',
        'w:pPrChange'
    )
    bottom = OxmlElement('w:bottom')
    bottom.set(qn('w:val'), 'single')
    bottom.set(qn('w:sz'), '12')
    bottom.set(qn('w:space'), '1')
    bottom.set(qn('w:color'), '000000')
    pBdr.append(bottom)

    return doc


def complaint_create_sections_word(doc, sections: list[tuple[str, str]]):
    """
    Builds the Word document sections for a Complaint.
    Returns the modified doc object with identical formatting to your original code.
    """
    # Define styles
    heading_style = doc.styles['Heading 1']
    heading_style.font.size = Pt(12)
    heading_style.font.bold = True
    heading_style.font.underline = True
    heading_style.font.name = 'Times New Roman'
    heading_style.paragraph_format.space_before = Pt(5)
    heading_style.paragraph_format.space_after = Pt(5)

    heading_style = doc.styles['Heading 2']
    heading_style.font.size = Pt(12)
    heading_style.font.bold = True
    heading_style.font.name = 'Times New Roman'
    heading_style.paragraph_format.space_before = Pt(5)
    heading_style.paragraph_format.space_after = Pt(5)

    default_style = doc.styles['Normal']
    default_style.font.size = Pt(12)
    default_style.font.name = 'Times New Roman'

    relative_line_num = 1
    rel_para_i = 1

    for sec_index, (section_txt, title) in enumerate(sections):
        if not section_txt.strip():
            continue  # skip blank sections

        # Split the text into individual paragraphs by newline
        paragraphs = section_txt.split('\n')
        if paragraphs and not paragraphs[0].isupper():
            # Insert the heading/title if the first paragraph is NOT uppercase
            sanitized_title = sanitize_word_text(title)
            p = doc.add_paragraph(sanitized_title, style='Heading 1')
            p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
            p.paragraph_format.left_indent = Inches(0.5)
            p.paragraph_format.right_indent = Inches(0.5)
            p.paragraph_format.line_spacing = 2.0
            relative_line_num += 1

        for i, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            # Sanitize paragraph text to remove NULL bytes and invalid control characters
            paragraph = sanitize_word_text(paragraph)

            # Check for uppercase "title" paragraphs
            if paragraph.isupper():
                # For complaint, this can catch headings like "FACTUAL ALLEGATIONS"
                if i == 0 and title == "FACTUAL ALLEGATIONS" and not paragraph.startswith('FACTUAL ALLEGATIONS'):
                    # Insert the user-provided section title if mismatch
                    sanitized_title = sanitize_word_text(title)
                    p = doc.add_paragraph(sanitized_title, style='Heading 1')
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                    p.paragraph_format.left_indent = Inches(0.5)
                    p.paragraph_format.right_indent = Inches(0.5)
                    p.paragraph_format.line_spacing = 2.0

                # Now insert the uppercase paragraph as a heading
                p = doc.add_paragraph(paragraph, style='Heading 1')
                p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                p.paragraph_format.left_indent = Inches(0.5)
                p.paragraph_format.right_indent = Inches(0.5)
                p.paragraph_format.line_spacing = 2.0

            # Handle known subheaders within "FACTUAL ALLEGATIONS" (e.g., # A. INTRO, etc.)
            elif title == "FACTUAL ALLEGATIONS":
                sub_header_p = r"#+\s+[A-Z]"
                if re.match(sub_header_p, paragraph):
                    # Subheader line
                    paragraph = paragraph.replace('#', '').strip()
                    p = doc.add_paragraph(paragraph, style='Heading 2')
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                    p.paragraph_format.left_indent = Inches(0.5)
                    p.paragraph_format.right_indent = Inches(0.5)
                    p.paragraph_format.line_spacing = 2.0
                else:
                    # Word supplies numbering; remove only an explicit "N. " prefix,
                    # preserving factual numbers such as years, quantities, and decimals.
                    paragraph = re.sub(r"^\d+\.\s+", "", paragraph, count=1)
                    p = doc.add_paragraph(paragraph, style='List Number')
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
                    p.paragraph_format.left_indent = Inches(0.2)
                    p.paragraph_format.right_indent = Inches(0.2)
                    p.paragraph_format.line_spacing = 2.0
                    p.paragraph_format.first_line_indent = Inches(0.5)
                    rel_para_i += 1

            else:
                # Remove any existing leading number from LLM
                paragraph = strip_leading_num(paragraph).strip()
                # Sanitize again after processing
                paragraph = sanitize_word_text(paragraph)

                # Parenthetical sub-headers, e.g., "(A)" or "(i)"
                if paragraph.startswith('(') and paragraph.endswith(')'):
                    p = doc.add_paragraph(paragraph, style='Heading 2')
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                    p.paragraph_format.left_indent = Inches(0.5)
                    p.paragraph_format.right_indent = Inches(0.5)
                    p.paragraph_format.line_spacing = 2.0

                # WHEREFORE paragraphs (not renumbered)
                elif paragraph.startswith('WHEREFORE') or title == "":
                    p = doc.add_paragraph(f"\t{paragraph}", style='Normal')
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
                    p.paragraph_format.left_indent = Inches(0.2)
                    p.paragraph_format.right_indent = Inches(0.2)
                    p.paragraph_format.line_spacing = 2.0
                    p.paragraph_format.first_line_indent = Inches(0.5)

                else:
                    # if this is cause of action section, replace llm generated "paragraphs 1 through xxx" with prev rel_para_i
                    if title == "CAUSE OF ACTION":  
                        logger.info("complaint_word current_section_title=%s", title)
                        para_ref_pattern = re.compile(r'(paragraphs 1 through )\d+')
                        if para_ref_pattern.search(paragraph):
                            replacement = r'\g<1>' + str(rel_para_i - 1)
                            logger.info(
                                "complaint_word paragraph_before_reference_update=%s",
                                paragraph,
                            )
                            paragraph = para_ref_pattern.sub(replacement, paragraph)
                            logger.info(
                                "complaint_word paragraph_after_reference_update=%s",
                                paragraph,
                            )
                            # Sanitize again after regex substitution
                            paragraph = sanitize_word_text(paragraph)

                    # Normal complaint paragraph with numbering
                    # p = doc.add_paragraph(f'{rel_para_i}. {paragraph}', style='Normal')
                    p = doc.add_paragraph(paragraph, style='List Number')
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
                    p.paragraph_format.left_indent = Inches(0.2)
                    p.paragraph_format.right_indent = Inches(0.2)
                    p.paragraph_format.line_spacing = 2.0
                    p.paragraph_format.first_line_indent = Inches(0.5)
                    rel_para_i += 1

            relative_line_num += 1

    return doc


def answer_create_sections_word(doc, sections: list[tuple[str, str]]):
    """
    Builds the Word document sections for an Answer.
    Returns the modified doc object with identical formatting to your original code.
    """
    # Define styles
    heading_style = doc.styles['Heading 1']
    heading_style.font.size = Pt(12)
    heading_style.font.bold = True
    heading_style.font.underline = True
    heading_style.font.name = 'Times New Roman'
    heading_style.paragraph_format.space_before = Pt(5)
    heading_style.paragraph_format.space_after = Pt(5)

    heading_style = doc.styles['Heading 2']
    heading_style.font.size = Pt(12)
    heading_style.font.bold = True
    heading_style.font.name = 'Times New Roman'
    heading_style.paragraph_format.space_before = Pt(5)
    heading_style.paragraph_format.space_after = Pt(5)

    default_style = doc.styles['Normal']
    default_style.font.size = Pt(12)
    default_style.font.name = 'Times New Roman'

    relative_line_num = 1
    rel_para_i = 1

    for section_txt, title in sections:
        if not section_txt.strip():
            continue  # skip blank sections

        paragraphs = section_txt.split('\n')

        # Insert a heading/title if the first paragraph is NOT uppercase
        if paragraphs and not paragraphs[0].isupper():
            sanitized_title = sanitize_word_text(title)
            p = doc.add_paragraph(sanitized_title, style='Heading 1')
            p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
            p.paragraph_format.left_indent = Inches(0.5)
            p.paragraph_format.right_indent = Inches(0.5)
            p.paragraph_format.line_spacing = 2.0
            relative_line_num += 1

        in_response = False
        for i, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            paragraph = sanitize_word_text(paragraph)

            if paragraph.isupper():
                if i == 0 and title == "FACTUAL ALLEGATIONS" and not paragraph.startswith('FACTUAL ALLEGATIONS'):
                    sanitized_title = sanitize_word_text(title)
                    p = doc.add_paragraph(sanitized_title, style='Heading 1')
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                    p.paragraph_format.left_indent = Inches(0.5)
                    p.paragraph_format.right_indent = Inches(0.5)
                    p.paragraph_format.line_spacing = 2.0

                p = doc.add_paragraph(paragraph, style='Heading 1')
                p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                p.paragraph_format.left_indent = Inches(0.5)
                p.paragraph_format.right_indent = Inches(0.5)
                p.paragraph_format.line_spacing = 2.0
                in_response = False

            elif title in ["AFFIRMATIVE DEFENSES", "GENERAL DENIAL", "ANSWER"]:
                # Parenthetical sub-title (e.g., (FIRST DEFENSE))
                if paragraph.startswith('(') and paragraph.endswith(')'):
                    p = doc.add_paragraph(f"{paragraph.title()}", style='Heading 2')
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                    p.paragraph_format.left_indent = Inches(0.5)
                    p.paragraph_format.right_indent = Inches(0.5)
                    p.paragraph_format.line_spacing = 2.0

                else:
                    # Normal text within “Answer” sections
                    p = doc.add_paragraph(f'{paragraph}', style='Normal')
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
                    p.paragraph_format.left_indent = Inches(0.2)
                    p.paragraph_format.right_indent = Inches(0.2)
                    p.paragraph_format.line_spacing = 2.0
                    p.paragraph_format.first_line_indent = Inches(0.5)

            elif paragraph.startswith("Response:"):
                p = doc.add_paragraph(paragraph, style='Normal')
                p.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
                p.paragraph_format.left_indent = Inches(0.2)
                p.paragraph_format.right_indent = Inches(0.2)
                p.paragraph_format.line_spacing = 2.0
                p.paragraph_format.first_line_indent = Inches(0.5)
                in_response = True

            elif in_response:
                p = doc.add_paragraph(paragraph, style='Normal')
                p.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
                p.paragraph_format.left_indent = Inches(0.2)
                p.paragraph_format.right_indent = Inches(0.2)
                p.paragraph_format.line_spacing = 2.0
                p.paragraph_format.first_line_indent = Inches(0.5)
                in_response = False

            else:
                paragraph = strip_leading_num(paragraph).strip()
                paragraph = sanitize_word_text(paragraph)

                if paragraph.startswith('WHEREFORE'):
                    p = doc.add_paragraph(f"\t{paragraph}", style='Normal')
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
                    p.paragraph_format.left_indent = Inches(0.2)
                    p.paragraph_format.right_indent = Inches(0.2)
                    p.paragraph_format.line_spacing = 2.0
                    p.paragraph_format.first_line_indent = Inches(0.5)
                else:
                    p = doc.add_paragraph(paragraph, style='List Number')
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
                    p.paragraph_format.left_indent = Inches(0.2)
                    p.paragraph_format.right_indent = Inches(0.2)
                    p.paragraph_format.line_spacing = 2.0
                    p.paragraph_format.first_line_indent = Inches(0.5)
                    rel_para_i += 1

            relative_line_num += 1

    return doc


def create_rfp_sections_word(doc, sections: dict):

    # Define styles
    default_style = doc.styles['Normal']
    default_style.font.size = Pt(12)
    default_style.font.name = 'Times New Roman'

    relative_line_num = 1
    rel_para_i = 1

    for section_txt, title in sections:
        if len(section_txt) < 1:  # skip blank sections
            continue

        # Split the text into paragraphs
        paragraphs = section_txt.split('\n')
        logger.info("render_sections_word first_paragraph=%s", paragraphs[0])

        # Check if the first paragraph is not uppercase, then insert the title
        if not paragraphs[0].isupper():
            logger.info("render_sections_word writing_given_title")
            p = doc.add_paragraph()  # Create the paragraph with the desired style
            run = p.add_run(title)  # Add a run with the title text
            run.bold = True
            run.underline = True  # Apply underline to the run  
            p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER  # Center the title
            p.paragraph_format.left_indent = Inches(0.5)  # 1 inch left indent
            p.paragraph_format.right_indent = Inches(0.5)
            p.paragraph_format.line_spacing = 2.0

            relative_line_num += 1

        for i, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()
            if len(paragraph.strip()) < 1:
                continue

            # Sanitize paragraph text to remove NULL bytes and invalid control characters
            paragraph = sanitize_word_text(paragraph)

            if paragraph.isupper():  # Use bold/underline for titles
                logger.info("render_sections_word writing_title index=%s", i)
                if any(paragraph.startswith(keyword) for keyword in ["REQUEST FOR PRODUCTION NO", "REQUEST FOR ADMISSION NO", "INTERROGATORY NO", "RESPONSE TO"]):
                    p = doc.add_paragraph()
                    run = p.add_run(paragraph)
                    run.bold = True
                    run.underline = True
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                    p.paragraph_format.left_indent = Inches(0.5)  # 1 inch left indent
                    p.paragraph_format.right_indent = Inches(0.5)
                    p.paragraph_format.line_spacing = 2.0
                else:    # writing titles given
                    p = doc.add_paragraph()
                    run = p.add_run(paragraph)
                    run.bold = True
                    run.underline = True
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                    p.paragraph_format.left_indent = Inches(0.5)  # 1 inch left indent
                    p.paragraph_format.right_indent = Inches(0.5)
                    p.paragraph_format.line_spacing = 2.0
                

            elif title in ["REQUESTS FOR PRODUCTION OF DOCUMENTS", "INTERROGATORIES", "REQUESTS FOR ADMISSIONS", "SPECIFIC OBJECTIONS AND RESPONSES"]:
                # Add formatted requests (similar to the PDF logic)
                logger.info("render_sections_word writing_request_text index=%s", i)
                p = doc.add_paragraph(paragraph, style='Normal')
                p.style = 'Normal'
                p.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
                # Set left and right indentation
                p.paragraph_format.left_indent = Inches(0.2)
                p.paragraph_format.right_indent = Inches(0.2)  
                p.paragraph_format.line_spacing = 2.0
                p.paragraph_format.first_line_indent = Inches(0.5)
            else:
                logger.info("render_sections_word writing_body_text index=%s", i)
                # Add paragraph numbers and remove leading numbers if any
                paragraph = strip_leading_num(paragraph).strip()
                # Sanitize again after processing
                paragraph = sanitize_word_text(paragraph)
                    
                if title == "" or (title == "DEFINITIONS" and i < 3):
                    p = doc.add_paragraph(paragraph, style='Normal')
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
                    p.paragraph_format.left_indent = Inches(0.2)
                    p.paragraph_format.right_indent = Inches(0.2)
                    p.paragraph_format.line_spacing = 2.0
                    p.paragraph_format.first_line_indent = Inches(0.5)
                else:
                    # p = doc.add_paragraph(f'{rel_para_i}. {paragraph}', style='Normal')
                    p = doc.add_paragraph(paragraph, style='List Number')
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
                    p.paragraph_format.left_indent = Inches(0.2)
                    p.paragraph_format.right_indent = Inches(0.2)
                    p.paragraph_format.line_spacing = 2.0
                    p.paragraph_format.first_line_indent = Inches(0.5)

                    rel_para_i += 1

            relative_line_num += 1
    return doc
    

def create_complaint_word(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        parties: str,
        jurisdiction_n_venue: str,
        factual_allegations: str,
        cause_of_action: str,
        damages: str,
        prayer_for_relief: str):
    
    """Drafts a complaint and returns the filename."""

     # Create a SimpleDocTemplate object  -> BaseDocTemplate
    doc = Document(template_path)
    
    # Set right margin to 0.5 inches for all sections
    for section in doc.sections:
        section.right_margin = Inches(0.4)

    # doc, attorneys_contact_info: str, venue: str, plaintiff: str, defendant: str, is_defendant=True
    doc = create_cover_page(doc, attorneys_contact_info, venue, plaintiff, defendant, title="COMPLAINT", is_defendant=False)

    if len(introduction.split("INTRODUCTION")) > 1:
        sections = [(introduction.split("INTRODUCTION")[0], ""),
            (introduction.split("INTRODUCTION")[1], "INTRODUCTION"),
            (parties, "PARTIES"),
                    (jurisdiction_n_venue, "JURISDICTION AND VENUE"),
                    (factual_allegations, "FACTUAL ALLEGATIONS"),
                    (cause_of_action, "CAUSE OF ACTION"),
                    (prayer_for_relief, "REQUEST FOR RELIEF")
                    ]
    else:
        sections = [(introduction, "INTRODUCTION"),
                    (parties, "PARTIES"),
                    (jurisdiction_n_venue, "JURISDICTION AND VENUE"),
                    (factual_allegations, "FACTUAL ALLEGATIONS"),
                    (cause_of_action, "CAUSE OF ACTION"),
                    (prayer_for_relief, "REQUEST FOR RELIEF")
                    ]
    
    doc = complaint_create_sections_word(doc, sections)
    doc.save(fn)
    return fn



def create_answer_word(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        responses: str,
        affirmative_defenses: str,
        counter_claims: str):

    if responses.lstrip().startswith("GENERAL"):
        responses_title = "GENERAL DENIAL"
        responses = responses.replace('GENERAL DENIAL', '', 1)
    else:
        responses_title = "RESPONSES TO ALLEGATIONS"

    sections = [(introduction, "ANSWER"),
                (responses, responses_title),
                (affirmative_defenses, "AFFIRMATIVE DEFENSES"),
                (counter_claims, "REQUEST FOR RELIEF")
                ]
    
    doc = Document(template_path)
    # Set right margin to 0.5 inches for all sections
    for section in doc.sections:
        section.right_margin = Inches(0.4)
    doc = create_cover_page(doc, attorneys_contact_info, venue, plaintiff, defendant, title="ANSWER", is_defendant=True)

    doc = answer_create_sections_word(doc, sections)
    doc.save(fn)
    return fn


def create_rfp_word(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        definitions: str,
        instructions: str,
        rfps: str,
        is_defendant: bool):
    
    doc = Document(template_path)
    # Set right margin to 0.5 inches for all sections
    for section in doc.sections:
        section.right_margin = Inches(0.4)
    doc = create_cover_page(doc, attorneys_contact_info, venue, plaintiff, defendant, 
                            title="DEFENDANT'S REQUESTS FOR PRODUCTION OF DOCUMENTS" if is_defendant else "PLAINTIFF'S REQUESTS FOR PRODUCTION OF DOCUMENTS", 
                            is_defendant=is_defendant)

    sections = [(introduction, ""),
            (definitions, "DEFINITIONS"),
            (instructions, "INSTRUCTIONS"),
            (rfps, "REQUESTS FOR PRODUCTION OF DOCUMENTS"),
            ]
    doc = create_rfp_sections_word(doc, sections)
    doc.save(fn)
    return fn


def create_rfa_word(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        definitions: str,
        instructions: str,
        rfas: str,
        is_defendant: bool):
    
    sections = [(introduction, ""),
            (definitions, "DEFINITIONS"),
            (instructions, "INSTRUCTIONS"),
            (rfas, "REQUESTS FOR ADMISSIONS"),
            ]
    
    doc = Document(template_path)
    # Set right margin to 0.5 inches for all sections
    for section in doc.sections:
        section.right_margin = Inches(0.4)
    doc = create_cover_page(doc, attorneys_contact_info, venue, plaintiff, defendant, 
                            title="DEFENDANT'S REQUESTS FOR ADMISSIONS" if is_defendant else "PLAINTIFF'S REQUESTS FOR ADMISSIONS",
                            is_defendant=is_defendant)
    doc = create_rfp_sections_word(doc, sections)
    doc.save(fn)    
    return fn


def create_rog_word(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        definitions: str,
        instructions: str,
        rogs: str,
        is_defendant: bool):
    
    sections = [(introduction, ""),
            (definitions, "DEFINITIONS"),
            (instructions, "INSTRUCTIONS"),
            (rogs, "INTERROGATORIES"),
            ]
    
    doc = Document(template_path)
    # Set right margin to 0.5 inches for all sections
    for section in doc.sections:
        section.right_margin = Inches(0.4)
    doc = create_cover_page(doc, attorneys_contact_info, venue, plaintiff, defendant, 
                            title="DEFENDANT'S INTERROGATORIES" if is_defendant else "PLAINTIFF'S INTERROGATORIES",
                            is_defendant=is_defendant)
    doc = create_rfp_sections_word(doc, sections)
    doc.save(fn)
    return fn


def create_rfp_response_word(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        general_objections: str,
        rfp_response: str,
        is_defendant: bool):

    sections = [(introduction, ""),
                (general_objections, "GENERAL OBJECTIONS"),
                (rfp_response, "SPECIFIC OBJECTIONS AND RESPONSES"),
                ]
    doc = Document(template_path)
    # Set right margin to 0.5 inches for all sections
    for section in doc.sections:
        section.right_margin = Inches(0.4)
    doc = create_cover_page(doc, attorneys_contact_info, venue, plaintiff, defendant, 
                            title="DEFENDANT'S RESPONSES TO PLAINTIFF'S REQUEST FOR PRODUCTION OF DOCUMENTS" if is_defendant else "PLAINTIFF'S RESPONSES TO DEFENDANT'S REQUEST FOR PRODUCTION OF DOCUMENTS",
                            is_defendant=is_defendant)
    doc = create_rfp_sections_word(doc, sections)
    doc.save(fn)
    return fn


def create_rog_response_word(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        general_objections: str,
        rog_response: str,
        is_defendant: bool):

    sections = [
        (introduction, ""),
        (general_objections, "GENERAL OBJECTIONS"),
        (rog_response, "SPECIFIC OBJECTIONS AND RESPONSES"),
    ]
    doc = Document(template_path)
    # Set right margin to 0.5 inches for all sections
    for section in doc.sections:
        section.right_margin = Inches(0.4)
    doc = create_cover_page(
        doc,
        attorneys_contact_info,
        venue,
        plaintiff,
        defendant,
        title="DEFENDANT'S RESPONSES TO PLAINTIFF'S INTERROGATORIES" if is_defendant else "PLAINTIFF'S RESPONSES TO DEFENDANT'S INTERROGATORIES",
        is_defendant=is_defendant,
    )
    doc = create_rfp_sections_word(doc, sections)
    doc.save(fn)
    return fn


def create_complaint_analysis_word(
        fn: str,
        plaintiff: str,
        defendant: str,
        factual_allegations: str,
        elements: str,
        recomendations: str,
        side: str):
    
    """Drafts a complaint analyzer and returns the filename."""

     # Create a SimpleDocTemplate object  -> BaseDocTemplate
    doc = Document()  # use the default template
    # Set right margin to 0.5 inches for all sections
    for section in doc.sections:
        section.right_margin = Inches(0.4)
    # Set default font and size for the document
    style = doc.styles['Normal']
    style.font.name = 'Times New Roman'
    style.font.size = Pt(12)
    style.font.color.rgb = RGBColor(0, 0, 0)
    # Configure paragraph spacing
    paragraph_format = style.paragraph_format
    paragraph_format.line_spacing = 1.5
    paragraph_format.space_before = Pt(0)
    paragraph_format.space_after = Pt(0)

    # Cover page
    doc = create_analysis_cover_page(doc, plaintiff, defendant, side)

    # Render all sections from Markdown for consistency with PDF
    sections = [
            (factual_allegations, "Summary"),
            (elements, "Elements Analysis"),
            (recomendations, "Recommendations")
            ]

    doc = render_sections_markdown_word(doc, sections)
    doc.save(fn)
    return fn


def create_analysis_sections_word(doc, sections: dict):

    heading_style = doc.styles['Heading 1']
    heading_style.font.size = Pt(12)
    heading_style.font.bold = True
    heading_style.font.color.rgb = RGBColor(0, 0, 0)  # Set font color to black
    heading_style.font.name = 'Times New Roman'
    heading_style.paragraph_format.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
    # heading_style.paragraph_format.space_before = Pt(5)
    # heading_style.paragraph_format.space_after = Pt(7)

    relative_line_num = 1
    rel_para_i = 1

    for section_txt, title in sections:
        if len(section_txt) < 1:  # skip blank sections
            continue

        # Split the text into paragraphs
        paragraphs = section_txt.split('\n')
        logger.info("render_analysis_word first_paragraph=%s", paragraphs[0])

        # Check if the first paragraph is not uppercase, then insert the title
        # if not paragraphs[0].isupper():
        logger.info("render_analysis_word writing_given_title=%s", title)
        if not paragraphs[0].isupper() or '<B>' in paragraphs[0]:
            sanitized_title = sanitize_word_text(title)
            p = doc.add_paragraph(sanitized_title, style='Heading 1')
            run = p.runs[0]
            run.font.size = Pt(12)
            run.font.bold = True
            run.font.name = 'Times New Roman'
            relative_line_num += 1

        for i, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()
            if len(paragraph.strip()) < 1:
                continue

            # Sanitize paragraph text to remove NULL bytes and invalid control characters
            paragraph = sanitize_word_text(paragraph)

            if paragraph.isupper():  # Use bold/underline for coa titles
                if paragraph.startswith("COUNT") and title in ["LEGAL ELEMENTS"]:
                    logger.info(
                        "render_analysis_word writing_coa_text index=%s paragraph=%s",
                        i,
                        paragraph,
                    )
                    p = doc.add_paragraph(paragraph, style='Heading 1')
                    run = p.runs[0]
                    run.font.size = Pt(12)
                    run.font.bold = True
                    run.font.name = 'Times New Roman'
                else:  # write this line as title 
                    if '<B>' in paragraph:
                        paragraph = paragraph.replace('<B>', '').replace('</B>', '')
                    p = doc.add_paragraph(paragraph, style='Heading 1')
                    run = p.runs[0]
                    run.font.size = Pt(12)
                    run.font.bold = True
                    run.font.name = 'Times New Roman'
            
            elif title == "FACTUAL ALLEGATIONS":
                if '<B>' in paragraph:
                    paragraph = paragraph.replace('<B>', '').replace('</B>', ' ')
                    p = doc.add_paragraph(paragraph, style='Heading 1')
                    run = p.runs[0]
                    run.font.bold = True
                    run.font.size = Pt(12)
                    run.font.name = 'Times New Roman'
                else:
                    # p = doc.add_paragraph(f"{rel_para_i}. {paragraph}", style="Normal")
                    p = doc.add_paragraph(paragraph, style='List Number')
                    rel_para_i += 1
                    
            elif title == "LEGAL ELEMENTS":
                logger.info("render_analysis_word writing_element_text=%s", paragraph)
                p = doc.add_paragraph(paragraph, style="Normal")
            elif title == "RECOMMENDATIONS":
                if paragraph.startswith('Cause of Action:') or paragraph == 'Recommendations:' or paragraph == 'Citations:':
                    p = doc.add_paragraph(paragraph, style="Normal")
                    run = p.runs[0]
                    run.font.bold = True
                else:
                    p = doc.add_paragraph(paragraph, style="Normal")
            
            else: # this should be items inside factual allegations
                logger.info(
                    "render_analysis_word writing_body_text index=%s paragraph=%s",
                    i,
                    paragraph,
                )
                # Add paragraph numbers and remove leading numbers if any
                # paragraph = strip_leading_num(paragraph).strip()
                # p = doc.add_paragraph(f"{rel_para_i}. {paragraph}", style='Normal')
                p = doc.add_paragraph(paragraph, style='List Number')
                rel_para_i += 1

            relative_line_num += 1

    return doc


def create_dismiss_word(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        background: str,
        standard: str,
        arguments: str,
        conclusion: str):
    
    # print("before remove anchor argument: \n", arguments)
    # print("***")
    # print("after remove anchor: \n", remove_anchor_tags(arguments))

    sections = [(remove_anchor_tags(introduction), ""),
            (remove_anchor_tags(background), "BACKGROUND"),
            (remove_anchor_tags(standard), "LEGAL STANDARD"),
            (remove_anchor_tags(arguments), "ARGUMENTS"),
            (remove_anchor_tags(conclusion), "CONCLUSION"),
            ]
    
    doc = Document(template_path)
    # Set right margin to 0.5 inches for all sections
    for section in doc.sections:
        section.right_margin = Inches(0.4)
    doc = create_cover_page(doc, attorneys_contact_info, venue, plaintiff, defendant, 
                            title="MOTION TO DISMISS")
    doc = dismiss_create_sections_word(doc, sections)
    doc.save(fn)
    return fn


def dismiss_create_sections_word(doc, sections: list[tuple[str, str]]):
    """Builds the Word document sections for Motion to dismiss."""
    # Define styles
    heading_style = doc.styles['Heading 1']
    heading_style.font.size = Pt(12)
    heading_style.font.bold = True
    heading_style.font.underline = True
    heading_style.font.name = 'Times New Roman'
    heading_style.paragraph_format.space_before = Pt(5)
    heading_style.paragraph_format.space_after = Pt(5)

    heading_style = doc.styles['Heading 2']
    heading_style.font.size = Pt(12)
    heading_style.font.bold = True
    heading_style.font.name = 'Times New Roman'
    heading_style.paragraph_format.space_before = Pt(5)
    heading_style.paragraph_format.space_after = Pt(5)

    default_style = doc.styles['Normal']
    default_style.font.size = Pt(12)
    default_style.font.name = 'Times New Roman'

    relative_line_num = 1
    rel_para_i = 1

    for section_txt, title in sections:
        if not section_txt.strip():
            continue  # skip blank sections

        sanitized_title = sanitize_word_text(title)
        p = doc.add_paragraph(sanitized_title, style='Heading 1')
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        p.paragraph_format.left_indent = Inches(0.5)
        p.paragraph_format.right_indent = Inches(0.5)
        p.paragraph_format.line_spacing = 2.0

        paragraphs = section_txt.split('\n')
        prev_letter_i = 0
        for i, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            # Sanitize paragraph text to remove NULL bytes and invalid control characters
            paragraph = sanitize_word_text(paragraph)

            # If the paragraph is uppercase, treat it as a heading
            if paragraph.isupper():   # coa-level dismissal header is all uppercase
                if paragraph.startswith('**'):
                    paragraph = paragraph[2:-2]
                p = doc.add_paragraph(paragraph, style='Heading 1')
                prev_letter_i = 0
                p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                p.paragraph_format.left_indent = Inches(0.5)
                p.paragraph_format.right_indent = Inches(0.5)
                p.paragraph_format.line_spacing = 2.0

            elif paragraph.startswith('**') and paragraph.endswith('**'):
                p = doc.add_paragraph(f"{number_to_letter(prev_letter_i +1)}. {paragraph[2:-2].strip().title()}", style='Heading 2')
                prev_letter_i += 1
                p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                p.paragraph_format.left_indent = Inches(0.5)
                p.paragraph_format.right_indent = Inches(0.5)
                p.paragraph_format.line_spacing = 2.0

            else:
                # Remove any leading LLM-provided numbering
                paragraph = strip_leading_num(paragraph).strip()
                # Sanitize again after processing
                paragraph = sanitize_word_text(paragraph)

                # p = doc.add_paragraph(f'{rel_para_i}. {paragraph}', style='Normal')
                p = doc.add_paragraph(paragraph, style='List Number')
                
                # run = p.add_run(f"{rel_para_i}. ")
                # run.italic = False  # Ensure numbering is not italicized

                segments = re.split(r'(<i>.*?</i>)', paragraph)
                for seg in segments:
                    if seg.startswith('<i>') and seg.endswith('</i>'):
                        italic_text = seg[3:-4]  # Remove <i> and </i>
                        run = p.add_run(italic_text)
                        run.italic = True
                    else:
                        run = p.add_run(seg)
                        run.italic = False

                p.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
                p.paragraph_format.left_indent = Inches(0.2)
                p.paragraph_format.right_indent = Inches(0.2)
                p.paragraph_format.line_spacing = 2.0
                p.paragraph_format.first_line_indent = Inches(0.5)
                rel_para_i += 1

            relative_line_num += 1

    return doc



if __name__ == "__main__":
    doc = Document(template_path)
    for style in doc.styles:
        if style.type == 1:  # WD_STYLE_TYPE.PARAGRAPH
            logger.info("style_name=%s", style.name)
