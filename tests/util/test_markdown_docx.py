"""Tests for CAED markdown → Word export."""

import copy
import tempfile
from pathlib import Path

import pytest
from docx import Document
from docx.enum.text import WD_LINE_SPACING, WD_PARAGRAPH_ALIGNMENT
from docx.shared import Inches, Pt

from backend.util.markdown_docx import (
    _line_matches_section_name,
    _strip_duplicate_section_heading,
    create_everything_pleading_document,
    normalize_markdown,
    reorder_sections,
    render_markdown_to_docx,
)
from backend.util.pleading_format import (
    DEFAULT_EXPORT_SPEC,
    STYLE_BODY,
    STYLE_BODY_CONTINUED,
    STYLE_BLOCK_QUOTE,
    STYLE_NUMBERED_PARAGRAPH,
    STYLE_TITLE,
    STYLE_TITLE1,
    STYLE_TITLE2,
    STYLE_TITLE3,
    apply_pleading_format_to_document,
)


def _body_only_export_spec():
    spec = copy.deepcopy(DEFAULT_EXPORT_SPEC)
    spec["structural"]["require_caption_page"] = False
    return spec


def _caption_enabled_export_spec():
    spec = copy.deepcopy(DEFAULT_EXPORT_SPEC)
    spec["structural"]["require_caption_page"] = True
    spec["structural"]["caption_ready"] = True
    return spec


def test_normalize_markdown_html_italic():
    assert normalize_markdown("See <i>Smith v. Jones</i>.") == "See *Smith v. Jones*."


def test_reorder_sections_respects_frontend_order():
    sections = [
        {"section_name": "B", "section_content": "b"},
        {"section_name": "A", "section_content": "a"},
        {"section_name": "C", "section_content": "c"},
    ]
    ordered = reorder_sections(sections, ["C", "A", "B"])
    assert [s["section_name"] for s in ordered] == ["C", "A", "B"]


def test_line_matches_section_name_variants():
    assert _line_matches_section_name("**Introduction**", "Introduction")
    assert _line_matches_section_name("# Introduction", "Introduction")
    assert _line_matches_section_name("INTRODUCTION:", "Introduction")
    assert _line_matches_section_name("I. Introduction", "Introduction")
    assert not _line_matches_section_name("Plain body only.", "Introduction")


def test_strip_duplicate_removes_markdown_heading():
    # section_name is rendered as the heading by the exporter, so a duplicate markdown heading at
    # the top of the body is stripped too (prevents a doubled heading).
    stripped = _strip_duplicate_section_heading("# Introduction\n\nBody.", "Introduction")
    assert stripped == "Body."


def test_strip_duplicate_removes_plain_and_bold():
    assert _strip_duplicate_section_heading(
        "**Introduction**\n\nBody.", "Introduction"
    ) == "Body."
    assert _strip_duplicate_section_heading(
        "Introduction\n\nBody.", "Introduction"
    ) == "Body."


def test_apply_pleading_format_margins_and_body_spacing():
    doc = Document()
    apply_pleading_format_to_document(doc)
    section = doc.sections[0]
    assert section.left_margin == Inches(1.0)
    assert section.right_margin == Inches(1.0)
    body_style = doc.styles[STYLE_BODY]
    pf = body_style.paragraph_format
    assert pf.first_line_indent == Inches(0.5)
    assert pf.line_spacing == Pt(24)
    assert pf.line_spacing_rule == WD_LINE_SPACING.EXACTLY


def test_numbered_paragraph_style_matches_body_first_line_indent():
    doc = Document()
    apply_pleading_format_to_document(doc)
    body_pf = doc.styles[STYLE_BODY].paragraph_format
    numbered_pf = doc.styles[STYLE_NUMBERED_PARAGRAPH].paragraph_format
    assert numbered_pf.left_indent == Inches(0)
    assert numbered_pf.first_line_indent == body_pf.first_line_indent


def test_render_markdown_preserves_inline_formatting():
    doc = Document()
    apply_pleading_format_to_document(doc)
    render_markdown_to_docx(
        doc,
        "Plain text.\n\n**Bold header**\n\n*Italic case* and **bold**.",
    )
    texts = []
    for p in doc.paragraphs:
        for run in p.runs:
            texts.append((run.text, run.bold, run.italic))
    assert any(t[0] == "Bold header" and t[1] for t in texts)
    assert any("Italic case" in t[0] and t[2] for t in texts)


def test_render_standalone_bold_line_as_centered_header():
    doc = Document()
    apply_pleading_format_to_document(doc)
    render_markdown_to_docx(
        doc,
        "**A. The Antitrust Claims Still Fail**\n\nBody paragraph.",
        require_numbered_paragraphs=True,
    )

    heading = next(p for p in doc.paragraphs if p.text.strip() == "A. The Antitrust Claims Still Fail")
    assert heading.alignment == WD_PARAGRAPH_ALIGNMENT.CENTER
    assert heading.style.name != STYLE_NUMBERED_PARAGRAPH
    assert heading.runs and all(run.bold for run in heading.runs)


def test_render_inline_bold_stays_inline_not_centered_header():
    doc = Document()
    apply_pleading_format_to_document(doc)
    render_markdown_to_docx(doc, "This paragraph has **inline bold** text.")

    paragraph = next(p for p in doc.paragraphs if "inline bold" in p.text)
    assert paragraph.alignment != WD_PARAGRAPH_ALIGNMENT.CENTER
    assert any(run.text == "inline bold" and run.bold for run in paragraph.runs)


def test_render_markdown_headings_use_caed_styles():
    doc = Document()
    apply_pleading_format_to_document(doc)
    render_markdown_to_docx(doc, "# Top\n\n## Sub\n\n### Subsub\n\nBody line.")
    styles = [p.style.name for p in doc.paragraphs if p.text.strip()]
    assert STYLE_TITLE1 in styles
    assert STYLE_TITLE2 in styles
    assert STYLE_TITLE3 in styles
    assert STYLE_BODY in styles


def test_render_block_quote_and_continued_body():
    doc = Document()
    apply_pleading_format_to_document(doc)
    render_markdown_to_docx(doc, "> Quoted passage.\n\nFollowing paragraph.")
    style_names = [p.style.name for p in doc.paragraphs if p.text.strip()]
    assert STYLE_BLOCK_QUOTE in style_names
    assert STYLE_BODY_CONTINUED in style_names


def test_render_numbered_paragraphs_use_numbered_style():
    doc = Document()
    apply_pleading_format_to_document(doc)
    render_markdown_to_docx(
        doc,
        "1. First allegation.\n\n2. Second allegation.",
        require_numbered_paragraphs=True,
    )
    numbered = [p for p in doc.paragraphs if p.style.name == STYLE_NUMBERED_PARAGRAPH]
    assert len(numbered) == 2
    assert numbered[0].text.startswith("1.")
    assert numbered[1].text.startswith("2.")


def test_auto_number_plain_body_paragraphs():
    doc = Document()
    apply_pleading_format_to_document(doc)
    render_markdown_to_docx(
        doc,
        "**JURISDICTION AND VENUE**\n\n"
        "First allegation sentence.\n\n"
        "Second allegation sentence.",
        require_numbered_paragraphs=True,
    )
    numbered = [p for p in doc.paragraphs if p.style.name == STYLE_NUMBERED_PARAGRAPH]
    assert len(numbered) == 2
    assert numbered[0].text.startswith("1. First allegation")
    assert numbered[1].text.startswith("2. Second allegation")
    headers = [p for p in doc.paragraphs if "JURISDICTION AND VENUE" in p.text]
    assert len(headers) == 1
    assert headers[0].style.name != STYLE_NUMBERED_PARAGRAPH


def test_auto_number_skips_all_caps_headers():
    doc = Document()
    apply_pleading_format_to_document(doc)
    render_markdown_to_docx(
        doc,
        "FACTUAL BACKGROUND\n\nPlaintiff alleges breach of contract.",
        require_numbered_paragraphs=True,
    )
    numbered = [p for p in doc.paragraphs if p.style.name == STYLE_NUMBERED_PARAGRAPH]
    assert len(numbered) == 1
    assert numbered[0].text.startswith("1. Plaintiff alleges")
    caps = [p for p in doc.paragraphs if p.text.strip() == "FACTUAL BACKGROUND"]
    assert len(caps) == 1
    assert caps[0].style.name != STYLE_NUMBERED_PARAGRAPH


def test_continuous_numbering_plain_paragraphs_across_sections():
    doc = Document()
    apply_pleading_format_to_document(doc)
    counter = [1]
    render_markdown_to_docx(
        doc,
        "First in section A.\n\nSecond in section A.",
        require_numbered_paragraphs=True,
        paragraph_counter=counter,
    )
    render_markdown_to_docx(
        doc,
        "First in section B.",
        require_numbered_paragraphs=True,
        paragraph_counter=counter,
    )
    numbered = [p for p in doc.paragraphs if p.style.name == STYLE_NUMBERED_PARAGRAPH]
    assert len(numbered) == 3
    assert numbered[0].text.startswith("1.")
    assert numbered[1].text.startswith("2.")
    assert numbered[2].text.startswith("3.")


def test_continuous_numbering_across_sections():
    doc = Document()
    apply_pleading_format_to_document(doc)
    counter = [1]
    render_markdown_to_docx(
        doc,
        "1. First in section A.",
        require_numbered_paragraphs=True,
        continuous_paragraph_numbering=True,
        paragraph_counter=counter,
    )
    render_markdown_to_docx(
        doc,
        "1. First in section B.",
        require_numbered_paragraphs=True,
        continuous_paragraph_numbering=True,
        paragraph_counter=counter,
    )
    numbered = [p for p in doc.paragraphs if p.style.name == STYLE_NUMBERED_PARAGRAPH]
    assert len(numbered) == 2
    assert numbered[0].text.startswith("1.")
    assert numbered[1].text.startswith("2.")


def test_create_everything_document_skips_duplicate_section_heading():
    # section_name renders as the heading; a bold duplicate at the top of the body is stripped,
    # so "Introduction" appears exactly once (as the heading) and the body renders once.
    doc = create_everything_pleading_document(
        "Motion to Dismiss",
        [
            {
                "section_name": "Introduction",
                "section_content": "**Introduction**\n\nFirst paragraph.",
            }
        ],
        format_spec=_body_only_export_spec(),
    )
    body_paras = [
        p for p in doc.paragraphs
        if p.style.name == STYLE_NUMBERED_PARAGRAPH and "First paragraph" in p.text
    ]
    assert len(body_paras) == 1
    intro_paras = [p for p in doc.paragraphs if p.text.strip() == "Introduction"]
    assert len(intro_paras) == 1
    assert intro_paras[0].style.name == STYLE_TITLE1


def test_create_everything_document_markdown_heading_once():
    doc = create_everything_pleading_document(
        "Motion",
        [
            {
                "section_name": "Introduction",
                "section_content": "# Introduction\n\nFirst paragraph.",
            }
        ],
        format_spec=_body_only_export_spec(),
    )
    intro_title1 = [
        p for p in doc.paragraphs
        if p.style.name == STYLE_TITLE1 and p.text.strip() == "Introduction"
    ]
    assert len(intro_title1) == 1


def test_create_everything_document_renders_section_and_subheadings():
    # section_name -> Heading 1; standalone bold lines in the body -> centered bold headers.
    doc = create_everything_pleading_document(
        "Motion",
        [
            {
                "section_name": "Argument",
                "level": 1,
                "section_content": "Lead paragraph.\n\n**Standard of Review**\n\nBody.\n\n**Subpoint**\n\nMore.",
            }
        ],
        format_spec=_body_only_export_spec(),
    )
    by_para = {p.text.strip(): p for p in doc.paragraphs if p.text.strip()}
    assert by_para["Argument"].style.name == STYLE_TITLE1
    assert by_para["Standard of Review"].style.name != STYLE_NUMBERED_PARAGRAPH
    assert by_para["Subpoint"].style.name != STYLE_NUMBERED_PARAGRAPH
    # section heading and bold paragraph headers are bold and centered; body text is neither.
    for heading in ("Argument", "Standard of Review", "Subpoint"):
        assert by_para[heading].runs and all(r.bold for r in by_para[heading].runs)
        assert by_para[heading].alignment == WD_PARAGRAPH_ALIGNMENT.CENTER
    body = next(p for p in doc.paragraphs if "Lead paragraph." in p.text)
    assert body.runs and not any(r.bold for r in body.runs)
    assert body.alignment != WD_PARAGRAPH_ALIGNMENT.CENTER


def test_create_everything_document_preserve_original_style_heading_once():
    # preserve_original shape: section_name set, plain body with no markdown heading -> the heading
    # still renders exactly once from section_name.
    doc = create_everything_pleading_document(
        "Hallucinated Brief.pdf",
        [{"section_name": "Preliminary Statement", "section_content": "The debtors are entities."}],
        format_spec=_body_only_export_spec(),
    )
    headings = [p for p in doc.paragraphs if p.text.strip() == "Preliminary Statement"]
    assert len(headings) == 1
    assert headings[0].style.name == STYLE_TITLE1


def test_create_everything_document_renders_section_name_heading():
    # section_name is the authoritative heading: it renders as a Word heading even when the body
    # carries no markdown heading of its own.
    doc = create_everything_pleading_document(
        "Motion",
        [{"section_name": "Facts", "section_content": "- Item one\n- Item two"}],
        format_spec=_body_only_export_spec(),
    )
    facts_headers = [
        p for p in doc.paragraphs
        if p.text.strip() == "Facts" and p.style.name == STYLE_TITLE1
    ]
    assert len(facts_headers) == 1


def test_create_everything_document_writes_to_disk():
    doc = create_everything_pleading_document(
        "Test Pleading",
        [{"section_name": "Facts", "section_content": "- Item one\n- Item two"}],
        format_spec=_body_only_export_spec(),
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.docx"
        doc.save(str(path))
        assert path.exists()
        loaded = Document(str(path))
        assert any(p.style.name == STYLE_TITLE for p in loaded.paragraphs)


def test_create_everything_document_includes_caption_when_enabled():
    doc = create_everything_pleading_document(
        "Complaint for Damages",
        [{"section_name": "Facts", "section_content": "Body text."}],
        format_spec=_caption_enabled_export_spec(),
    )
    assert len(doc.paragraphs) > 0
    body_titles = [p for p in doc.paragraphs if p.style.name == STYLE_TITLE]
    assert len(body_titles) == 0


def test_create_everything_document_strips_caption_source_sections_from_body():
    spec = _caption_enabled_export_spec()
    spec["structural"]["caption_source_section_indices"] = [0]
    doc = create_everything_pleading_document(
        "Complaint",
        [
            {"section_name": "", "section_content": "UNITED STATES DISTRICT COURT\nJohn Doe v. Acme Corp.\nCase No. 1:23-cv-001"},
            {"section_name": "Preliminary Statement", "section_content": "The plaintiff alleges harm."},
        ],
        format_spec=spec,
    )
    full_text = "\n".join(p.text for p in doc.paragraphs)
    # caption-source leading section is not duplicated in the body
    assert "John Doe v. Acme Corp." not in full_text
    # the real body section still renders
    assert any(p.text.strip() == "Preliminary Statement" for p in doc.paragraphs)
    assert "The plaintiff alleges harm." in full_text


def test_create_everything_document_no_caption_source_renders_full_body():
    spec = _caption_enabled_export_spec()
    spec["structural"]["caption_source_section_indices"] = []
    doc = create_everything_pleading_document(
        "Complaint",
        [{"section_name": "Preliminary Statement", "section_content": "Alleged facts."}],
        format_spec=spec,
    )
    assert any(p.text.strip() == "Preliminary Statement" for p in doc.paragraphs)


def test_create_everything_document_all_marked_falls_back_to_full_body():
    spec = _caption_enabled_export_spec()
    spec["structural"]["caption_source_section_indices"] = [0]
    doc = create_everything_pleading_document(
        "Complaint",
        [{"section_name": "Only Section", "section_content": "Body text."}],
        format_spec=spec,
    )
    # dropping every section is a degenerate response; full body is rendered instead
    assert any(p.text.strip() == "Only Section" for p in doc.paragraphs)


def test_create_everything_document_does_not_mutate_input_sections():
    spec = _caption_enabled_export_spec()
    spec["structural"]["caption_source_section_indices"] = [0]
    sections = [
        {"section_name": "Caption", "section_content": "UNITED STATES DISTRICT COURT"},
        {"section_name": "Body", "section_content": "Text."},
    ]
    create_everything_pleading_document("Complaint", sections, format_spec=spec)
    assert len(sections) == 2
    assert sections[0]["section_name"] == "Caption"


def test_create_everything_document_caption_uses_one_inch_margins_without_vertical_lines():
    # Ordinary federal caption doc (no vertical lines): page margins are the normal 1", not the
    # pleading template's 1.25" left / 0.55" right.
    doc = create_everything_pleading_document(
        "Complaint for Damages",
        [{"section_name": "Facts", "section_content": "Body text."}],
        format_spec=_caption_enabled_export_spec(),
    )
    assert doc.sections[0].left_margin == Inches(1.0)
    assert doc.sections[0].right_margin == Inches(1.0)


def test_create_everything_document_caption_preserves_gutter_with_vertical_lines():
    # CA pleading paper (vertical lines): the template's wider left gutter is preserved for the
    # printed line-number rule.
    spec = _caption_enabled_export_spec()
    spec["structural"]["use_vertical_lines"] = True
    doc = create_everything_pleading_document(
        "Complaint for Damages",
        [{"section_name": "Facts", "section_content": "Body text."}],
        format_spec=spec,
    )
    assert doc.sections[0].left_margin == Inches(1.25)


def test_create_everything_document_analysis_has_no_caption_page():
    doc = create_everything_pleading_document(
        "Internal Case Analysis",
        [{"section_name": "Summary", "section_content": "Analysis text."}],
        format_spec=_body_only_export_spec(),
    )
    from docx.oxml.ns import qn

    assert not any(child.tag == qn("w:tbl") for child in doc.element.body)
    assert not any(
        child.tag == qn("w:p") and child.xpath(".//w:br[@w:type='page']")
        for child in doc.element.body
    )
    body_titles = [p for p in doc.paragraphs if p.style.name == STYLE_TITLE]
    assert len(body_titles) == 1
