"""Shared assertions for generated PDF and Word pleading artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from pypdf import PdfReader


@dataclass(frozen=True)
class PDFValidation:
    path: Path
    page_count: int
    text: str


@dataclass(frozen=True)
class WordValidation:
    path: Path
    paragraph_count: int
    text: str


def _expected_terms(case: Mapping[str, Any]) -> list[str]:
    causes_of_action = case.get("causes_of_action") or []
    claim_text = causes_of_action[2] if len(causes_of_action) > 2 else ""
    return [
        str(case["venue"]),
        str(case["plaintiff"]),
        str(case["defendant"]),
        "written",
        "agreement",
        "breached",
        "damages",
        claim_text,
    ]


def _assert_common_complaint_content(text: str, case: Mapping[str, Any], label: str) -> None:
    normalized = " ".join(text.casefold().split())
    missing = [
        term
        for term in _expected_terms(case)
        if term and " ".join(term.casefold().split()) not in normalized
    ]
    assert not missing, f"{label} missing complaint content: {missing}"
    assert "united states district court" in normalized
    assert "first cause of action" in normalized
    assert "request for relief" in normalized


def _word_text(document: Any) -> str:
    paragraphs = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        paragraphs.extend(
            cell.text
            for row in table.rows
            for cell in row.cells
        )
    return "\n".join(paragraphs)


def validate_complaint_pdf(
    path: str | Path,
    case: Mapping[str, Any],
    *,
    minimum_pages: int = 2,
) -> PDFValidation:
    """Read a generated complaint PDF and assert its common contract."""

    path = Path(path)
    assert path.is_file(), f"PDF artifact does not exist: {path}"
    reader = PdfReader(str(path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    assert len(reader.pages) >= minimum_pages, (
        f"PDF should contain at least {minimum_pages} pages, "
        f"but contained {len(reader.pages)}"
    )
    _assert_common_complaint_content(text, case, "PDF")
    return PDFValidation(path=path, page_count=len(reader.pages), text=text)


def validate_complaint_word(
    path: str | Path,
    case: Mapping[str, Any],
    *,
    heading_style: str = "Heading 1",
    numbered_style: str = "List Number",
) -> WordValidation:
    """Read a generated complaint Word document and assert its common contract."""

    path = Path(path)
    assert path.is_file(), f"Word artifact does not exist: {path}"
    document = Document(str(path))
    text = _word_text(document)
    _assert_common_complaint_content(text, case, "Word document")

    cause_heading = next(
        (
            paragraph
            for paragraph in document.paragraphs
            if paragraph.text.strip() == "FIRST CAUSE OF ACTION"
        ),
        None,
    )
    assert cause_heading is not None, "Word document is missing the cause-of-action heading"
    assert cause_heading.style.name == heading_style
    assert cause_heading.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert cause_heading.style.font.bold is True

    numbered_paragraphs = [
        paragraph
        for paragraph in document.paragraphs
        if paragraph.style.name == numbered_style
    ]
    assert numbered_paragraphs, f"Word document has no {numbered_style!r} paragraphs"
    expected_claim = str(case.get("causes_of_action", ["", "", ""])[2])
    assert any(expected_claim in paragraph.text for paragraph in numbered_paragraphs)
    return WordValidation(
        path=path,
        paragraph_count=len(document.paragraphs),
        text=text,
    )
