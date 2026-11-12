"""Pleading-paper layout and FRCP Rule 10 structural defaults for Word export."""

import copy

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.shared import Inches, Pt, RGBColor

# Style names used by markdown_docx renderer
STYLE_BODY = "CAED Body Text"
STYLE_BODY_CONTINUED = "CAED Body Text Continued"
STYLE_BLOCK_QUOTE = "CAED Block Quote"
STYLE_TITLE = "CAED Title"
STYLE_TITLE1 = "CAED Title 1"
STYLE_TITLE2 = "CAED Title 2"
STYLE_TITLE3 = "CAED Title 3"
STYLE_NUMBERED_PARAGRAPH = "CAED Numbered Paragraph"

DEFAULT_CAED_PLEADING_SPEC = {
    "source": "https://www.caed.uscourts.gov/caednew/assets/File/Pleading%20paper%20instructions.pdf",
    "font_name": "Times New Roman",
    "font_size_pt": 12,
    "page_margins_in": {"left": 1.0, "right": 1.0, "top": 1.0, "bottom": 1.0},
    "body": {
        "first_line_indent_in": 0.5,
        "line_spacing_pt": 24,
        "line_spacing_rule": "exact",
    },
    "body_continued": {"first_line_indent_in": 0.0, "line_spacing_pt": 24},
    "block_quote": {
        "left_indent_in": 1.0,
        "right_indent_in": 1.0,
        "line_spacing_pt": 12,
        "alignment": "justify",
    },
    "title": {"bold": True, "all_caps": True, "centered": True},
    "title1": {"bold": True, "left_indent_in": 0.5},
    "title2": {"left_indent_in": 1.0},
    "title3": {"left_indent_in": 1.5, "hanging_indent_in": 0.5},
}

DEFAULT_STRUCTURAL_SPEC = {
    "source_rule": "https://www.law.cornell.edu/rules/frcp/rule_10",
    "require_caption_page": False,
    "require_numbered_paragraphs": True,
    "continuous_paragraph_numbering": True,
    "use_vertical_lines": False,
    "caption_ready": False,
    # 0-based indices of leading draft sections that already hold caption material; dropped from the
    # body at export so a formatted caption page does not duplicate them. Empty unless require_caption_page.
    "caption_source_section_indices": [],
    "caption": {
        "court_line_1": "UNITED STATES DISTRICT COURT",
        "court_line_2": "",
        "plaintiff": "(Plaintiff's Name)",
        "defendant": "(Defendant's Name)",
        "case_number": "___________",
        "pleading_title": "TITLE OF PLEADING",
        "attorney_contact": "(your name & contact info)",
    },
}

DEFAULT_EXPORT_SPEC = {
    "structural": copy.deepcopy(DEFAULT_STRUCTURAL_SPEC),
    "layout": copy.deepcopy(DEFAULT_CAED_PLEADING_SPEC),
    "_meta": {},
}


def get_layout_spec(spec: dict | None) -> dict:
    """Return layout dict from nested export spec or legacy flat layout spec."""
    if not spec:
        return DEFAULT_CAED_PLEADING_SPEC
    if "layout" in spec and isinstance(spec["layout"], dict):
        return spec["layout"]
    if "font_name" in spec:
        return spec
    return DEFAULT_CAED_PLEADING_SPEC


def get_structural_spec(spec: dict | None) -> dict:
    """Return structural dict from export spec."""
    if spec and isinstance(spec.get("structural"), dict):
        return spec["structural"]
    return DEFAULT_STRUCTURAL_SPEC


def finalize_export_spec(
    export_spec: dict | None,
    *,
    draft_title: str = "",
    user_contact: str | None = None,
) -> dict:
    """Apply draft title and user contact to caption fields before export."""
    spec = copy.deepcopy(export_spec or DEFAULT_EXPORT_SPEC)
    if "structural" not in spec:
        spec = {
            "structural": copy.deepcopy(DEFAULT_STRUCTURAL_SPEC),
            "layout": get_layout_spec(spec),
            "_meta": spec.get("_meta", {}),
        }
    caption = spec["structural"].setdefault("caption", {})
    title = (draft_title or "").strip()
    if title and caption.get("pleading_title") in ("", "TITLE OF PLEADING", None):
        caption["pleading_title"] = title
    if user_contact and str(user_contact).strip():
        caption["attorney_contact"] = str(user_contact).strip()
    return spec


def _ensure_paragraph_style(doc: Document, name: str, base_name: str = "Normal"):
    try:
        return doc.styles[name]
    except KeyError:
        style = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = doc.styles[base_name]
        return style


def _set_font(style, font_name: str, size_pt: int, bold: bool = False):
    style.font.name = font_name
    style.font.size = Pt(size_pt)
    style.font.bold = bold
    style.font.color.rgb = RGBColor(0, 0, 0)


def _set_exact_line_spacing(pf, spacing_pt: int):
    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    pf.line_spacing = Pt(spacing_pt)
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)


def apply_pleading_format_to_document(
    doc: Document,
    spec: dict | None = None,
    *,
    preserve_page_layout: bool = False,
) -> Document:
    """Apply pleading-paper margins and paragraph styles (accepts export or layout spec)."""
    layout = get_layout_spec(spec)
    font_name = layout["font_name"]
    size_pt = layout["font_size_pt"]
    margins = layout["page_margins_in"]

    if not preserve_page_layout:
        for section in doc.sections:
            section.left_margin = Inches(margins["left"])
            section.right_margin = Inches(margins["right"])
            section.top_margin = Inches(margins["top"])
            section.bottom_margin = Inches(margins["bottom"])

    normal = doc.styles["Normal"]
    _set_font(normal, font_name, size_pt)
    _set_exact_line_spacing(normal.paragraph_format, layout["body"]["line_spacing_pt"])

    body_spec = layout["body"]
    body = _ensure_paragraph_style(doc, STYLE_BODY)
    _set_font(body, font_name, size_pt)
    bpf = body.paragraph_format
    bpf.first_line_indent = Inches(body_spec["first_line_indent_in"])
    _set_exact_line_spacing(bpf, body_spec["line_spacing_pt"])

    cont_spec = layout["body_continued"]
    body_cont = _ensure_paragraph_style(doc, STYLE_BODY_CONTINUED)
    _set_font(body_cont, font_name, size_pt)
    cpf = body_cont.paragraph_format
    cpf.first_line_indent = Inches(cont_spec["first_line_indent_in"])
    _set_exact_line_spacing(cpf, cont_spec["line_spacing_pt"])

    bq_spec = layout["block_quote"]
    block = _ensure_paragraph_style(doc, STYLE_BLOCK_QUOTE)
    _set_font(block, font_name, size_pt)
    bqpf = block.paragraph_format
    bqpf.left_indent = Inches(bq_spec["left_indent_in"])
    bqpf.right_indent = Inches(bq_spec["right_indent_in"])
    bqpf.first_line_indent = Inches(0)
    _set_exact_line_spacing(bqpf, bq_spec["line_spacing_pt"])
    if bq_spec.get("alignment") == "justify":
        bqpf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    title_spec = layout["title"]
    title = _ensure_paragraph_style(doc, STYLE_TITLE)
    _set_font(title, font_name, size_pt, bold=title_spec.get("bold", True))
    tpf = title.paragraph_format
    if title_spec.get("centered"):
        tpf.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_exact_line_spacing(tpf, body_spec["line_spacing_pt"])

    t1_spec = layout["title1"]
    t1 = _ensure_paragraph_style(doc, STYLE_TITLE1)
    _set_font(t1, font_name, size_pt, bold=t1_spec.get("bold", True))
    t1pf = t1.paragraph_format
    t1pf.left_indent = Inches(t1_spec["left_indent_in"])
    t1pf.first_line_indent = Inches(0)
    _set_exact_line_spacing(t1pf, body_spec["line_spacing_pt"])

    t2_spec = layout["title2"]
    t2 = _ensure_paragraph_style(doc, STYLE_TITLE2)
    _set_font(t2, font_name, size_pt)
    t2pf = t2.paragraph_format
    t2pf.left_indent = Inches(t2_spec["left_indent_in"])
    t2pf.first_line_indent = Inches(0)
    _set_exact_line_spacing(t2pf, body_spec["line_spacing_pt"])

    t3_spec = layout["title3"]
    t3 = _ensure_paragraph_style(doc, STYLE_TITLE3)
    _set_font(t3, font_name, size_pt)
    t3pf = t3.paragraph_format
    t3pf.left_indent = Inches(t3_spec["left_indent_in"])
    t3pf.first_line_indent = Inches(-t3_spec["hanging_indent_in"])
    _set_exact_line_spacing(t3pf, body_spec["line_spacing_pt"])

    numbered = _ensure_paragraph_style(doc, STYLE_NUMBERED_PARAGRAPH)
    _set_font(numbered, font_name, size_pt)
    npf = numbered.paragraph_format
    npf.left_indent = Inches(0)
    npf.first_line_indent = Inches(body_spec["first_line_indent_in"])
    _set_exact_line_spacing(npf, body_spec["line_spacing_pt"])

    return doc


def add_document_title(doc: Document, title: str, spec: dict | None = None) -> None:
    """Add pleading title using CAED Title style (bold, ALL CAPS, centered)."""
    layout = get_layout_spec(spec)
    text = title.strip() or "Legal Document"
    if layout["title"].get("all_caps"):
        text = text.upper()
    p = doc.add_paragraph(style=STYLE_TITLE)
    run = p.add_run(text)
    run.bold = layout["title"].get("bold", True)
