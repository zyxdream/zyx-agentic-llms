import json
import re
import pytest

from backend import answer_drafter
from tests.answer_parse_test_support import install_legacy_answer_json_mock


@pytest.fixture(autouse=True)
def _legacy_answer_json_mock(monkeypatch):
    install_legacy_answer_json_mock(monkeypatch)


def _extract_fact_prompt_metadata(prompt: str) -> dict:
    match = re.search(r"Metadata:\n(\{[\s\S]*?\})\n\nSource rows:", prompt)
    assert match is not None
    return json.loads(match.group(1))


def test_factual_digest_source_ranges_prefer_factual_sections_before_coas():
    lines = [
        "Caption",
        "INTRODUCTION",
        "FACTUAL ALLEGATIONS",
        "1. Plaintiff alleges a contract.",
        "2. Defendant allegedly breached.",
        "FIRST CAUSE OF ACTION",
        "Breach of Contract",
        "PRAYER FOR RELIEF",
    ]
    sections = [
        {"start_line": 1, "end_line": 1, "type": "introduction", "title": "INTRODUCTION"},
        {"start_line": 2, "end_line": 4, "type": "factual_allegations", "title": "FACTUAL ALLEGATIONS"},
        {"start_line": 5, "end_line": 6, "type": "cause_of_action", "title": "FIRST CAUSE OF ACTION"},
        {"start_line": 7, "end_line": 7, "type": "prayer", "title": "PRAYER FOR RELIEF"},
    ]

    ranges = answer_drafter._answer_fact_digest_source_ranges(
        sections,
        lines,
        first_body_line_number=1,
    )

    assert ranges == [
        {
            "start_line": 2,
            "end_line": 4,
            "sources": ["FACTUAL ALLEGATIONS"],
        }
    ]


def test_factual_digest_source_ranges_fallback_to_main_body_before_first_coa():
    lines = [
        "Caption",
        "INTRODUCTION",
        "1. Plaintiff alleges a contract.",
        "2. Defendant allegedly breached.",
        "FIRST CAUSE OF ACTION",
        "Breach of Contract",
    ]
    sections = [
        {"start_line": 1, "end_line": 3, "type": "introduction", "title": "INTRODUCTION"},
        {"start_line": 4, "end_line": 5, "type": "cause_of_action", "title": "FIRST CAUSE OF ACTION"},
    ]

    ranges = answer_drafter._answer_fact_digest_source_ranges(
        sections,
        lines,
        first_body_line_number=1,
    )

    assert ranges == [
        {
            "start_line": 1,
            "end_line": 3,
            "sources": ["fallback_main_body_before_first_coa"],
        }
    ]


def test_factual_digest_chunks_use_line_limit_and_token_guard(monkeypatch):
    lines = [f"{index}. factual line" for index in range(25)]
    source_ranges = [{"start_line": 3, "end_line": 22, "sources": ["FACTS"]}]

    def fake_num_tokens(prompt):
        metadata = _extract_fact_prompt_metadata(prompt)
        primary_count = metadata["primary_line_end"] - metadata["primary_line_start"] + 1
        return primary_count * 100

    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", fake_num_tokens)

    chunks = answer_drafter._build_answer_fact_digest_chunks(
        lines,
        source_ranges,
        paragraph_by_line={},
        primary_lines=10,
        context_before_lines=1,
        context_after_lines=2,
        max_prompt_tokens=600,
    )

    assert chunks[0]["primary_line_start"] == 3
    assert chunks[0]["primary_line_end"] == 7
    assert chunks[0]["context_line_start"] == 2
    assert chunks[0]["context_line_end"] == 9
    assert all(
        chunk["primary_line_end"] - chunk["primary_line_start"] + 1 <= 6
        for chunk in chunks
    )
    assert chunks[-1]["primary_line_end"] == 22


def test_factual_digest_prompt_uses_minimal_metadata_and_ad_aware_rules():
    lines = [
        "FACTUAL ALLEGATIONS",
        "1. Plaintiff alleges notice in March 2020.",
        "Plaintiff filed suit in August 2025.",
    ]
    paragraph_by_line = {1: 1, 2: 1}
    chunk = answer_drafter._build_answer_fact_digest_chunk(
        total_lines=len(lines),
        primary_start=1,
        primary_end=2,
        context_before_lines=1,
        context_after_lines=0,
        chunk_id=1,
        chunk_count=1,
    )

    prompt = answer_drafter._build_answer_fact_digest_chunk_prompt(
        chunk,
        lines,
        paragraph_by_line,
    )

    assert '"line_number": 1' in prompt
    assert '"paragraph_number": 1' in prompt
    assert '"text": "1. Plaintiff alleges notice in March 2020."' in prompt
    assert '"page_number"' not in prompt
    assert '"bold"' not in prompt
    assert "later identify possible" in prompt
    assert "affirmative defenses" in prompt
    assert "Summarize and extract key allegations only from primary factual lines" in prompt
    assert "Do not invent facts, paragraph numbers, dates, parties, locations, claims, or defenses" in prompt


def test_factual_digest_prompt_allows_null_paragraph_numbers_for_state_path():
    lines = ["FACTUAL ALLEGATIONS", "Plaintiff alleges unnumbered facts."]
    chunk = answer_drafter._build_answer_fact_digest_chunk(
        total_lines=len(lines),
        primary_start=0,
        primary_end=1,
        context_before_lines=0,
        context_after_lines=0,
        chunk_id=1,
        chunk_count=1,
    )

    prompt = answer_drafter._build_answer_fact_digest_chunk_prompt(chunk, lines, {})

    assert '"paragraph_number": null' in prompt
    assert '"paragraph_numbers": []' in prompt


def test_factual_digest_consolidates_chunk_outputs_with_source_anchors(monkeypatch):
    lines = [
        "Caption",
        "FACTUAL ALLEGATIONS",
        "1. Plaintiff alleges a written contract dated January 1, 2020.",
        "The contract required monthly payments.",
        "2. Plaintiff alleges Defendant stopped paying in June 2021.",
        "FIRST CAUSE OF ACTION",
    ]
    sections = [
        {"start_line": 1, "end_line": 4, "type": "factual_allegations", "title": "FACTUAL ALLEGATIONS"},
        {"start_line": 5, "end_line": 5, "type": "cause_of_action", "title": "FIRST CAUSE OF ACTION"},
    ]
    calls = []

    monkeypatch.setattr(answer_drafter, "ANSWER_FACT_DIGEST_PRIMARY_LINES", 2)
    monkeypatch.setattr(answer_drafter, "ANSWER_FACT_DIGEST_CONTEXT_BEFORE_LINES", 0)
    monkeypatch.setattr(answer_drafter, "ANSWER_FACT_DIGEST_CONTEXT_AFTER_LINES", 0)
    monkeypatch.setattr(answer_drafter, "ANSWER_FACT_DIGEST_MAX_PROMPT_TOKENS", 60000)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: len(prompt))

    def fake_chat_completion_j(prompt=None):
        calls.append(prompt)
        if "Input JSON:" in prompt:
            return json.dumps({
                "overall_summary": "Plaintiff alleges a contract, payment obligations, and nonpayment.",
                "key_allegations": [
                    {
                        "text": "Plaintiff alleges a written contract dated January 1, 2020.",
                        "paragraph_numbers": [1],
                        "line_numbers": [2],
                        "tags": ["contract", "date"],
                    },
                    {
                        "text": "Invented line should be rejected.",
                        "paragraph_numbers": [99],
                        "line_numbers": [99],
                        "tags": ["bad"],
                    },
                ],
                "defense_relevant_facts": [
                    {
                        "text": "Plaintiff alleges Defendant stopped paying in June 2021.",
                        "possible_relevance": ["payment", "breach", "limitations"],
                        "paragraph_numbers": [2],
                        "line_numbers": [4],
                    }
                ],
            })

        metadata = _extract_fact_prompt_metadata(prompt)
        if metadata["chunk_id"] == 1:
            return json.dumps({
                "chunk_id": 1,
                "summary": "Plaintiff alleges a written contract and payment terms.",
                "key_allegations": [
                    {
                        "text": "Plaintiff alleges a written contract dated January 1, 2020.",
                        "paragraph_numbers": [1],
                        "line_numbers": [2],
                        "tags": ["contract", "date"],
                    }
                ],
                "defense_relevant_facts": [],
            })
        return json.dumps({
            "chunk_id": 2,
            "summary": "Plaintiff alleges Defendant stopped paying.",
            "key_allegations": [],
            "defense_relevant_facts": [
                {
                    "text": "Plaintiff alleges Defendant stopped paying in June 2021.",
                    "possible_relevance": ["payment", "breach", "limitations"],
                    "paragraph_numbers": [2],
                    "line_numbers": [4],
                }
            ],
        })

    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)

    digest = answer_drafter.get_answer_factual_digest_from_llm(
        lines,
        sections,
        para_2_ln={1: 2, 2: 4},
        first_body_line_number=1,
    )

    assert len(calls) == 3
    assert digest["summary"] == "Plaintiff alleges a contract, payment obligations, and nonpayment."
    assert digest["source_ranges"] == [
        {"start_line": 1, "end_line": 4, "sources": ["FACTUAL ALLEGATIONS"]}
    ]
    assert digest["key_allegations"] == [
        {
            "text": "Plaintiff alleges a written contract dated January 1, 2020.",
            "paragraph_numbers": [1],
            "line_numbers": [2],
            "tags": ["contract", "date"],
        }
    ]
    assert digest["defense_relevant_facts"] == [
        {
            "text": "Plaintiff alleges Defendant stopped paying in June 2021.",
            "paragraph_numbers": [2],
            "line_numbers": [4],
            "possible_relevance": ["payment", "breach", "limitations"],
        }
    ]
