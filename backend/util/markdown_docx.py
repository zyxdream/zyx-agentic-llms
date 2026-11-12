"""Markdown to Word rendering with CAED pleading-paper styles."""

import re

from docx import Document
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

from backend.util.pleading_caption import create_document_with_caption
from backend.util.pleading_format import (
    STYLE_BLOCK_QUOTE,
    STYLE_BODY,
    STYLE_BODY_CONTINUED,
    STYLE_NUMBERED_PARAGRAPH,
    STYLE_TITLE1,
    STYLE_TITLE2,
    STYLE_TITLE3,
    add_document_title,
    apply_pleading_format_to_document,
    finalize_export_spec,
    get_structural_spec,
)
from backend.util.word_sanitize import sanitize_word_text

_ORDERED_LIST_RE = re.compile(r"^\d+\.\s+")
_STANDALONE_BOLD_LINE_RE = re.compile(r"^\s*\*\*(?P<text>.+?)\*\*\s*$")
_NUMBERED_TITLE_PREFIX_RE = re.compile(
    r"^(?:[IVXLC]+\.|[A-Za-z]\.|\d+\.)\s+",
    re.IGNORECASE,
)
_CAPS_HEADER_MAX_LEN = 120


def _strip_leading_num(text: str) -> str:
    """Strip a leading 'N.' prefix if present (matches complaint export behavior)."""
    match = re.match(r"^\d+\.\s*", text)
    return text[match.end() :] if match else text


def _should_auto_number_paragraph(text: str) -> bool:
    """Return True for narrative body lines that should receive allegation numbers."""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("#"):
        return False
    if stripped.startswith("**") and stripped.endswith("**"):
        return False
    if stripped.isupper() and len(stripped) <= _CAPS_HEADER_MAX_LEN:
        return False
    if _NUMBERED_TITLE_PREFIX_RE.match(stripped):
        return False
    if stripped.upper().startswith("WHEREFORE"):
        return False
    if stripped.startswith("(") and stripped.endswith(")"):
        return False
    return True


def normalize_markdown(md_text: str) -> str:
    """Normalize HTML italics and line endings before rendering."""
    if not md_text:
        return ""
    text = md_text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(
        r"<i\b[^>]*>(.*?)</i>",
        r"*\1*",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"<em\b[^>]*>(.*?)</em>",
        r"*\1*",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return text


def _normalize_title_for_match(text: str) -> str:
    return text.strip().rstrip(":").casefold()


def _title_text_from_line(line: str) -> str:
    """Extract comparable title text from a possible section-title line."""
    stripped = line.strip()
    if stripped.startswith("#"):
        return stripped.lstrip("#").strip()
    if stripped.startswith("**") and stripped.endswith("**"):
        return stripped[2:-2].strip()
    prefix = _NUMBERED_TITLE_PREFIX_RE.match(stripped)
    if prefix:
        return stripped[prefix.end() :].strip()
    return stripped


def _line_matches_section_name(line: str, section_name: str) -> bool:
    if not line.strip() or not section_name:
        return False
    return _normalize_title_for_match(_title_text_from_line(line)) == _normalize_title_for_match(
        section_name
    )


def _should_strip_duplicate_line(line: str, section_name: str) -> bool:
    """Strip any leading line that duplicates section_name (plain, **bold**, numbered, or #/##).

    The exporter now renders section_name itself as the section heading, so a repeat of it at the
    top of section_content — in whatever markdown form — would double the heading and is removed.
    """
    return _line_matches_section_name(line, section_name)


def _strip_duplicate_section_heading(md_text: str, section_name: str | None) -> str:
    """Remove leading lines that repeat section_name (UI card header is not exported)."""
    if not section_name or not md_text:
        return md_text
    lines = md_text.split("\n")
    changed = True
    while changed:
        changed = False
        idx = 0
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if idx >= len(lines):
            break
        if _should_strip_duplicate_line(lines[idx], section_name):
            del lines[idx]
            changed = True
            while idx < len(lines) and not lines[idx].strip():
                del lines[idx]
    return "\n".join(lines)


def reorder_sections(
    sections: list[dict],
    section_order: list[str] | None,
) -> list[dict]:
    if not section_order:
        return sections
    by_name = {s.get("section_name"): s for s in sections if s.get("section_name")}
    ordered = []
    seen = set()
    for name in section_order:
        if name in by_name and name not in seen:
            ordered.append(by_name[name])
            seen.add(name)
    for sec in sections:
        name = sec.get("section_name")
        if name and name not in seen:
            ordered.append(sec)
            seen.add(name)
    return ordered


def _apply_inline_markdown_runs(paragraph, text: str, font_name: str = "Times New Roman", *, force_bold: bool = False):
    # force_bold makes every run bold (used for heading paragraphs, where the run-level bold value
    # otherwise overrides the heading style); inline italic emphasis is still honored.
    text = sanitize_word_text(text)
    pattern = re.compile(r"(\*\*.+?\*\*|\*(?!\*)(.+?)(?<!\*)\*|_(.+?)_)")
    pos = 0
    for m in pattern.finditer(text):
        start, end = m.span()
        if start > pos:
            run = paragraph.add_run(text[pos:start])
            run.font.name = font_name
            run.bold = force_bold
            run.italic = False
        token = text[start:end]
        if token.startswith("**") and token.endswith("**"):
            run = paragraph.add_run(token[2:-2])
            run.font.name = font_name
            run.bold = True
            run.italic = False
        elif token.startswith("*") and token.endswith("*"):
            run = paragraph.add_run(token[1:-1])
            run.font.name = font_name
            run.bold = force_bold
            run.italic = True
        elif token.startswith("_") and token.endswith("_"):
            run = paragraph.add_run(token[1:-1])
            run.font.name = font_name
            run.bold = force_bold
            run.italic = True
        pos = end
    if pos < len(text):
        run = paragraph.add_run(text[pos:])
        run.font.name = font_name
        run.bold = force_bold
        run.italic = False


def _standalone_bold_line_text(line: str) -> str | None:
    match = _STANDALONE_BOLD_LINE_RE.match(line)
    if not match:
        return None
    text = sanitize_word_text(match.group("text").strip())
    return text or None


def render_markdown_to_docx(
    doc: Document,
    md_text: str,
    *,
    section_name: str | None = None,
    after_block_quote: bool = False,
    require_numbered_paragraphs: bool = False,
    continuous_paragraph_numbering: bool = True,
    paragraph_counter: list[int] | None = None,
) -> bool:
    """Render markdown body into doc. Returns whether the last block was a block quote."""
    md_text = normalize_markdown(md_text or "")
    md_text = _strip_duplicate_section_heading(md_text, section_name)
    if not md_text.strip():
        return after_block_quote

    lines = md_text.split("\n")
    bullet_buffer: list[str] = []
    ordered_buffer: list[tuple[int, str]] = []
    use_continued = after_block_quote
    counter = paragraph_counter if paragraph_counter is not None else [1]
    local_num = [1]

    def flush_bullets():
        nonlocal bullet_buffer, use_continued
        for item in bullet_buffer:
            style = STYLE_BODY_CONTINUED if use_continued else STYLE_BODY
            p = doc.add_paragraph(style=style)
            _apply_inline_markdown_runs(p, item)
            use_continued = False
        bullet_buffer = []

    def flush_ordered():
        nonlocal ordered_buffer, use_continued
        for source_num, item in ordered_buffer:
            if require_numbered_paragraphs:
                num = counter[0] if continuous_paragraph_numbering else source_num
                if continuous_paragraph_numbering:
                    counter[0] += 1
                p = doc.add_paragraph(style=STYLE_NUMBERED_PARAGRAPH)
                _apply_inline_markdown_runs(p, f"{num}. {item}")
            else:
                p = doc.add_paragraph(style=STYLE_TITLE3)
                _apply_inline_markdown_runs(p, item)
            use_continued = False
        ordered_buffer = []

    def add_body_paragraph(text: str):
        nonlocal use_continued
        if require_numbered_paragraphs and _should_auto_number_paragraph(text):
            body_text = _strip_leading_num(sanitize_word_text(text)).strip()
            if continuous_paragraph_numbering:
                num = counter[0]
                counter[0] += 1
            else:
                num = local_num[0]
                local_num[0] += 1
            p = doc.add_paragraph(style=STYLE_NUMBERED_PARAGRAPH)
            _apply_inline_markdown_runs(p, f"{num}. {body_text}")
        else:
            style = STYLE_BODY_CONTINUED if use_continued else STYLE_BODY
            p = doc.add_paragraph(style=style)
            _apply_inline_markdown_runs(p, text)
        use_continued = False

    def add_block_quote(text: str):
        nonlocal use_continued
        p = doc.add_paragraph(style=STYLE_BLOCK_QUOTE)
        _apply_inline_markdown_runs(p, text)
        use_continued = True

    last_was_block_quote = after_block_quote

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_bullets()
            flush_ordered()
            continue

        if line.strip() == "---":
            flush_bullets()
            flush_ordered()
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(2)
            p_pr = p._p.get_or_add_pPr()
            p_bdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "12")
            bottom.set(qn("w:space"), "1")
            bottom.set(qn("w:color"), "000000")
            p_bdr.append(bottom)
            p_pr.append(p_bdr)
            use_continued = False
            last_was_block_quote = False
            continue

        if line.startswith("> ") or line.startswith(">"):
            flush_bullets()
            flush_ordered()
            quote_text = line[1:].lstrip() if line.startswith(">") else line
            add_block_quote(sanitize_word_text(quote_text))
            last_was_block_quote = True
            continue

        if line.startswith("# "):
            flush_bullets()
            flush_ordered()
            p = doc.add_paragraph(style=STYLE_TITLE1)
            _apply_inline_markdown_runs(p, sanitize_word_text(line[2:].strip()), force_bold=True)
            _center_heading_paragraph(p)
            use_continued = False
            last_was_block_quote = False
            continue

        if line.startswith("## "):
            flush_bullets()
            flush_ordered()
            p = doc.add_paragraph(style=STYLE_TITLE2)
            _apply_inline_markdown_runs(p, sanitize_word_text(line[3:].strip()), force_bold=True)
            _center_heading_paragraph(p)
            use_continued = False
            last_was_block_quote = False
            continue

        if line.startswith("### "):
            flush_bullets()
            flush_ordered()
            p = doc.add_paragraph(style=STYLE_TITLE3)
            _apply_inline_markdown_runs(p, sanitize_word_text(line[4:].strip()), force_bold=True)
            _center_heading_paragraph(p)
            use_continued = False
            last_was_block_quote = False
            continue

        bold_heading_text = _standalone_bold_line_text(line)
        if bold_heading_text is not None:
            flush_bullets()
            flush_ordered()
            p = doc.add_paragraph(style=STYLE_BODY)
            _apply_inline_markdown_runs(p, bold_heading_text, force_bold=True)
            _center_heading_paragraph(p)
            use_continued = False
            last_was_block_quote = False
            continue

        stripped = line.lstrip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            bullet_text = stripped[2:].strip()
            bullet_buffer.append(bullet_text)
            last_was_block_quote = False
            continue

        ordered_match = _ORDERED_LIST_RE.match(stripped)
        if ordered_match:
            num_str = stripped[: ordered_match.end()].strip().rstrip(".")
            try:
                source_num = int(num_str)
            except ValueError:
                source_num = len(ordered_buffer) + 1
            text = stripped[ordered_match.end() :].strip()
            ordered_buffer.append((source_num, text))
            last_was_block_quote = False
            continue

        flush_bullets()
        flush_ordered()
        add_body_paragraph(sanitize_word_text(line))
        last_was_block_quote = False

    flush_bullets()
    flush_ordered()
    return last_was_block_quote


def _center_heading_paragraph(p) -> None:
    """Center a heading paragraph across the text width.

    The CAED title styles carry left/hanging indents; zero them here so a centered heading is
    centered across the full width rather than within the indented space.
    """
    p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    pf = p.paragraph_format
    pf.left_indent = Inches(0)
    pf.first_line_indent = Inches(0)


def _section_heading_style(level) -> str:
    """Map a section level (1/2/3) to its CAED heading style, defaulting to Heading 1."""
    try:
        lvl = int(level or 1)
    except (TypeError, ValueError):
        lvl = 1
    return {1: STYLE_TITLE1, 2: STYLE_TITLE2, 3: STYLE_TITLE3}.get(max(1, min(3, lvl)), STYLE_TITLE1)


def render_draft_sections_to_docx(
    doc: Document,
    sections: list[dict],
    *,
    section_order: list[str] | None = None,
    require_numbered_paragraphs: bool = False,
    continuous_paragraph_numbering: bool = True,
    paragraph_counter: list[int] | None = None,
) -> Document:
    """Render ordered draft sections.

    section_name is the authoritative section heading and is rendered as a Word heading by the
    section's level; section_content is the body. A leading content line duplicating section_name is
    stripped so the heading appears exactly once.
    """
    sections = reorder_sections(sections, section_order)
    after_bq = False
    counter = paragraph_counter if paragraph_counter is not None else [1]
    for sec in sections:
        section_name = str(sec.get("section_name") or "").strip()
        section_content = sec.get("section_content")
        has_body = bool(str(section_content or "").strip())
        if not section_name and not has_body:
            continue
        if section_name:
            heading_p = doc.add_paragraph(style=_section_heading_style(sec.get("level", 1)))
            _apply_inline_markdown_runs(heading_p, sanitize_word_text(section_name), force_bold=True)
            _center_heading_paragraph(heading_p)
            after_bq = False
        if has_body:
            after_bq = render_markdown_to_docx(
                doc,
                section_content,
                section_name=section_name,
                after_block_quote=after_bq,
                require_numbered_paragraphs=require_numbered_paragraphs,
                continuous_paragraph_numbering=continuous_paragraph_numbering,
                paragraph_counter=counter,
            )
    return doc


def create_everything_pleading_document(
    title: str,
    sections: list[dict],
    *,
    section_order: list[str] | None = None,
    format_spec: dict | None = None,
    user_contact: str | None = None,
) -> Document:
    """Build a Rule 10 + CAED-formatted document from draft title and sections."""
    export_spec = finalize_export_spec(
        format_spec,
        draft_title=title,
        user_contact=user_contact,
    )
    structural = get_structural_spec(export_spec)
    require_caption = structural.get("require_caption_page", False)
    use_vertical_lines = structural.get("use_vertical_lines", False)

    if require_caption:
        doc = create_document_with_caption(
            export_spec,
            draft_title=title,
            user_contact=user_contact,
        )
    else:
        doc = Document()

    apply_pleading_format_to_document(
        doc,
        export_spec,
        # Keep the pleading template's page margins only for vertical-lines (CA) pleading paper,
        # whose wider left margin is the line-number gutter. Other caption docs use the spec's
        # normal 1" margins; the auto-width caption table reflows to fit.
        preserve_page_layout=require_caption and use_vertical_lines,
    )

    caption = structural.get("caption") or {}
    skip_body_title = require_caption and bool(
        caption.get("pleading_title") or title
    )
    if not skip_body_title:
        add_document_title(doc, title, export_spec)

    # When a caption page is produced, drop the leading sections whose content is already caption
    # material so the body does not duplicate them. Filter against the original section order
    # (before reorder), non-destructively, and never drop the entire body.
    body_sections = sections
    if require_caption:
        drop = {i for i in (structural.get("caption_source_section_indices") or []) if isinstance(i, int)}
        if drop:
            filtered = [sec for i, sec in enumerate(sections) if i not in drop]
            if filtered:
                body_sections = filtered

    para_counter = [1]
    render_draft_sections_to_docx(
        doc,
        body_sections,
        section_order=section_order,
        require_numbered_paragraphs=structural.get("require_numbered_paragraphs", True),
        continuous_paragraph_numbering=structural.get(
            "continuous_paragraph_numbering", True
        ),
        paragraph_counter=para_counter,
    )
    return doc


def _ensure_caed_styles(doc: Document) -> None:
    try:
        doc.styles[STYLE_BODY]
    except KeyError:
        apply_pleading_format_to_document(doc)


def render_simple_markdown_to_docx(doc: Document, md_text: str) -> Document:
    """Render markdown into doc; applies CAED styles if not already configured."""
    _ensure_caed_styles(doc)
    render_markdown_to_docx(doc, md_text)
    return doc


def render_sections_markdown_word(
    doc: Document,
    sections: list[tuple[str, str]],
) -> Document:
    """Legacy API: sections as (content, title) tuples."""
    _ensure_caed_styles(doc)
    draft_sections = [
        {"section_name": title, "section_content": content}
        for content, title in sections
    ]
    render_draft_sections_to_docx(doc, draft_sections)
    return doc
