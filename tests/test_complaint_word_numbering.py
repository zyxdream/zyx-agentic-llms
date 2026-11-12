"""Complaint Word exports own paragraph numbering, including for older drafts."""

from io import BytesIO

import pytest
from docx import Document
from docx.oxml.ns import qn

from backend.word_creator import complaint_create_sections_word, create_complaint_word


def _render_sections(sections):
    document = complaint_create_sections_word(Document(), sections)
    saved = BytesIO()
    document.save(saved)
    saved.seek(0)
    return Document(saved)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("1. Google launched its self-driving project.", "Google launched its self-driving project."),
        ("14. Waymo invested in research.", "Waymo invested in research."),
        ("1.\tGoogle launched its self-driving project.", "Google launched its self-driving project."),
        ("Google launched its self-driving project.", "Google launched its self-driving project."),
        ("2016, the project became Waymo.", "2016, the project became Waymo."),
        ("14,000 miles were logged.", "14,000 miles were logged."),
        ("3.5 million miles were driven.", "3.5 million miles were driven."),
        ("8,836,922 is the patent number.", "8,836,922 is the patent number."),
        # The narrow fix removes one ordinary prefix, not arbitrarily nested numbering.
        ("1. 2. An already nested paragraph.", "2. An already nested paragraph."),
    ],
)
def test_factual_paragraph_removes_only_one_plain_number_prefix(source, expected):
    document = _render_sections([(source, "FACTUAL ALLEGATIONS")])
    paragraphs = [paragraph for paragraph in document.paragraphs if paragraph.style.name == "List Number"]

    assert [paragraph.text for paragraph in paragraphs] == [expected]


@pytest.mark.parametrize("marker", ["#", "###"])
def test_factual_subheadings_remain_headings_not_numbered_paragraphs(marker):
    document = _render_sections([
        (
            f"FACTUAL ALLEGATIONS\n{marker} A. Waymo's Research\n1. Waymo invested in research.",
            "FACTUAL ALLEGATIONS",
        ),
    ])

    assert [(paragraph.text, paragraph.style.name) for paragraph in document.paragraphs] == [
        ("FACTUAL ALLEGATIONS", "Heading 1"),
        ("A. Waymo's Research", "Heading 2"),
        ("Waymo invested in research.", "List Number"),
    ]


def test_factual_paragraphs_keep_preceding_sections_numbering_without_restart():
    document = Document()
    original_numbering = document.part.numbering_part.element.xml
    preceding = [f"Background allegation {index}." for index in range(1, 14)]
    complaint_create_sections_word(document, [
        ("\n".join(preceding), "INTRODUCTION"),
        (
            "FACTUAL ALLEGATIONS\n### A. Research\n1. Google launched the project.\n2. Waymo invested in research.",
            "FACTUAL ALLEGATIONS",
        ),
    ])
    numbered = [paragraph for paragraph in document.paragraphs if paragraph.style.name == "List Number"]

    assert [paragraph.text for paragraph in numbered] == preceding + [
        "Google launched the project.",
        "Waymo invested in research.",
    ]
    assert numbered[13].text == "Google launched the project."
    # Word displays these as paragraphs 14 and 15: all use the same inherited
    # numbering definition, with no new list or paragraph-level restart override.
    inherited_numbering = numbered[0].style.element.pPr.numPr.xml
    assert all(paragraph.style.element.pPr.numPr.xml == inherited_numbering for paragraph in numbered)
    assert all(paragraph._p.find(f"{qn('w:pPr')}/{qn('w:numPr')}") is None for paragraph in numbered)
    assert document.part.numbering_part.element.xml == original_numbering


def test_export_of_saved_numbered_facts_is_idempotent_and_does_not_rewrite_source(tmp_path):
    saved_sections = {
        "factual_allegations": "FACTUAL ALLEGATIONS\n### A. Research\n1. Google launched the project.\n2. In 2016, it became Waymo.",
    }
    original = dict(saved_sections)
    exports = []

    for attempt in range(2):
        output = tmp_path / f"complaint-{attempt}.docx"
        assert create_complaint_word(
            str(output),
            plaintiff="Waymo LLC",
            defendant="Uber Technologies, Inc.",
            attorneys_contact_info="Example Counsel\nExample Law Firm",
            venue="UNITED STATES DISTRICT COURT\nNORTHERN DISTRICT OF CALIFORNIA",
            introduction="This is a trade-secret action.",
            parties="Waymo is the plaintiff.",
            jurisdiction_n_venue="Venue is proper in this district.",
            factual_allegations=saved_sections["factual_allegations"],
            cause_of_action="",
            damages="Damages according to proof.",
            prayer_for_relief="",
        ) == str(output)
        exports.append(Document(output))
        assert saved_sections == original

    first_paragraphs = [(paragraph.text, paragraph.style.name) for paragraph in exports[0].paragraphs]
    assert first_paragraphs == [(paragraph.text, paragraph.style.name) for paragraph in exports[1].paragraphs]
    facts = [paragraph.text for paragraph in exports[0].paragraphs if paragraph.style.name == "List Number"]
    assert facts[-2:] == ["Google launched the project.", "In 2016, it became Waymo."]
