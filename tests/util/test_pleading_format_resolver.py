"""Tests for pleading format resolver."""

import copy
import logging
from types import SimpleNamespace
from unittest.mock import patch

from docx import Document
from docx.shared import Inches

from backend.ai import base as ai_base
from backend.util.markdown_docx import create_everything_pleading_document
from backend.util.pleading_format import (
    DEFAULT_CAED_PLEADING_SPEC,
    DEFAULT_EXPORT_SPEC,
    DEFAULT_STRUCTURAL_SPEC,
)
from backend.util.pleading_format_resolver import (
    build_draft_excerpt,
    extract_heuristic_caption,
    infer_export_policy_heuristic,
    merge_llm_format_response,
    PleadingFormatResult,
    resolve_format_spec_for_export,
)


def _layout(spec: dict) -> dict:
    return spec["layout"]


def test_build_draft_excerpt_includes_title_and_sections():
    draft = {
        "title": "Complaint for Damages",
        "sections": [
            {
                "section_name": "Jurisdiction",
                "section_content": "28 U.S.C. § 1332.\n\nMore text.",
            },
            {"section_name": "Facts", "section_content": "x" * 2000},
        ],
    }
    excerpt = build_draft_excerpt(draft, max_sections=2, max_chars_per_section=50)
    assert "Complaint for Damages" in excerpt
    assert "Jurisdiction" in excerpt
    assert "truncated" in excerpt
    # 0-based index labels let the LLM reference caption-source sections.
    assert "[index 0]" in excerpt
    assert "[index 1]" in excerpt


def test_build_draft_excerpt_index_reflects_original_position_when_empty_skipped():
    draft = {
        "title": "T",
        "sections": [
            {"section_name": "", "section_content": ""},  # skipped, but index preserved
            {"section_name": "Caption", "section_content": "UNITED STATES DISTRICT COURT"},
            {"section_name": "Body", "section_content": "Allegation."},
        ],
    }
    excerpt = build_draft_excerpt(draft)
    assert "[index 1] " in excerpt and "Caption" in excerpt
    assert "[index 2] " in excerpt and "Body" in excerpt
    assert "[index 0]" not in excerpt  # the empty leading section is not emitted


def test_merge_llm_format_response_carries_caption_source_indices():
    spec = merge_llm_format_response(
        {
            "confidence": "high",
            "structural": {
                "require_caption_page": True,
                "caption_ready": True,
                "caption_source_section_indices": [0],
            },
            "caption": {"court_line_1": "UNITED STATES DISTRICT COURT"},
        },
        draft={"sections": [{"section_id": "1"}, {"section_id": "2"}]},
    )
    assert spec["structural"]["require_caption_page"] is True
    assert spec["structural"]["caption_source_section_indices"] == [0]


def test_merge_llm_format_response_validates_caption_source_indices():
    spec = merge_llm_format_response(
        {
            "confidence": "high",
            "structural": {
                "require_caption_page": True,
                "caption_ready": True,
                "caption_source_section_indices": [0, 5, -1, "x", 0],
            },
            "caption": {"court_line_1": "UNITED STATES DISTRICT COURT"},
        },
        draft={"sections": [{"section_id": "1"}, {"section_id": "2"}]},
    )
    # out-of-range, negative, non-int dropped; deduped + sorted; index 0 valid for a 2-section draft
    assert spec["structural"]["caption_source_section_indices"] == [0]


def test_merge_llm_format_response_zeroes_caption_source_indices_when_no_caption():
    spec = merge_llm_format_response(
        {
            "confidence": "low",
            "structural": {
                "require_caption_page": True,
                "caption_ready": True,
                "caption_source_section_indices": [0],
            },
        },
        draft={"sections": [{"section_id": "1"}]},
    )
    # low confidence forces caption off, so the indices are cleared.
    assert spec["structural"]["require_caption_page"] is False
    assert spec["structural"]["caption_source_section_indices"] == []


def test_merge_llm_format_response_heuristic_fallback_indices_empty():
    spec = merge_llm_format_response(None, draft={"sections": [{"section_id": "1"}]})
    assert spec["structural"]["caption_source_section_indices"] == []


def test_extract_heuristic_caption_from_constraints():
    draft = {"title": "Motion to Dismiss"}
    constraints = {
        "cases": {
            "Doe v. Acme": {
                "court": "United States District Court, Eastern District of California",
                "party_names": ["John Doe", "Acme Corp."],
            }
        }
    }
    hints = extract_heuristic_caption(draft, constraints)
    assert hints["court_line_1"] == "UNITED STATES DISTRICT COURT"
    assert "EASTERN" in hints["court_line_2"]
    assert hints["plaintiff"] == "John Doe"
    assert hints["defendant"] == "Acme Corp."
    assert hints["pleading_title"] == "Motion to Dismiss"


def test_merge_llm_format_response_low_confidence_uses_default_values():
    spec = merge_llm_format_response(
        {
            "confidence": "low",
            "jurisdiction": "unknown",
            "layout": {
                "font_size_pt": 8,
                "page_margins_in": {"left": 0.1},
            },
        }
    )
    assert "structural" in spec
    assert "layout" in spec
    assert _layout(spec)["font_size_pt"] == DEFAULT_CAED_PLEADING_SPEC["font_size_pt"]
    assert _layout(spec)["page_margins_in"]["left"] == DEFAULT_CAED_PLEADING_SPEC[
        "page_margins_in"
    ]["left"]
    assert spec["_meta"]["confidence"] == "low"
    assert spec["structural"]["require_caption_page"] is False
    assert spec["structural"]["use_vertical_lines"] is False


def test_merge_llm_format_response_low_confidence_disables_caption_even_if_requested():
    spec = merge_llm_format_response(
        {
            "confidence": "low",
            "structural": {
                "require_caption_page": True,
                "caption_ready": True,
                "use_vertical_lines": True,
            },
        }
    )
    assert spec["structural"]["require_caption_page"] is False
    assert spec["structural"]["use_vertical_lines"] is False


def test_merge_llm_format_response_low_confidence_formal_reply_keeps_caption():
    spec = merge_llm_format_response(
        {"confidence": "low"},
        draft={"title": "Plaintiff's Reply in Support of Motion to Dismiss"},
        constraints={},
    )
    assert spec["_meta"]["confidence"] == "medium"
    assert spec["structural"]["require_caption_page"] is True
    assert spec["structural"]["caption_ready"] is True
    assert spec["structural"]["caption"]["pleading_title"].startswith("Plaintiff")


def test_merge_llm_format_response_internal_analysis_no_caption():
    spec = merge_llm_format_response(
        {
            "confidence": "high",
            "document_class": "internal_analysis",
            "pleading_type": "research_memo",
            "structural": {
                "require_caption_page": False,
                "caption_ready": False,
                "use_vertical_lines": False,
            },
        }
    )
    assert spec["structural"]["require_caption_page"] is False
    assert spec["structural"]["use_vertical_lines"] is False


def test_merge_llm_format_response_ca_complaint_uses_vertical_lines():
    spec = merge_llm_format_response(
        {
            "confidence": "high",
            "jurisdiction": "US federal",
            "pleading_type": "complaint",
            "structural": {
                "require_caption_page": True,
                "caption_ready": True,
                "use_vertical_lines": True,
            },
            "caption": {
                "court_line_1": "UNITED STATES DISTRICT COURT",
                "court_line_2": "EASTERN DISTRICT OF CALIFORNIA",
            },
        }
    )
    assert spec["structural"]["require_caption_page"] is True
    assert spec["structural"]["use_vertical_lines"] is True


def test_merge_llm_format_response_caption_not_ready_disables_caption():
    spec = merge_llm_format_response(
        {
            "confidence": "high",
            "structural": {
                "require_caption_page": True,
                "caption_ready": False,
                "use_vertical_lines": True,
            },
        }
    )
    assert spec["structural"]["require_caption_page"] is False
    assert spec["structural"]["use_vertical_lines"] is False


def test_infer_export_policy_heuristic_with_court_constraints():
    draft = {"title": "Complaint for Damages"}
    constraints = {
        "cases": {
            "Doe v. Acme": {
                "court": "United States District Court, Eastern District of California",
                "party_names": ["John Doe", "Acme Corp."],
            }
        }
    }
    policy = infer_export_policy_heuristic(draft, constraints)
    assert policy["require_caption_page"] is True
    assert policy["caption_ready"] is True
    assert policy["use_vertical_lines"] is True


def test_infer_export_policy_heuristic_formal_reply_without_court_uses_caption():
    draft = {"title": "Plaintiff's Reply in Support of Motion to Dismiss"}
    policy = infer_export_policy_heuristic(draft, {})
    assert policy["require_caption_page"] is True
    assert policy["caption_ready"] is True
    assert policy["use_vertical_lines"] is False


def test_infer_export_policy_heuristic_internal_analysis():
    draft = {"title": "Internal Case Analysis"}
    policy = infer_export_policy_heuristic(draft, {})
    assert policy["require_caption_page"] is False
    assert policy["use_vertical_lines"] is False


def test_merge_llm_format_response_caption_overlay():
    spec = merge_llm_format_response(
        {
            "confidence": "high",
            "caption": {
                "plaintiff": "Jane Roe",
                "defendant": "BigCo LLC",
                "case_number": "2:24-cv-00123",
            },
        }
    )
    cap = spec["structural"]["caption"]
    assert cap["plaintiff"] == "Jane Roe"
    assert cap["defendant"] == "BigCo LLC"
    assert cap["case_number"] == "2:24-cv-00123"
    assert cap["court_line_1"] == DEFAULT_STRUCTURAL_SPEC["caption"]["court_line_1"]


def test_merge_llm_format_response_high_confidence_applies_bounds():
    spec = merge_llm_format_response(
        {
            "confidence": "high",
            "jurisdiction": "US federal",
            "court": "USDC ED Cal",
            "pleading_type": "complaint",
            "layout": {
                "font_name": "Times New Roman",
                "font_size_pt": 12,
                "page_margins_in": {"left": 1.25, "right": 1.0, "top": 1.0, "bottom": 1.0},
                "body": {"first_line_indent_in": 0.5, "line_spacing_pt": 24},
            },
        }
    )
    assert _layout(spec)["page_margins_in"]["left"] == 1.25
    assert spec["_meta"]["pleading_type"] == "complaint"
    assert _layout(spec)["font_size_pt"] == 12


def test_merge_llm_format_response_clamps_out_of_range():
    spec = merge_llm_format_response(
        {
            "confidence": "medium",
            "layout": {
                "font_size_pt": 99,
                "page_margins_in": {"left": 5.0},
            },
        }
    )
    assert _layout(spec)["font_size_pt"] == 14
    assert _layout(spec)["page_margins_in"]["left"] == 2.0


@patch("backend.util.pleading_format_resolver.chat_completion_parse")
def test_resolve_format_spec_for_export_uses_llm(mock_chat, caplog):
    raw = {
        "confidence": "high",
        "jurisdiction": "US federal",
        "court": "USDC",
        "pleading_type": "complaint",
        "layout": {
            "font_name": "Times New Roman",
            "font_size_pt": 12,
            "page_margins_in": {"left": 1.0, "right": 1.0, "top": 1.0, "bottom": 1.0},
            "body": {"first_line_indent_in": 0.5, "line_spacing_pt": 24, "line_spacing_rule": "exact"},
            "body_continued": {"first_line_indent_in": 0.0, "line_spacing_pt": 24},
            "block_quote": {"left_indent_in": 1.0, "right_indent_in": 1.0, "line_spacing_pt": 12, "alignment": "justify"},
            "title": {"bold": True, "all_caps": True, "centered": True},
            "title1": {"bold": True, "left_indent_in": 0.5},
            "title2": {"left_indent_in": 1.0},
            "title3": {"left_indent_in": 1.5, "hanging_indent_in": 0.5}
        },
        "sources": []
    }
    mock_chat.return_value = SimpleNamespace(model_dump=lambda **kwargs: raw)
    draft = {"title": "Complaint", "sections": [{"section_name": "Intro", "section_content": "Body"}]}
    caplog.set_level(logging.INFO, logger="backend.util.pleading_format_resolver")
    spec = resolve_format_spec_for_export(
        draft,
        constraints={},
        conversation_brief="",
        model="gpt-6.1-sol",
        reasoning_effort="low",
    )
    assert spec["_meta"]["confidence"] == "high"
    assert "layout" in spec
    assert "pleading_format_resolver: confidence=high jurisdiction=US federal" in caplog.text
    mock_chat.assert_called_once()
    assert mock_chat.call_args.args[1] is PleadingFormatResult
    assert mock_chat.call_args.kwargs["model"] == "gpt-6.1-sol"
    assert mock_chat.call_args.kwargs["reasoning_effort"] == "low"


def test_resolve_format_spec_for_export_uses_responses_parse(monkeypatch):
    parsed = PleadingFormatResult.model_validate({
        "confidence": "low",
        "jurisdiction": "",
        "court": "",
        "pleading_type": "memo",
        "document_class": "internal_analysis",
        "caption_reason": "Internal draft",
        "sources": [],
        "structural": {
            "require_caption_page": False,
            "require_numbered_paragraphs": False,
            "continuous_paragraph_numbering": True,
            "use_vertical_lines": False,
            "caption_ready": False,
            "caption_source_section_indices": [],
        },
        "caption": {
            "court_line_1": "", "court_line_2": "", "plaintiff": "",
            "defendant": "", "case_number": "", "pleading_title": "",
            "attorney_contact": None,
        },
        "layout": None,
    })
    requests = []

    def fake_parse(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            id="resp_format",
            status="completed",
            model=kwargs["model"],
            output_parsed=parsed,
            output=[],
            usage=SimpleNamespace(total_tokens=23),
        )

    monkeypatch.setattr(ai_base, "get_openai_client", lambda: SimpleNamespace(
        responses=SimpleNamespace(parse=fake_parse)
    ))
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", lambda *args, **kwargs: None)

    result = resolve_format_spec_for_export(
        {"title": "Research Memo", "sections": []},
        model="gpt-6.1-sol",
        reasoning_effort="low",
    )

    assert result["_meta"]["document_class"] == "internal_analysis"
    assert len(requests) == 1
    assert requests[0]["model"] == "gpt-6.1-sol"
    assert requests[0]["reasoning"] == {"effort": "low"}
    assert requests[0]["text_format"] is PleadingFormatResult


@patch("backend.util.pleading_format_resolver.chat_completion_parse")
def test_resolve_format_spec_for_export_fallback_on_bad_json(mock_chat):
    mock_chat.return_value = None
    spec = resolve_format_spec_for_export({"title": "T", "sections": []})
    assert _layout(spec)["font_size_pt"] == DEFAULT_CAED_PLEADING_SPEC["font_size_pt"]
    assert spec["structural"]["require_caption_page"] is False


@patch("backend.util.pleading_format_resolver.chat_completion_parse")
def test_resolve_format_spec_for_export_fallback_formal_reply_caption(mock_chat):
    mock_chat.return_value = None
    spec = resolve_format_spec_for_export(
        {"title": "Plaintiff's Reply in Support of Motion to Dismiss", "sections": []}
    )
    assert spec["structural"]["require_caption_page"] is True
    assert spec["structural"]["caption_ready"] is True


@patch("backend.util.pleading_format_resolver.chat_completion_parse")
def test_resolve_format_spec_for_export_fallback_on_llm_error(mock_chat):
    mock_chat.side_effect = RuntimeError("api down")
    spec = resolve_format_spec_for_export({"title": "T", "sections": []})
    assert spec["layout"] == DEFAULT_EXPORT_SPEC["layout"]
    assert spec["structural"]["require_caption_page"] is False


@patch("backend.util.pleading_format_resolver.chat_completion_parse")
def test_resolve_format_spec_for_export_heuristic_caption_with_constraints(mock_chat):
    mock_chat.return_value = None
    constraints = {
        "cases": {
            "Doe v. Acme": {
                "court": "United States District Court, Eastern District of California",
                "party_names": ["John Doe", "Acme Corp."],
            }
        }
    }
    spec = resolve_format_spec_for_export(
        {"title": "Complaint", "sections": []},
        constraints=constraints,
    )
    assert spec["structural"]["require_caption_page"] is True
    assert spec["structural"]["use_vertical_lines"] is True


def test_create_everything_pleading_document_accepts_custom_format_spec():
    custom = copy.deepcopy(DEFAULT_EXPORT_SPEC)
    custom["structural"]["require_caption_page"] = False
    custom["layout"]["page_margins_in"] = {
        "left": 1.5,
        "right": 1.5,
        "top": 1.5,
        "bottom": 1.5,
    }
    doc = create_everything_pleading_document(
        "Test",
        [{"section_name": "A", "section_content": "Paragraph."}],
        format_spec=custom,
    )
    assert doc.sections[0].left_margin == Inches(1.5)
