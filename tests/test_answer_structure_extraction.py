import json
import pytest

from backend import answer_drafter
from tests.answer_parse_test_support import install_legacy_answer_json_mock


@pytest.fixture(autouse=True)
def _legacy_answer_json_mock(monkeypatch):
    install_legacy_answer_json_mock(monkeypatch)


def _metadata_for_pages(pages: list[int]) -> list[dict]:
    return [
        {
            "line_number": index,
            "page_number": page_number,
            "page_line_number": index % 2,
            "bold": index == 0,
            "ocr": page_number > 10,
        }
        for index, page_number in enumerate(pages)
    ]


def test_answer_structure_prompt_uses_first_twenty_pdf_pages():
    lines = [f"line {index}" for index in range(44)]
    pages = [(index // 2) + 1 for index in range(44)]

    prompt, source_rows = answer_drafter._build_answer_structure_prompt(
        lines,
        _metadata_for_pages(pages),
    )

    assert len(source_rows) == 40
    assert all(row["page_number"] <= 20 for row in source_rows)
    assert source_rows[0] == {
        "line_number": 0,
        "page_number": 1,
        "page_line_number": 0,
        "bold": True,
        "ocr": False,
        "text": "line 0",
    }
    assert '"line_number": 39' in prompt
    assert '"page_number": 20' in prompt
    assert '"line_number": 40' not in prompt
    assert "first_body_line_number must point to the first real pleading/body content" in prompt
    assert "Return the full list of plaintiffs and the full list of defendants" in prompt


def test_answer_structure_prompt_falls_back_to_approximate_line_window_without_pages():
    lines = [f"line {index}" for index in range(1300)]

    _prompt, source_rows = answer_drafter._build_answer_structure_prompt(lines, [])

    assert len(source_rows) == 1200
    assert source_rows[0]["line_number"] == 0
    assert source_rows[-1]["line_number"] == 1199
    assert source_rows[0]["page_number"] is None


def test_answer_structure_extraction_normalizes_source_backed_result(monkeypatch):
    lines = [
        "UNITED STATES DISTRICT COURT",
        "Plaintiff LLC,",
        "v.",
        "Defendant Inc.",
        "Case No. 1:23-cv-00001",
        "TABLE OF CONTENTS",
        "INTRODUCTION",
        "Plaintiff brings this action for damages.",
    ]
    metadata = _metadata_for_pages([1, 1, 1, 1, 1, 2, 3, 3])

    def fake_chat_completion_j(prompt):
        assert '"line_number": 7' in prompt
        assert "Use only the line numbers and page numbers shown in the source rows." in prompt
        return json.dumps({
            "has_caption": True,
            "caption_pages": [1],
            "has_table_of_contents": True,
            "toc_pages": [2],
            "has_table_of_authorities": False,
            "toa_pages": [],
            "first_body_line_number": 7,
            "first_body_page": 3,
            "first_body_page_line_number": 1,
            "first_body_reason": "The introduction text begins after the TOC.",
            "first_body_confidence": "high",
            "court": "UNITED STATES DISTRICT COURT",
            "venue": "UNITED STATES DISTRICT COURT",
            "jurisdiction_type": "federal",
            "jurisdiction_reason": "Federal district court caption.",
            "jurisdiction_confidence": "high",
            "state": "",
            "case_number": "1:23-cv-00001",
            "plaintiffs": ["Plaintiff LLC"],
            "defendants": ["Defendant Inc."],
            "parties": [
                {"name": "Plaintiff LLC", "role": "plaintiff"},
                {"name": "Defendant Inc.", "role": "defendant"},
            ],
            "parties_reason": "Caption party block.",
            "parties_confidence": "high",
            "pleading_title": "Complaint",
            "jury_demand": True,
        })

    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))
    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)

    result = answer_drafter.get_answer_complaint_structure_from_llm(lines, metadata)

    assert result["first_body_line_number"] == 7
    assert result["first_body_page"] == 3
    assert result["plaintiffs"] == ["Plaintiff LLC"]
    assert result["defendants"] == ["Defendant Inc."]
    assert result["jurisdiction_type"] == "federal"
    assert result["jury_demand"] is True


def test_answer_structure_rejects_body_line_not_in_source_rows(monkeypatch):
    lines = [f"line {index}" for index in range(1400)]

    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))
    monkeypatch.setattr(
        answer_drafter,
        "chat_completion_j",
        lambda prompt: json.dumps({
            "first_body_line_number": 1300,
            "plaintiffs": ["Plaintiff"],
            "defendants": ["Defendant"],
            "venue": "Superior Court",
            "jurisdiction_type": "state",
        }),
    )

    result = answer_drafter.get_answer_complaint_structure_from_llm(lines, [])

    assert result["first_body_line_number"] is None
    assert "body starts" in answer_drafter._answer_structure_setup_error(result)


def test_answer_structure_setup_requires_parties_and_body_start():
    assert answer_drafter._answer_structure_setup_error({
        "plaintiffs": [],
        "defendants": ["Defendant"],
        "first_body_line_number": 5,
    }) == "Something went wrong when extracting parties from the complaint, please try again."
    assert answer_drafter._answer_structure_setup_error({
        "plaintiffs": ["Plaintiff"],
        "defendants": ["Defendant"],
        "first_body_line_number": None,
    }) == "Something went wrong when identifying where the complaint body starts, please try again."


def test_answer_analysis_returns_clear_error_when_structure_missing_body_start(monkeypatch):
    memory = {
        "complaint_text": json.dumps(["Caption", "INTRODUCTION"]),
        "line_metadata": json.dumps(_metadata_for_pages([1, 1])),
    }

    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_complaint_structure_from_llm",
        lambda text_by_lines, line_metadata: {
            "plaintiffs": ["Plaintiff"],
            "defendants": ["Defendant"],
            "first_body_line_number": None,
        },
    )

    result = answer_drafter.generate_answer_analysis_from_memory(memory, "draft-public-id")

    assert result == {
        "message": "error",
        "error_message": "Something went wrong when identifying where the complaint body starts, please try again.",
    }
    assert json.loads(memory["complaint_structure"])["first_body_line_number"] is None
