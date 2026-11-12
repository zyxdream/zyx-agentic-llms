import json
import re

import pytest

from backend import answer_drafter
from tests.answer_parse_test_support import install_legacy_answer_json_mock


@pytest.fixture(autouse=True)
def _legacy_answer_json_mock(monkeypatch):
    install_legacy_answer_json_mock(monkeypatch)


def _extract_prompt_metadata(prompt: str) -> dict:
    match = re.search(r"Metadata:\n(\{[\s\S]*?\})\n\nSource content:", prompt)
    assert match is not None
    return json.loads(match.group(1))


def test_answer_paragraph_chunks_use_global_primary_and_context_ranges():
    lines = [f"line {i}" for i in range(31461)]

    chunks = answer_drafter._build_answer_para_chunks(
        lines,
        primary_lines=500,
        context_lines=40,
    )

    assert len(chunks) == 63
    assert chunks[0]["primary_line_start"] == 0
    assert chunks[0]["primary_line_end"] == 499
    assert chunks[0]["context_line_start"] == 0
    assert chunks[0]["context_line_end"] == 539
    assert chunks[1]["primary_line_start"] == 500
    assert chunks[1]["primary_line_end"] == 999
    assert chunks[1]["context_line_start"] == 460
    assert chunks[1]["context_line_end"] == 1039
    assert chunks[-1]["primary_line_start"] == 31000
    assert chunks[-1]["primary_line_end"] == 31460
    assert chunks[-1]["context_line_start"] == 30960
    assert chunks[-1]["context_line_end"] == 31460


def test_answer_paragraph_chunks_can_start_at_body_line():
    lines = [f"line {i}" for i in range(1200)]

    chunks = answer_drafter._build_answer_para_chunks(
        lines,
        primary_lines=500,
        context_lines=40,
        primary_start_line=420,
    )

    assert len(chunks) == 2
    assert chunks[0]["primary_line_start"] == 420
    assert chunks[0]["primary_line_end"] == 919
    assert chunks[0]["context_line_start"] == 380
    assert chunks[0]["context_line_end"] == 959
    assert chunks[1]["primary_line_start"] == 920
    assert chunks[1]["primary_line_end"] == 1199
    assert chunks[1]["context_line_start"] == 880
    assert chunks[1]["context_line_end"] == 1199


def test_chunked_paragraph_extraction_filters_context_only_rows(monkeypatch):
    lines = [
        "1. First allegation",
        "First continuation",
        "More first",
        "Still first",
        "2. Second allegation",
        "3. Third allegation",
        "Third continuation",
        "More third",
        "Still third",
        "Almost fourth",
        "4. Fourth allegation",
        "Fourth continuation",
    ]

    monkeypatch.setattr(answer_drafter, "ANSWER_PARA_PRIMARY_LINES", 5)
    monkeypatch.setattr(answer_drafter, "ANSWER_PARA_CONTEXT_LINES", 1)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))

    def fake_chat_completion_j(prompt):
        metadata = _extract_prompt_metadata(prompt)
        paragraphs = []
        for line_number in range(metadata["context_line_start"], metadata["context_line_end"] + 1):
            match = re.match(r"^(\d+)\.", lines[line_number])
            if match:
                paragraphs.append({
                    "line_number": line_number,
                    "paragraph_number": int(match.group(1)),
                })
        return json.dumps({"chunk_id": metadata["chunk_id"], "paragraphs": paragraphs})

    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)

    assert answer_drafter.get_paralines_from_llm(lines) == {
        1: 0,
        2: 4,
        3: 5,
        4: 10,
    }


def test_chunked_paragraph_extraction_starts_at_body_line_with_global_numbers(monkeypatch):
    lines = [
        "UNITED STATES DISTRICT COURT",
        "TABLE OF CONTENTS",
        "1. Table entry",
        "INTRODUCTION",
        "1. First allegation",
        "2. Second allegation",
        "3. Third allegation",
    ]
    calls = []

    monkeypatch.setattr(answer_drafter, "ANSWER_PARA_PRIMARY_LINES", 2)
    monkeypatch.setattr(answer_drafter, "ANSWER_PARA_CONTEXT_LINES", 1)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))

    def fake_chat_completion_j(prompt):
        metadata = _extract_prompt_metadata(prompt)
        calls.append(metadata)
        paragraphs = []
        for line_number in range(metadata["context_line_start"], metadata["context_line_end"] + 1):
            match = re.match(r"^(\d+)\.", lines[line_number])
            if match:
                paragraphs.append({
                    "line_number": line_number,
                    "paragraph_number": int(match.group(1)),
                })
        return json.dumps({"chunk_id": metadata["chunk_id"], "paragraphs": paragraphs})

    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)

    assert answer_drafter.get_paralines_from_llm(lines, primary_start_line=4) == {
        1: 4,
        2: 5,
        3: 6,
    }
    first_chunk = next(call for call in calls if call["chunk_id"] == 1)
    assert first_chunk["primary_line_start"] == 4
    assert first_chunk["context_line_start"] == 3


def test_empty_paragraph_chunk_is_valid_and_does_not_break(monkeypatch):
    lines = [
        "UNITED STATES DISTRICT COURT",
        "CASE NO. 1:23-CV-00001",
        "TABLE OF CONTENTS",
        "INTRODUCTION",
        "caption text",
        "1. First allegation",
        "First continuation",
        "2. Second allegation",
        "Second continuation",
        "3. Third allegation",
        "Third continuation",
        "4. Fourth allegation",
    ]
    calls = []

    monkeypatch.setattr(answer_drafter, "ANSWER_PARA_PRIMARY_LINES", 5)
    monkeypatch.setattr(answer_drafter, "ANSWER_PARA_CONTEXT_LINES", 1)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))

    def fake_chat_completion_j(prompt):
        metadata = _extract_prompt_metadata(prompt)
        calls.append(metadata["chunk_id"])
        assert "If the primary range contains no true paragraph starts" in prompt
        assert "Do not return placeholder objects with blank line_number or paragraph_number" in prompt
        paragraphs = []
        for line_number in range(metadata["primary_line_start"], metadata["primary_line_end"] + 1):
            match = re.match(r"^(\d+)\.", lines[line_number])
            if match:
                paragraphs.append({
                    "line_number": line_number,
                    "paragraph_number": int(match.group(1)),
                })
        return json.dumps({"chunk_id": metadata["chunk_id"], "paragraphs": paragraphs})

    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)

    assert answer_drafter.get_paralines_from_llm(lines) == {
        1: 5,
        2: 7,
        3: 9,
        4: 11,
    }
    assert sorted(calls) == [1, 2, 3]


def test_chunked_paragraph_extraction_retries_too_large_chunks_by_lines(monkeypatch):
    lines = [
        "1. First allegation",
        "2. Second allegation",
        "3. Third allegation",
        "4. Fourth allegation",
    ]

    monkeypatch.setattr(answer_drafter, "ANSWER_PARA_PRIMARY_LINES", 4)
    monkeypatch.setattr(answer_drafter, "ANSWER_PARA_CONTEXT_LINES", 0)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))

    def fake_chat_completion_j(prompt):
        metadata = _extract_prompt_metadata(prompt)
        primary_size = metadata["primary_line_end"] - metadata["primary_line_start"] + 1
        if primary_size > 2:
            return json.dumps({
                "note": "This chunk is too large; split it into smaller chunks.",
            })
        paragraphs = []
        for line_number in range(metadata["primary_line_start"], metadata["primary_line_end"] + 1):
            match = re.match(r"^(\d+)\.", lines[line_number])
            if match:
                paragraphs.append({
                    "line_number": line_number,
                    "paragraph_number": int(match.group(1)),
                })
        return json.dumps({"chunk_id": metadata["chunk_id"], "paragraphs": paragraphs})

    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)

    assert answer_drafter.get_paralines_from_llm(lines) == {
        1: 0,
        2: 1,
        3: 2,
        4: 3,
    }


def test_paragraph_retry_decision_ignores_size_words_when_parse_succeeds():
    should_retry, retry_reasons = answer_drafter._answer_chunk_retry_decision(
        False,
        json.dumps({
            "paragraphs": [],
            "note": "The source heading appears truncated, but there are no paragraph starts.",
        }),
    )

    assert should_retry is False
    assert retry_reasons == []


def test_blank_placeholder_paragraph_rows_are_rejected():
    chunk = {
        "chunk_id": 1,
        "primary_line_start": 0,
        "primary_line_end": 2,
    }
    rows = answer_drafter._filter_answer_para_rows(
        chunk,
        [
            {"line_number": "", "paragraph_number": ""},
            {"line_number": None, "paragraph_number": 1},
            {"line_number": 0, "paragraph_number": None},
            {"line_number": "1", "paragraph_number": "2"},
        ],
        ["1. First allegation", "2. Second allegation", "3. Third allegation"],
    )

    assert rows == [
        {
            "paragraph_number": 2,
            "line_number": 1,
            "chunk_id": 1,
            "line_text": "2. Second allegation",
        }
    ]


def test_all_empty_paragraph_chunks_raise_clear_error(monkeypatch):
    lines = [
        "UNITED STATES DISTRICT COURT",
        "CASE NO. 1:23-CV-00001",
        "TABLE OF CONTENTS",
        "PRAYER FOR RELIEF",
    ]

    monkeypatch.setattr(answer_drafter, "ANSWER_PARA_PRIMARY_LINES", 2)
    monkeypatch.setattr(answer_drafter, "ANSWER_PARA_CONTEXT_LINES", 0)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))
    monkeypatch.setattr(
        answer_drafter,
        "chat_completion_j",
        lambda prompt: json.dumps({"paragraphs": []}),
    )

    with pytest.raises(ValueError, match="did not identify any paragraphs"):
        answer_drafter.get_paralines_from_llm(lines)


def test_consolidation_prefers_duplicate_with_nearby_numbered_neighbors():
    rows = [
        {
            "paragraph_number": 1,
            "line_number": 0,
            "chunk_id": 1,
            "line_text": "1. Table of contents entry",
        },
        {
            "paragraph_number": 1,
            "line_number": 5,
            "chunk_id": 1,
            "line_text": "1. Real first allegation",
        },
        {
            "paragraph_number": 2,
            "line_number": 6,
            "chunk_id": 1,
            "line_text": "2. Real second allegation",
        },
    ]

    assert answer_drafter._consolidate_answer_para_rows(rows, [""] * 7) == {
        1: 5,
        2: 6,
    }


def test_consolidation_prefers_duplicate_inside_neighbor_window(capsys):
    rows = [
        {
            "paragraph_number": 10,
            "line_number": 80,
            "chunk_id": 1,
            "line_text": "10. Out-of-order earlier duplicate.",
        },
        {
            "paragraph_number": 9,
            "line_number": 90,
            "chunk_id": 1,
            "line_text": "9. Previous allegation.",
        },
        {
            "paragraph_number": 10,
            "line_number": 100,
            "chunk_id": 1,
            "line_text": "10. Correct allegation.",
        },
        {
            "paragraph_number": 11,
            "line_number": 110,
            "chunk_id": 1,
            "line_text": "11. Next allegation.",
        },
    ]

    assert answer_drafter._consolidate_answer_para_rows(rows, [""] * 111) == {
        9: 90,
        10: 100,
        11: 110,
    }
    captured = capsys.readouterr().out
    assert "Duplicate paragraph number" in captured
    assert "kept line 100" in captured


def test_consolidation_prefers_earliest_plausible_duplicate_after_previous_anchor():
    rows = [
        {
            "paragraph_number": 9,
            "line_number": 90,
            "chunk_id": 1,
            "line_text": "9. Previous allegation.",
        },
        {
            "paragraph_number": 10,
            "line_number": 100,
            "chunk_id": 1,
            "line_text": "10. First plausible duplicate.",
        },
        {
            "paragraph_number": 10,
            "line_number": 105,
            "chunk_id": 1,
            "line_text": "10. Later plausible duplicate.",
        },
        {
            "paragraph_number": 11,
            "line_number": 110,
            "chunk_id": 1,
            "line_text": "11. Next allegation.",
        },
    ]

    assert answer_drafter._consolidate_answer_para_rows(rows, [""] * 111) == {
        9: 90,
        10: 100,
        11: 110,
    }


def test_paragraph_warnings_clean_map_has_no_warnings():
    warnings = answer_drafter._validate_answer_para_line_map({
        1: 10,
        2: 15,
        3: 20,
    })

    assert warnings == {
        "has_warnings": False,
        "summary": "",
        "issues": [],
        "stats": {
            "duplicate_count": 0,
            "decreasing_count": 0,
            "suspicious_gap_count": 0,
            "missing_count": 0,
        },
    }


def test_paragraph_warnings_flag_suspicious_gap_without_changing_map():
    para_2_ln = {
        3065: 28705,
        30066: 28714,
        3067: 28726,
    }
    original = dict(para_2_ln)

    warnings = answer_drafter._validate_answer_para_line_map(para_2_ln)

    assert para_2_ln == original
    assert warnings["has_warnings"] is True
    assert warnings["summary"] == "Some paragraph numbers may need review before drafting responses."
    assert warnings["stats"]["suspicious_gap_count"] == 1
    assert warnings["stats"]["decreasing_count"] == 1
    assert warnings["issues"][0] == {
        "type": "suspicious_gap",
        "message": "Check numbering around Paragraph 3065, Paragraph 30066, and Paragraph 3067.",
        "paragraph_numbers": [3065, 30066, 3067],
        "line_numbers": [28705, 28714, 28726],
    }


def test_paragraph_warnings_flag_small_bounded_missing_range():
    warnings = answer_drafter._validate_answer_para_line_map({
        40: 100,
        43: 110,
    })

    assert warnings["has_warnings"] is True
    assert warnings["stats"]["missing_count"] == 2
    assert warnings["issues"][0] == {
        "type": "missing_range",
        "message": "Paragraphs 41-42 may need review between Paragraph 40 and Paragraph 43.",
        "paragraph_numbers": [40, 43],
        "line_numbers": [100, 110],
    }


def test_paragraph_warnings_keep_duplicate_stats_without_user_facing_issue():
    rows = [
        {
            "paragraph_number": 1,
            "line_number": 0,
            "chunk_id": 1,
            "line_text": "1. Table of contents entry",
        },
        {
            "paragraph_number": 1,
            "line_number": 5,
            "chunk_id": 1,
            "line_text": "1. Real first allegation",
        },
        {
            "paragraph_number": 2,
            "line_number": 6,
            "chunk_id": 1,
            "line_text": "2. Real second allegation",
        },
    ]
    duplicate_issues = []

    para_2_ln = answer_drafter._consolidate_answer_para_rows(
        rows,
        [""] * 7,
        duplicate_issues=duplicate_issues,
    )
    warnings = answer_drafter._validate_answer_para_line_map(
        para_2_ln,
        duplicate_issues=duplicate_issues,
    )

    assert para_2_ln == {
        1: 5,
        2: 6,
    }
    assert warnings["has_warnings"] is False
    assert warnings["summary"] == ""
    assert warnings["issues"] == []
    assert warnings["stats"]["duplicate_count"] == 1
