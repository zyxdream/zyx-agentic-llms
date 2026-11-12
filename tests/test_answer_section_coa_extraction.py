import json
import re
import pytest

from backend import answer_drafter
from tests.answer_parse_test_support import install_legacy_answer_json_mock


@pytest.fixture(autouse=True)
def _legacy_answer_json_mock(monkeypatch):
    install_legacy_answer_json_mock(monkeypatch)


def _metadata_for_lines(lines: list[str]) -> list[dict]:
    return [
        {
            "line_number": index,
            "page_number": (index // 40) + 1,
            "page_line_number": index % 40,
            "bold": line.isupper(),
            "ocr": False,
        }
        for index, line in enumerate(lines)
    ]


def _extract_section_prompt_metadata(prompt: str) -> dict:
    match = re.search(r"Metadata:\n(\{[\s\S]*?\})\n\nSource rows:", prompt)
    assert match is not None
    return json.loads(match.group(1))


def _section_item(
    lines: list[str],
    start_line: int,
    title: str,
    section_type: str,
    level: int | None,
    is_cause_of_action: bool = False,
    claim_name: str | None = None,
    claim_number: int | None = None,
) -> dict:
    return {
        "start_line": start_line,
        "end_line": None,
        "title": title,
        "type": section_type,
        "level": level,
        "is_cause_of_action": is_cause_of_action,
        "claim_name": claim_name,
        "claim_number": claim_number,
        "against": [],
        "by": [],
        "source_lines": [start_line],
        "confidence": "high",
        "reason": "",
        "_line_text": lines[start_line],
    }


def test_answer_section_chunks_start_at_body_line_and_clamp_context(monkeypatch):
    lines = [f"line {index}" for index in range(2500)]
    metadata = _metadata_for_lines(lines)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: 1)

    chunks = answer_drafter._build_answer_section_chunks(
        lines,
        metadata,
        first_body_line_number=420,
    )

    assert len(chunks) == 5
    assert chunks[0]["primary_line_start"] == 420
    assert chunks[0]["primary_line_end"] == 919
    assert chunks[0]["context_line_start"] == 370
    assert chunks[0]["context_line_end"] == 994
    assert chunks[-1]["primary_line_start"] == 2420
    assert chunks[-1]["primary_line_end"] == 2499
    assert chunks[-1]["context_line_start"] == 2370
    assert chunks[-1]["context_line_end"] == 2499


def test_answer_section_chunks_shrink_primary_range_when_token_guard_exceeded(monkeypatch):
    lines = [f"line {index}" for index in range(25)]
    metadata = _metadata_for_lines(lines)

    def fake_num_tokens(prompt):
        prompt_metadata = _extract_section_prompt_metadata(prompt)
        primary_count = (
            prompt_metadata["primary_line_end"]
            - prompt_metadata["primary_line_start"]
            + 1
        )
        return primary_count * 100

    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", fake_num_tokens)

    chunks = answer_drafter._build_answer_section_chunks(
        lines,
        metadata,
        first_body_line_number=0,
        primary_lines=10,
        context_before_lines=0,
        context_after_lines=0,
        max_prompt_tokens=600,
    )

    assert chunks[0]["primary_line_start"] == 0
    assert chunks[0]["primary_line_end"] == 4
    assert all(
        chunk["primary_line_end"] - chunk["primary_line_start"] + 1 <= 6
        for chunk in chunks
    )


def test_answer_section_prompt_contains_valid_types_and_primary_rules():
    lines = [
        "INTRODUCTION",
        "1. Plaintiff alleges facts.",
        "FIRST CAUSE OF ACTION",
        "Breach of Contract",
    ]
    metadata = _metadata_for_lines(lines)
    chunk = answer_drafter._build_answer_section_chunk(
        total_lines=len(lines),
        primary_start=0,
        primary_end=3,
        context_before_lines=0,
        context_after_lines=0,
        chunk_id=1,
        chunk_count=1,
    )

    prompt = answer_drafter._build_answer_section_chunk_prompt(chunk, lines, metadata)

    assert '"type": "cause_of_action"' in prompt
    assert '"valid_types"' in prompt
    assert '"cause_of_action"' in prompt
    assert "Return only items whose start_line is inside the primary range" in prompt
    assert "Do not invent line numbers, titles, claims, parties, or defendants" in prompt
    assert "A cause of action is also a section" in prompt
    assert "Return end_line: null" in prompt
    assert '"line_number": 0' in prompt
    assert '"page_number": 1' in prompt
    assert '"text": "INTRODUCTION"' in prompt
    assert '"items": []' in prompt


def test_answer_section_valid_items_with_truncated_reason_do_not_split(monkeypatch):
    lines = [
        "C. Focus on Price Increases Intensifies",
        "Collusion from Late 2012 continued.",
    ]
    metadata = _metadata_for_lines(lines)
    chunk = answer_drafter._build_answer_section_chunk(
        total_lines=len(lines),
        primary_start=0,
        primary_end=1,
        context_before_lines=0,
        context_after_lines=0,
        chunk_id=1,
        chunk_count=1,
    )
    calls = []

    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))

    def fake_chat_completion_j(prompt=None):
        calls.append(_extract_section_prompt_metadata(prompt)["chunk_id"])
        return json.dumps({
            "chunk_id": 1,
            "items": [
                {
                    "start_line": 0,
                    "end_line": None,
                    "title": "C. Focus on Price Increases Intensifies",
                    "type": "factual_allegations",
                    "level": 2,
                    "is_cause_of_action": False,
                    "claim_name": None,
                    "claim_number": None,
                    "against": [],
                    "by": [],
                    "source_lines": [0, 1],
                    "confidence": "medium",
                    "reason": "The title appears truncated or hyphenated in the source.",
                }
            ],
        })

    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)

    result = answer_drafter._extract_answer_section_items_for_chunk(chunk, lines, metadata)

    assert calls == [1]
    assert result["split_retry"] is False
    assert result["retry_reasons"] == []
    assert result["items"][0]["title"] == "C. Focus on Price Increases Intensifies"


def test_answer_section_empty_items_are_valid_and_do_not_split(monkeypatch):
    lines = ["No structural heading.", "Still body text."]
    metadata = _metadata_for_lines(lines)
    chunk = answer_drafter._build_answer_section_chunk(
        total_lines=len(lines),
        primary_start=0,
        primary_end=1,
        context_before_lines=0,
        context_after_lines=0,
        chunk_id=1,
        chunk_count=1,
    )
    calls = []

    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))

    def fake_chat_completion_j(prompt=None):
        calls.append(_extract_section_prompt_metadata(prompt)["chunk_id"])
        return json.dumps({"chunk_id": 1, "items": []})

    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)

    result = answer_drafter._extract_answer_section_items_for_chunk(chunk, lines, metadata)

    assert calls == [1]
    assert result["items"] == []
    assert result["parse_failed"] is False
    assert result["split_retry"] is False


def test_answer_section_malformed_json_triggers_split_retry(monkeypatch):
    lines = ["INTRODUCTION", "1. Plaintiff alleges facts."]
    metadata = _metadata_for_lines(lines)
    chunk = answer_drafter._build_answer_section_chunk(
        total_lines=len(lines),
        primary_start=0,
        primary_end=1,
        context_before_lines=0,
        context_after_lines=0,
        chunk_id=1,
        chunk_count=1,
    )
    calls = []

    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))

    def fake_chat_completion_j(prompt=None):
        chunk_id = _extract_section_prompt_metadata(prompt)["chunk_id"]
        calls.append(chunk_id)
        if chunk_id == 1:
            return "not json"
        return json.dumps({"chunk_id": chunk_id, "items": []})

    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)

    result = answer_drafter._extract_answer_section_items_for_chunk(chunk, lines, metadata)

    assert calls == [1, "1.1", "1.2"]
    assert result["split_retry"] is True
    assert result["retry_reasons"] == ["parse_failed"]


def test_answer_section_top_level_split_note_triggers_split_retry(monkeypatch):
    lines = ["INTRODUCTION", "1. Plaintiff alleges facts."]
    metadata = _metadata_for_lines(lines)
    chunk = answer_drafter._build_answer_section_chunk(
        total_lines=len(lines),
        primary_start=0,
        primary_end=1,
        context_before_lines=0,
        context_after_lines=0,
        chunk_id=1,
        chunk_count=1,
    )

    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))

    def fake_chat_completion_j(prompt=None):
        chunk_id = _extract_section_prompt_metadata(prompt)["chunk_id"]
        if chunk_id == 1:
            return json.dumps({"note": "Please split into smaller chunks."})
        return json.dumps({"chunk_id": chunk_id, "items": []})

    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)

    result = answer_drafter._extract_answer_section_items_for_chunk(chunk, lines, metadata)

    assert result["split_retry"] is True
    assert result["retry_reasons"] == ["parse_failed", "size_limit_request"]


def test_answer_section_extraction_filters_reconciles_and_adds_empty_coa_summary(monkeypatch):
    lines = [
        "Caption",
        "TABLE OF CONTENTS",
        "FIRST CAUSE OF ACTION ........................ 10",
        "INTRODUCTION",
        "1. Plaintiff begins.",
        "FIRST CAUSE OF ACTION",
        "Breach of Contract",
        "PRAYER FOR RELIEF",
    ]
    metadata = _metadata_for_lines(lines)

    monkeypatch.setattr(answer_drafter, "ANSWER_SECTION_PRIMARY_LINES", 4)
    monkeypatch.setattr(answer_drafter, "ANSWER_SECTION_CONTEXT_BEFORE_LINES", 1)
    monkeypatch.setattr(answer_drafter, "ANSWER_SECTION_CONTEXT_AFTER_LINES", 1)
    monkeypatch.setattr(answer_drafter, "ANSWER_SECTION_MAX_PROMPT_TOKENS", 60000)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))

    def fake_chat_completion_j(prompt=None):
        prompt_metadata = _extract_section_prompt_metadata(prompt)
        if prompt_metadata["chunk_id"] == 1:
            return json.dumps({
                "chunk_id": 1,
                "items": [
                    {
                        "start_line": 2,
                        "end_line": None,
                        "title": "FIRST CAUSE OF ACTION",
                        "type": "cause_of_action",
                        "level": 1,
                        "is_cause_of_action": True,
                        "claim_name": None,
                        "claim_number": 1,
                        "against": [],
                        "by": [],
                        "source_lines": [2],
                        "confidence": "high",
                        "reason": "Context-only TOC entry.",
                    },
                    {
                        "start_line": 3,
                        "end_line": None,
                        "title": "INTRODUCTION",
                        "type": "introduction",
                        "level": 1,
                        "is_cause_of_action": False,
                        "claim_name": None,
                        "claim_number": None,
                        "against": [],
                        "by": [],
                        "source_lines": [3],
                        "confidence": "high",
                        "reason": "Main body introduction heading.",
                    },
                    {
                        "start_line": 5,
                        "end_line": None,
                        "title": "FIRST CAUSE OF ACTION",
                        "type": "cause_of_action",
                        "level": 1,
                        "is_cause_of_action": True,
                        "claim_name": None,
                        "claim_number": 1,
                        "against": [],
                        "by": [],
                        "source_lines": [5],
                        "confidence": "high",
                        "reason": "COA ordinal heading.",
                    },
                    {
                        "start_line": 6,
                        "end_line": None,
                        "title": "Breach of Contract",
                        "type": "cause_of_action",
                        "level": 1,
                        "is_cause_of_action": True,
                        "claim_name": "Breach of Contract",
                        "claim_number": 1,
                        "against": ["ABC Corp."],
                        "by": ["Plaintiff"],
                        "source_lines": [6],
                        "confidence": "medium",
                        "reason": "Claim name continuation.",
                    },
                ],
            })
        return json.dumps({
            "chunk_id": 2,
            "items": [
                {
                    "start_line": 7,
                    "end_line": None,
                    "title": "PRAYER FOR RELIEF",
                    "type": "prayer",
                    "level": 1,
                    "is_cause_of_action": False,
                    "claim_name": None,
                    "claim_number": None,
                    "against": [],
                    "by": [],
                    "source_lines": [7],
                    "confidence": "high",
                    "reason": "Relief heading.",
                }
            ],
        })

    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)

    result = answer_drafter.get_answer_sections_and_coas_from_llm(
        lines,
        metadata,
        first_body_line_number=3,
    )

    sections = result["sections"]
    causes_of_action = result["causes_of_action"]
    assert [section["title"] for section in sections] == [
        "INTRODUCTION",
        "FIRST CAUSE OF ACTION - Breach of Contract",
        "PRAYER FOR RELIEF",
    ]
    assert sections[0]["start_line"] == 3
    assert sections[0]["end_line"] == 4
    assert sections[1]["start_line"] == 5
    assert sections[1]["end_line"] == 6
    assert sections[1]["source_lines"] == [5, 6]
    assert causes_of_action == [
        {
            **sections[1],
            "summary": "",
        }
    ]


def test_answer_section_reconciliation_dedupes_and_normalizes_invalid_type():
    lines = [
        "INTRODUCTION",
        "FACTUAL ALLEGATIONS",
        "FACTUAL ALLEGATIONS",
        "COUNT I",
        "Negligence",
    ]
    items = [
        {
            "start_line": 1,
            "end_line": None,
            "title": "FACTUAL ALLEGATIONS",
            "type": "factual_allegations",
            "level": 1,
            "is_cause_of_action": False,
            "claim_name": None,
            "claim_number": None,
            "against": [],
            "by": [],
            "source_lines": [1],
            "confidence": "medium",
            "reason": "",
            "_line_text": lines[1],
        },
        {
            "start_line": 1,
            "end_line": None,
            "title": "FACTUAL ALLEGATIONS",
            "type": "factual_allegations",
            "level": 1,
            "is_cause_of_action": False,
            "claim_name": None,
            "claim_number": None,
            "against": [],
            "by": [],
            "source_lines": [1],
            "confidence": "high",
            "reason": "",
            "_line_text": lines[1],
        },
        {
            "start_line": 3,
            "end_line": None,
            "title": "COUNT I",
            "type": "cause_of_action",
            "level": 1,
            "is_cause_of_action": True,
            "claim_name": None,
            "claim_number": 1,
            "against": [],
            "by": [],
            "source_lines": [3],
            "confidence": "high",
            "reason": "",
            "_line_text": lines[3],
        },
        {
            "start_line": 4,
            "end_line": None,
            "title": "Negligence",
            "type": "other",
            "level": 1,
            "is_cause_of_action": False,
            "claim_name": None,
            "claim_number": None,
            "against": [],
            "by": [],
            "source_lines": [4],
            "confidence": "medium",
            "reason": "",
            "_line_text": lines[4],
        },
    ]

    sections = answer_drafter._reconcile_answer_section_items(items, lines)

    assert len(sections) == 2
    assert sections[0]["title"] == "FACTUAL ALLEGATIONS"
    assert sections[0]["end_line"] == 2
    assert sections[1]["title"] == "COUNT I - Negligence"
    assert sections[1]["type"] == "cause_of_action"
    assert sections[1]["claim_name"] == "Negligence"
    assert sections[1]["claim_number"] == 1


def test_answer_section_reconciliation_computes_hierarchy_aware_end_lines():
    lines = [
        "FACTUAL ALLEGATIONS",
        "1. Background paragraph.",
        "C. Focus on Price Increases Intensifies",
        "i. Shifts in the Market Foster Collusion",
        "2. Detail paragraph.",
        "ii. Executives Coordinate Messaging",
        "3. More detail.",
        "D. Market Allocation",
        "4. Allocation detail.",
        "FIRST CAUSE OF ACTION",
        "Antitrust Conspiracy",
        "A. Elements",
        "5. Claim detail.",
        "SECOND CAUSE OF ACTION",
        "Unjust Enrichment",
        "PRAYER FOR RELIEF",
        "Wherefore...",
    ]
    items = [
        _section_item(lines, 0, "FACTUAL ALLEGATIONS", "factual_allegations", 1),
        _section_item(lines, 2, "C. Focus on Price Increases Intensifies", "factual_allegations", 2),
        _section_item(lines, 3, "i. Shifts in the Market Foster Collusion", "factual_allegations", 3),
        _section_item(lines, 5, "ii. Executives Coordinate Messaging", "factual_allegations", 3),
        _section_item(lines, 7, "D. Market Allocation", "factual_allegations", 2),
        _section_item(
            lines,
            9,
            "FIRST CAUSE OF ACTION - Antitrust Conspiracy",
            "cause_of_action",
            1,
            True,
            "Antitrust Conspiracy",
            1,
        ),
        _section_item(lines, 11, "A. Elements", "other", 2),
        _section_item(
            lines,
            13,
            "SECOND CAUSE OF ACTION - Unjust Enrichment",
            "cause_of_action",
            1,
            True,
            "Unjust Enrichment",
            2,
        ),
        _section_item(lines, 15, "PRAYER FOR RELIEF", "prayer", 1),
    ]

    sections = answer_drafter._reconcile_answer_section_items(items, lines)
    by_title = {section["title"]: section for section in sections}

    assert by_title["FACTUAL ALLEGATIONS"]["end_line"] == 8
    assert by_title["C. Focus on Price Increases Intensifies"]["end_line"] == 6
    assert by_title["i. Shifts in the Market Foster Collusion"]["end_line"] == 4
    assert by_title["ii. Executives Coordinate Messaging"]["end_line"] == 6
    assert by_title["D. Market Allocation"]["end_line"] == 8
    assert by_title["FIRST CAUSE OF ACTION - Antitrust Conspiracy"]["end_line"] == 12
    assert by_title["A. Elements"]["end_line"] == 12
    assert by_title["SECOND CAUSE OF ACTION - Unjust Enrichment"]["end_line"] == 14


def test_answer_section_reconciliation_infers_missing_heading_levels():
    lines = [
        "C. Focus on Price Increases Intensifies",
        "i. Shifts in the Market Foster Collusion",
        "PRAYER FOR RELIEF",
    ]
    items = [
        _section_item(lines, 0, "C. Focus on Price Increases Intensifies", "factual_allegations", None),
        _section_item(lines, 1, "i. Shifts in the Market Foster Collusion", "factual_allegations", None),
        _section_item(lines, 2, "PRAYER FOR RELIEF", "prayer", None),
    ]

    sections = answer_drafter._reconcile_answer_section_items(items, lines)

    assert [section["level"] for section in sections] == [2, 3, 1]
    assert sections[0]["end_line"] == 1
    assert sections[1]["end_line"] == 1


def test_build_answer_outline_from_locations_orders_sections_and_paragraphs():
    sections = [
        {
            "start_line": 10,
            "end_line": 19,
            "title": "FACTUAL ALLEGATIONS",
            "type": "factual_allegations",
            "level": 1,
        },
        {
            "start_line": 20,
            "end_line": 40,
            "title": "FIRST CAUSE OF ACTION",
            "type": "cause_of_action",
            "level": 1,
        },
    ]
    para_2_ln = {"1": 12, "2": 20, "3": 32}

    outline = answer_drafter.build_answer_outline_from_locations(
        sections,
        para_2_ln,
        first_body_line_number=10,
    )

    assert outline == [
        ["FACTUAL ALLEGATIONS", None],
        ["Paragraph 1", 1],
        ["FIRST CAUSE OF ACTION", None],
        ["Paragraph 2", 2],
        ["Paragraph 3", 3],
    ]


def test_build_answer_outline_from_locations_keeps_repeated_titles_in_line_order():
    sections = [
        {
            "start_line": 5,
            "end_line": 9,
            "title": "CLAIMS FOR RELIEF",
            "type": "cause_of_action",
            "level": 1,
        },
        {
            "start_line": 20,
            "end_line": 25,
            "title": "CLAIMS FOR RELIEF",
            "type": "cause_of_action",
            "level": 1,
        },
    ]
    para_2_ln = {1: 6, 2: 21}

    outline = answer_drafter.build_answer_outline_from_locations(
        sections,
        para_2_ln,
        first_body_line_number=5,
    )

    assert outline == [
        ["CLAIMS FOR RELIEF", None],
        ["Paragraph 1", 1],
        ["CLAIMS FOR RELIEF", None],
        ["Paragraph 2", 2],
    ]


def test_build_answer_outline_from_locations_skips_sections_without_paragraphs():
    sections = [
        {
            "start_line": 2,
            "end_line": 3,
            "title": "INTRODUCTION",
            "type": "introduction",
            "level": 1,
        },
        {
            "start_line": 10,
            "end_line": 11,
            "title": "SIGNATURE",
            "type": "signature",
            "level": 1,
        },
    ]
    para_2_ln = {"1": "3"}

    outline = answer_drafter.build_answer_outline_from_locations(
        sections,
        para_2_ln,
        first_body_line_number=2,
    )

    assert outline == [["INTRODUCTION", None], ["Paragraph 1", 1]]
