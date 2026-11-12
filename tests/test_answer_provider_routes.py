"""Provider-level contracts for Answer Drafter's active Responses calls."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend import answer_drafter
from backend.ai import base as ai_base
from backend.ai.answer_schemas import (
    AnswerComplaintStructure,
    AnswerSectionChunk,
    AnswerParagraphChunk,
    AnswerFactualDigestChunk,
    AnswerFactualDigestReduction,
    AnswerDefenseSuggestions,
    AnswerParagraphResponses,
    AnswerDraftedDefenses,
)


@pytest.fixture(autouse=True)
def enabled_account(monkeypatch):
    # Provider contracts use dictionary memory without persisted account owners.
    from backend.util import account_access
    monkeypatch.setattr(account_access, 'chat_account_id', lambda chat_id: 1)
    monkeypatch.setattr(account_access, 'account_execution', lambda user_id: nullcontext())


class Memory(dict):
    user = SimpleNamespace(contact_info="Test contact")


@pytest.fixture
def provider(monkeypatch):
    parse = Mock()
    create = Mock()
    monkeypatch.setattr(
        ai_base,
        "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(parse=parse, create=create)),
    )
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_base, "sleep", lambda seconds: None)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: 1)
    return SimpleNamespace(parse=parse, create=create)


def _response(*, model, parsed=None, text=""):
    return SimpleNamespace(
        id="answer_route_test",
        model=model,
        status="completed",
        output_parsed=parsed,
        output_text=text,
        output=[],
        usage=SimpleNamespace(total_tokens=10),
    )


STRUCTURE = {
    "has_caption": True,
    "caption_pages": [1],
    "has_table_of_contents": False,
    "toc_pages": [],
    "has_table_of_authorities": False,
    "toa_pages": [],
    "first_body_line_number": 1,
    "first_body_page": None,
    "first_body_page_line_number": None,
    "first_body_reason": None,
    "first_body_confidence": "high",
    "court": None,
    "venue": "Test Court",
    "jurisdiction_type": None,
    "jurisdiction_reason": "Unclear.",
    "jurisdiction_confidence": "low",
    "state": "",
    "case_number": None,
    "plaintiffs": ["Acme Corp."],
    "defendants": ["Beta LLC"],
    "parties": [{"name": "Acme Corp.", "role": "plaintiff"}],
    "parties_reason": "Caption.",
    "parties_confidence": "high",
    "pleading_title": "Complaint",
    "jury_demand": None,
}


@pytest.mark.parametrize(
    ("step", "schema", "payload", "model", "effort"),
    [
        ("structure", AnswerComplaintStructure, STRUCTURE, "gpt-6-luna", "medium"),
        ("sections", AnswerSectionChunk, {"chunk_id": "1", "items": []}, "gpt-6-luna", "medium"),
        ("paragraphs", AnswerParagraphChunk, {"chunk_id": "1", "paragraphs": [{"line_number": 1, "paragraph_number": 7}]}, "gpt-6-luna", "medium"),
        ("digest", AnswerFactualDigestChunk, {"chunk_id": "1", "summary": "Fact.", "key_allegations": [], "defense_relevant_facts": []}, "gpt-6-luna", "medium"),
        ("reducer", AnswerFactualDigestReduction, {"overall_summary": "Combined fact.", "key_allegations": [], "defense_relevant_facts": []}, "gpt-6-luna", "medium"),
        ("suggestions", AnswerDefenseSuggestions, {"coa_id": "1", "coa_title": "Count I", "affirmative_defenses": []}, "gpt-6.1-sol", "high"),
        ("responses", AnswerParagraphResponses, {"Answers": [{"paragraph": "2", "response": "Deny."}, {"paragraph": "7", "response": "Admit."}]}, "gpt-6.1-sol", "high"),
    ],
)
def test_answer_typed_call_sites_send_expected_provider_route(
    provider, step, schema, payload, model, effort
):
    provider.parse.side_effect = lambda **kwargs: _response(
        model=kwargs["model"], parsed=kwargs["text_format"].model_validate(payload)
    )
    lines = ["Caption", "7. Plaintiff alleges a fact."]
    if step == "structure":
        result = answer_drafter.get_answer_complaint_structure_from_llm(lines, [])
        assert result["jury_demand"] is None
        assert result["jurisdiction_type"] == "unknown"
        assert result["court"] == "" and result["case_number"] == ""
    elif step == "sections":
        chunk = answer_drafter._build_answer_section_chunk(2, 0, 1, 0, 0, 1, 1)
        result = answer_drafter._extract_answer_section_items_for_chunk(chunk, lines, [])
        assert result["items"] == [] and not result["parse_failed"]
    elif step == "paragraphs":
        chunk = answer_drafter._build_answer_para_chunk(2, 0, 1, 0, 1, 1)
        result = answer_drafter._extract_answer_para_rows_for_chunk(chunk, lines)
        assert any(row["paragraph_number"] == 7 for row in result["rows"])
    elif step == "digest":
        chunk = answer_drafter._build_answer_fact_digest_chunk(2, 0, 1, 0, 0, 1, 1)
        result = answer_drafter._extract_answer_fact_digest_for_chunk(chunk, lines, {1: 7})
        assert result["summary"] == "Fact."
    elif step == "reducer":
        result = answer_drafter._reduce_answer_fact_digest_with_llm(
            [{"summary": "Fact."}], [], [], lines, {1: 7}
        )
        assert result["summary"] == "Combined fact."
    elif step == "suggestions":
        coa = {"title": "Count I", "start_line": 1, "end_line": 1}
        result = answer_drafter._extract_answer_ad_suggestions_for_coa(
            lines, STRUCTURE, {}, coa, "1"
        )
        assert result["items"] == [] and not result["parse_failed"]
    else:
        chunk = {"chunk_id": 1, "chunk_count": 1, "paragraph_numbers": [2, 7], "prompt": "Draft two answers", "prompt_tokens": 1}
        result = answer_drafter._extract_answer_response_chunk(
            chunk, {}, {}, lines, "unused"
        )
        assert result["rows"] == {2: "Deny.", 7: "Admit."}

    request = provider.parse.call_args.kwargs
    assert provider.parse.call_count == 1
    assert request["model"] == model
    assert request["reasoning"] == {"effort": effort}
    assert request["text_format"] is schema
    assert request["input"][-1]["role"] == "user"
    assert request["input"][-1]["content"]
    if step == "suggestions":
        prompt = request["input"][-1]["content"]
        assert "Be overinclusive and suggest all plausible affirmative defenses" in prompt
        assert "Failure to State a Claim" in prompt
        assert '"affirmative_defenses"' in prompt
        assert "fewer, stronger, fact-tied defenses" not in prompt
    assert "tools" not in request and "response_format" not in request
    provider.create.assert_not_called()


def _patch_memory(monkeypatch, memory):
    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter.Drafts, "pdf_filename", lambda draft_id: "answer.pdf")
    monkeypatch.setattr(answer_drafter, "DBMemory", lambda db, doc_id: memory)
    pdf = Mock(return_value="ok")
    monkeypatch.setattr(answer_drafter, "create_answer_pdf", pdf)
    return pdf


def test_selected_defense_drafting_uses_sol_high_typed_output(provider, monkeypatch):
    memory = Memory({
        "ads_suggestions": json.dumps({"Waiver": {"1": {"description": "Plaintiff waived rights."}}}),
        "inputs/state": "state",
    })
    pdf = _patch_memory(monkeypatch, memory)
    provider.parse.side_effect = lambda **kwargs: _response(
        model=kwargs["model"],
        parsed=AnswerDraftedDefenses.model_validate({
            "defenses": [{"title": "Waiver", "paragraphs": "Plaintiff waived the right."}]
        }),
    )

    result = answer_drafter.state2.run({"draftId": "draft-id", "ad1": "Waiver"})

    assert result["message"] == "Form processed successfully"
    assert "Plaintiff waived the right." in memory["drafted_sections/affirmative_defenses"]
    assert pdf.call_count == 1
    request = provider.parse.call_args.kwargs
    assert request["model"] == "gpt-6.1-sol"
    assert request["reasoning"] == {"effort": "high"}
    assert request["text_format"] is AnswerDraftedDefenses
    provider.create.assert_not_called()


@pytest.mark.parametrize(
    ("suggestions", "selections", "expected_titles", "expected_reason"),
    [
        ({}, {"ad0": "  Equitable Estoppel  "}, ["Equitable Estoppel"], None),
        (
            {"Waiver": {"1": {"description": "Plaintiff waived its rights."}}},
            {"ad0": "Waiver", "ad1": "Equitable Estoppel"},
            ["Waiver", "Equitable Estoppel"],
            "Plaintiff waived its rights.",
        ),
    ],
)
def test_lawyer_added_defenses_reach_the_drafting_prompt(
    provider, monkeypatch, suggestions, selections, expected_titles, expected_reason
):
    memory = Memory({"ads_suggestions": json.dumps(suggestions), "inputs/state": "state"})
    pdf = _patch_memory(monkeypatch, memory)
    provider.parse.return_value = _response(
        model="gpt-6.1-sol",
        parsed=AnswerDraftedDefenses.model_validate({
            "defenses": [
                {"title": title, "paragraphs": f"Drafted {title}."}
                for title in expected_titles
            ]
        }),
    )

    result = answer_drafter.state2.run({"draftId": "draft-id", **selections})

    assert result["message"] == "Form processed successfully"
    assert memory["inputs/ad"] == "\n".join(expected_titles)
    for title in expected_titles:
        assert f"Drafted {title}." in memory["drafted_sections/affirmative_defenses"]
    pdf.assert_called_once()
    request = provider.parse.call_args.kwargs
    assert request["model"] == "gpt-6.1-sol"
    assert request["reasoning"] == {"effort": "high"}
    assert request["text_format"] is AnswerDraftedDefenses
    prompt = request["input"][-1]["content"]
    assert "Lawyer's selected defense: Equitable Estoppel" in prompt
    assert "Do not invent supporting case facts" in prompt
    assert f"Total {len(expected_titles)} Affirmative Defenses selected" in prompt
    if expected_reason:
        assert expected_reason in prompt


def test_blank_and_legacy_placeholder_defenses_are_not_drafted(provider, monkeypatch):
    memory = Memory({"ads_suggestions": json.dumps({}), "inputs/state": "state"})
    pdf = _patch_memory(monkeypatch, memory)

    result = answer_drafter.state2.run({
        "draftId": "draft-id",
        "ad0": "   ",
        "ad1": "New Affirmative Defense 2",
    })

    assert result["message"] == "Form processed successfully"
    assert memory["inputs/ad"] == ""
    assert memory["drafted_sections/affirmative_defenses"] == ""
    provider.parse.assert_not_called()
    pdf.assert_called_once()


def test_ignored_suggestion_does_not_displace_custom_defense(provider, monkeypatch):
    memory = Memory({
        "ads_suggestions": json.dumps({"Waiver": {"1": {"description": "Plaintiff waived its rights."}}}),
        "inputs/state": "state",
    })
    _patch_memory(monkeypatch, memory)
    provider.parse.return_value = _response(
        model="gpt-6.1-sol",
        parsed=AnswerDraftedDefenses.model_validate({
            "defenses": [{"title": "Equitable Estoppel", "paragraphs": "Drafted estoppel."}]
        }),
    )

    result = answer_drafter.state2.run({
        "draftId": "draft-id", "ad0": "Waiver", "ignore-ad0": "on", "ad1": "Equitable Estoppel",
    })

    assert result["message"] == "Form processed successfully"
    assert memory["inputs/ad"] == "Equitable Estoppel"
    prompt = provider.parse.call_args.kwargs["input"][-1]["content"]
    assert "Lawyer's selected defense: Waiver" not in prompt
    assert "Lawyer's selected defense: Equitable Estoppel" in prompt


@pytest.mark.parametrize(
    ("selected", "parsed"),
    [
        (False, None),
        (True, None),
        (True, AnswerDraftedDefenses(defenses=[])),
    ],
)
def test_defense_drafting_skips_unselected_and_rejects_missing_or_empty_output(
    provider, monkeypatch, selected, parsed
):
    memory = Memory({
        "ads_suggestions": json.dumps({"Waiver": {"1": {"description": "Waiver."}}}),
        "inputs/state": "state",
    })
    pdf = _patch_memory(monkeypatch, memory)
    provider.parse.return_value = _response(model="gpt-6.1-sol", parsed=parsed)

    result = answer_drafter.state2.run({
        "draftId": "draft-id", **({"ad1": "Waiver"} if selected else {})
    })

    if selected:
        assert result["message"] == "error"
        assert "drafted_sections/affirmative_defenses" not in memory
        pdf.assert_not_called()
    else:
        assert result["message"] == "Form processed successfully"
        assert memory["drafted_sections/affirmative_defenses"] == ""
        assert provider.parse.call_count == 0
        pdf.assert_called_once()


def test_counterclaim_uses_sol_high_text_and_stops_on_blank(provider, monkeypatch):
    memory = Memory({"summary": "Case summary."})
    pdf = _patch_memory(monkeypatch, memory)
    provider.create.return_value = _response(model="gpt-6.1-sol", text="Counterclaim text.")

    result = answer_drafter.state3.run({
        "draftId": "draft-id", "wants_counterclaim": "on", "ccFacts": "Facts."
    })

    assert result["message"] == "Form processed successfully"
    assert memory["drafted_sections/counter_claims"] == "Counterclaim text."
    request = provider.create.call_args.kwargs
    assert request["model"] == "gpt-6.1-sol"
    assert request["reasoning"] == {"effort": "high"}
    provider.parse.assert_not_called()
    assert pdf.called

    provider.create.reset_mock()
    provider.create.return_value = _response(model="gpt-6.1-sol", text="")
    memory.pop("drafted_sections/counter_claims")
    result = answer_drafter.state3.run({
        "draftId": "draft-id", "wants_counterclaim": "on", "ccFacts": "Facts."
    })
    assert result["message"] == "error"
    assert "drafted_sections/counter_claims" not in memory


@pytest.mark.parametrize("text", ["General denial text.", ""])
def test_general_denial_uses_sol_high_text_and_stops_on_blank(
    provider, monkeypatch, text
):
    memory = Memory({
        "complaint_text": json.dumps(["Caption", "Plaintiff alleges facts."]),
        "line_metadata": "[]",
        "complaint_cover_text": "Caption",
    })
    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter, "_answer_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        answer_drafter, "get_answer_complaint_structure_from_llm",
        lambda lines, metadata: {
            "plaintiffs": ["Acme Corp."],
            "defendants": ["Beta LLC"],
            "venue": "California Superior Court",
            "jurisdiction_type": "state",
            "state": "california",
            "first_body_line_number": 1,
        },
    )
    monkeypatch.setattr(
        answer_drafter, "get_answer_sections_and_coas_from_llm",
        lambda *args: {"sections": [], "causes_of_action": []},
    )
    digest = Mock(return_value={})
    monkeypatch.setattr(answer_drafter, "ensure_answer_factual_digest_and_ads", digest)
    provider.create.return_value = _response(model="gpt-6.1-sol", text=text)

    result = answer_drafter.generate_answer_analysis_from_memory(memory, "draft-id")

    request = provider.create.call_args.kwargs
    assert request["model"] == "gpt-6.1-sol"
    assert request["reasoning"] == {"effort": "high"}
    provider.parse.assert_not_called()
    if text:
        assert result["general_denial"] == text
        assert memory["analysis/general_denial"] == text
        digest.assert_called_once()
    else:
        assert result["message"] == "error"
        assert "analysis/general_denial" not in memory
        digest.assert_not_called()


def test_missing_typed_chunk_is_not_a_valid_empty_array(provider):
    provider.parse.return_value = _response(model="gpt-6-luna", parsed=None)
    chunk = answer_drafter._build_answer_section_chunk(1, 0, 0, 0, 0, 1, 1)

    result = answer_drafter._extract_answer_section_items_for_chunk(chunk, ["Heading"], [])

    assert result["items"] == []
    assert result["parse_failed"] is True
    assert result["split_retry"] is False
