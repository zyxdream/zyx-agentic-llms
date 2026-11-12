"""Tests for FRCP Rule 10 caption page generation."""

import copy

from docx.enum.text import WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.shared import Pt

from backend.util.pleading_caption import (
    CAPTION_TEMPLATE_NO_LINES,
    CAPTION_TEMPLATE_VERTICAL_LINES,
    EVERYTHING_CAPTION_TEMPLATE,
    build_caption_fields,
    create_document_with_caption,
    resolve_caption_template_path,
)
from backend.util.pleading_format import DEFAULT_EXPORT_SPEC, DEFAULT_STRUCTURAL_SPEC
from backend.util.markdown_docx import create_everything_pleading_document
from backend.word_creator import _build_cover_page_replacements, _paragraph_element_text
from backend.everything import _add_page_number_footer, _footer_has_page_field


def _body_table_count(doc) -> int:
    return sum(1 for child in doc.element.body if child.tag == qn("w:tbl"))


def _page_break_body_index(doc) -> int | None:
    for i, child in enumerate(doc.element.body):
        if child.tag == qn("w:p") and child.xpath(".//w:br[@w:type='page']"):
            return i
    return None


def test_everything_caption_uses_no_vertical_lines_template_by_default():
    assert EVERYTHING_CAPTION_TEMPLATE == CAPTION_TEMPLATE_NO_LINES
    assert CAPTION_TEMPLATE_NO_LINES.endswith(
        "Federal-Pleading-Template-Google-Doc-Compatible-new-no-verticalines.docx"
    )


def test_resolve_caption_template_path_vertical_lines():
    export_spec = copy.deepcopy(DEFAULT_EXPORT_SPEC)
    export_spec["structural"]["use_vertical_lines"] = True
    assert resolve_caption_template_path(export_spec) == CAPTION_TEMPLATE_VERTICAL_LINES

    export_spec["structural"]["use_vertical_lines"] = False
    assert resolve_caption_template_path(export_spec) == CAPTION_TEMPLATE_NO_LINES


def _caption_enabled_export_spec(**overrides) -> dict:
    spec = copy.deepcopy(DEFAULT_EXPORT_SPEC)
    spec["structural"]["require_caption_page"] = True
    spec["structural"]["caption_ready"] = True
    for key, value in overrides.items():
        if key in spec["structural"]:
            spec["structural"][key] = value
        else:
            spec[key] = value
    return spec


def test_build_cover_page_replacements_new_template_keys():
    reps = _build_cover_page_replacements(
        attorneys_contact_info="Jane Attorney",
        venue="UNITED STATES DISTRICT COURT\nEASTERN DISTRICT OF CALIFORNIA",
        plaintiff="John Doe",
        defendant="Acme Corp.",
        title="COMPLAINT FOR DAMAGES",
        is_defendant=False,
        case_number="2:24-cv-00123",
        use_new_template=True,
    )
    assert reps["UNITED STATES DISTRICT COURT"] == "UNITED STATES DISTRICT COURT"
    assert reps["SOUTHERN DISTRICT OF CALIFORNIA"] == "EASTERN DISTRICT OF CALIFORNIA"
    assert reps["Plaintiff(s)"] == "Plaintiff"
    assert reps["Defendant(s)"] == "Defendant"
    assert reps["(Insert Case Number)"] == "2:24-cv-00123"


def test_build_caption_fields_uses_placeholders():
    fields = build_caption_fields(DEFAULT_STRUCTURAL_SPEC, draft_title="")
    assert fields["plaintiff"] == "(Plaintiff's Name)"
    assert fields["defendant"] == "(Defendant's Name)"
    assert "UNITED STATES DISTRICT COURT" in fields["venue"]
    assert fields["title"] == "TITLE OF PLEADING"


def test_build_caption_fields_uses_draft_title_and_contact():
    structural = copy.deepcopy(DEFAULT_STRUCTURAL_SPEC)
    fields = build_caption_fields(
        structural,
        draft_title="Motion to Dismiss",
        user_contact="Jane Attorney\n123 Main St",
    )
    assert fields["title"] == "Motion to Dismiss"
    assert "Jane Attorney" in fields["attorneys_contact_info"]


def test_build_caption_fields_fills_empty_district():
    structural = copy.deepcopy(DEFAULT_STRUCTURAL_SPEC)
    structural["caption"]["court_line_2"] = ""
    fields = build_caption_fields(structural)
    assert "___________" in fields["venue"]


def test_create_document_with_caption_preserves_court_line_font():
    export_spec = copy.deepcopy(DEFAULT_EXPORT_SPEC)
    export_spec["structural"]["caption"]["court_line_2"] = "EASTERN DISTRICT OF CALIFORNIA"
    doc = create_document_with_caption(
        export_spec,
        draft_title="COMPLAINT FOR DAMAGES",
        user_contact="Attorney Name",
    )
    court_paras = [
        p
        for p in doc.paragraphs
        if "UNITED STATES DISTRICT COURT" in p.text
        or "EASTERN DISTRICT OF CALIFORNIA" in p.text
    ]
    assert court_paras
    for para in court_paras:
        for run in para.runs:
            if run.text.strip():
                assert run.font.name == "Times New Roman"
                assert run.font.size is not None


def test_create_document_with_caption_spacing_and_blank_lines():
    export_spec = copy.deepcopy(DEFAULT_EXPORT_SPEC)
    export_spec["structural"]["caption"]["court_line_2"] = "EASTERN DISTRICT OF CALIFORNIA"
    doc = create_document_with_caption(
        export_spec,
        user_contact="Jane Attorney\n123 Main St",
        draft_title="COMPLAINT",
    )
    attorney_idx = court_idx = None
    for i, para in enumerate(doc.paragraphs):
        text = para.text.strip()
        if text.startswith("Attorney for"):
            attorney_idx = i
        if attorney_idx is not None and i > attorney_idx and "DISTRICT COURT" in text.upper():
            court_idx = i
            break
    assert attorney_idx is not None
    assert court_idx is not None
    blank_between = [
        doc.paragraphs[i].text.strip()
        for i in range(attorney_idx + 1, court_idx)
    ]
    assert blank_between == ["", ""]
    assert len(doc.paragraphs[0].runs) >= 1
    assert "\n" not in doc.paragraphs[0].text or len(doc.paragraphs[0].runs) > 1

    contact_pf = doc.paragraphs[0].paragraph_format
    assert contact_pf.line_spacing == Pt(12)

    attorney_pf = doc.paragraphs[attorney_idx].paragraph_format
    assert attorney_pf.line_spacing == Pt(12)

    court_pf = doc.paragraphs[court_idx].paragraph_format
    assert court_pf.line_spacing == Pt(24)

    district_pf = doc.paragraphs[court_idx + 1].paragraph_format
    assert district_pf.line_spacing == Pt(24)

    for i in range(attorney_idx + 1, court_idx):
        assert doc.paragraphs[i].paragraph_format.line_spacing == Pt(12)


def test_create_document_with_caption_has_paragraphs():
    export_spec = copy.deepcopy(DEFAULT_EXPORT_SPEC)
    export_spec["structural"]["caption"]["pleading_title"] = "COMPLAINT FOR DAMAGES"
    doc = create_document_with_caption(
        export_spec,
        draft_title="COMPLAINT FOR DAMAGES",
        user_contact="Attorney Name",
    )
    assert len(doc.paragraphs) > 0
    texts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                texts.append(cell.text)
    assert any("COMPLAINT" in t.upper() for t in texts)


def test_create_document_with_caption_no_tail_after_title():
    export_spec = copy.deepcopy(DEFAULT_EXPORT_SPEC)
    doc = create_document_with_caption(
        export_spec,
        draft_title="COMPLAINT FOR DAMAGES",
        user_contact="Attorney Name",
    )

    all_text = " ".join(p.text for p in doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                all_text += " " + cell.text

    assert "first numbered paragraph" not in all_text.lower()
    assert "second numbered paragraph" not in all_text.lower()
    assert "COMPLAINT FOR DAMAGES" in all_text
    assert _body_table_count(doc) == 1

    page_break_idx = _page_break_body_index(doc)
    assert page_break_idx is not None
    prev = doc.element.body[page_break_idx - 1]
    assert prev.tag == qn("w:tbl")
    assert "COMPLAINT FOR DAMAGES" in _paragraph_element_text(prev)
    assert "Case No.:" in _paragraph_element_text(prev)

    for child in list(doc.element.body)[page_break_idx + 1 :]:
        if child.tag == qn("w:sectPr"):
            continue
        if child.tag == qn("w:p") and child.xpath(".//w:br[@w:type='page']"):
            continue
        if child.tag == qn("w:p") and not _paragraph_element_text(child).strip():
            continue
        if child.tag == qn("w:tbl"):
            raise AssertionError("Unexpected table after caption page break")
        if child.tag == qn("w:p") and _paragraph_element_text(child).strip():
            raise AssertionError(
                f"Unexpected paragraph after caption page break: "
                f"{_paragraph_element_text(child)!r}"
            )


def test_caption_export_preserves_template_footer():
    doc = create_everything_pleading_document(
        "COMPLAINT FOR DAMAGES",
        [{"section_name": "Facts", "section_content": "Body paragraph."}],
        format_spec=_caption_enabled_export_spec(),
    )
    assert _footer_has_page_field(doc)

    section = doc.sections[0]
    assert abs(section.bottom_margin.inches - 1.0) < 0.01
    assert abs(section.footer_distance.inches - 0.2) < 0.01

    footer = section.footer
    footer_xml = footer.paragraphs[0]._element.xml if footer.paragraphs else ""
    assert "PAGE" in footer_xml
    assert footer.paragraphs[0].text.strip().startswith("-")

    text_runs = [r for r in footer.paragraphs[0].runs if r.text.strip()]
    assert text_runs
    assert text_runs[0].font.size == Pt(10)

    _add_page_number_footer(doc)
    assert footer.paragraphs[0].text.strip().startswith("-")
    assert " - " not in footer.paragraphs[0].text
    assert abs(section.bottom_margin.inches - 1.0) < 0.01
    assert text_runs[0].font.size == Pt(10)
