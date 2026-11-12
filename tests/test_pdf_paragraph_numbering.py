import pytest

from backend import pdf_creator


def _flowable_texts(flowables):
    return [
        flowable.text.strip()
        for flowable in flowables
        if isinstance(flowable, pdf_creator.LegalDocText)
    ]


def test_complaint_pdf_renumbers_facts_without_removing_factual_numbers():
    introduction = "\n".join(
        f"Introductory allegation {number}." for number in range(1, 14)
    )
    facts = (
        "# Research and development\n"
        "1. Google began the project in 2009.\n"
        "2016, the program became Waymo.\n"
        "14,000 miles were driven during testing.\n"
        "3.5 million miles were later recorded."
    )

    flowables, numbered_text, last_number = pdf_creator.complaint_create_section_text(
        [(introduction, "INTRODUCTION"), (facts, "FACTUAL ALLEGATIONS")]
    )

    # Exporters own continuous numbering; saved LLM numbering must not become
    # "14. 1.", and genuine years, quantities, and decimals must not be stripped.
    expected_facts = [
        "14. Google began the project in 2009.",
        "15. 2016, the program became Waymo.",
        "16. 14,000 miles were driven during testing.",
        "17. 3.5 million miles were later recorded.",
    ]
    assert numbered_text.splitlines()[13:] == expected_facts
    assert last_number == 17
    assert _flowable_texts(flowables)[-4:] == expected_facts
    assert "<b>Research and development</b>" in _flowable_texts(flowables)
    assert facts.startswith("# Research and development\n1. Google")


@pytest.mark.parametrize("renderer", ["answer", "discovery", "motion"])
def test_shared_pdf_number_cleanup_preserves_numeric_content(renderer):
    section = (
        "8. This paragraph already has a number.\n"
        "2016, the relevant period began.\n"
        "14,000 records were reviewed.\n"
        "3.5 million dollars were at issue."
    )
    sections = [(section, "BACKGROUND")]
    if renderer == "answer":
        flowables, _ = pdf_creator.answer_create_section_text(sections)
        texts = _flowable_texts(flowables)
    elif renderer == "discovery":
        flowables = pdf_creator.create_section_text_rfp(sections)
        texts = [text.replace("<br />", "").strip() for text in _flowable_texts(flowables)]
    else:
        flowables = pdf_creator.create_section_text_dismiss(
            sections, avail_w=450, avail_h=600
        )
        texts = [
            flowable.getPlainText()
            for flowable in flowables
            if isinstance(flowable, pdf_creator.Paragraph)
        ]

    assert texts[-4:] == [
        "1. This paragraph already has a number.",
        "2. 2016, the relevant period began.",
        "3. 14,000 records were reviewed.",
        "4. 3.5 million dollars were at issue.",
    ]
