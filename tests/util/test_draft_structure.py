from pathlib import Path

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from backend.util import draft_structure


@pytest.mark.parametrize("items", [[1], [1, 2]])
def test_parallel_structure_propagates_celery_soft_timeout(items):
    def worker(_item):
        raise SoftTimeLimitExceeded()

    with pytest.raises(SoftTimeLimitExceeded):
        draft_structure._parallel_map(items, worker)


def test_detect_heading_candidates_from_bold_caps_and_numbering():
    lines = draft_structure.line_records_from_text(
        "caption text\n"
        "<B>INTRODUCTION</B>\n"
        "ordinary sentence\n"
        "ARGUMENT\n"
        "A. Plaintiff Fails To Plead Reliance\n"
        "== End of page 2 ==\n"
    )

    candidates = draft_structure.detect_heading_candidates(lines)

    assert [candidate["line"] for candidate in candidates] == [2, 4, 5]
    assert [candidate["reason"] for candidate in candidates] == [
        "bold_line",
        "all_caps",
        "numbered_heading",
    ]


def test_assemble_section_content_from_heading_and_paragraph_blocks():
    blocks = [
        {"block_id": "b_0001", "type": "heading", "level": 2, "text": "A. First Issue"},
        {
            "block_id": "b_0002",
            "type": "paragraph",
            "paragraph_index": 1,
            "paragraph_number": None,
            "text": "The first paragraph.",
        },
    ]

    assert draft_structure.assemble_section_content(blocks) == (
        "**A. First Issue**\n\nThe first paragraph."
    )


def test_assemble_section_content_normalizes_heading_block_markers():
    blocks = [
        {"block_id": "b_0001", "type": "heading", "level": 2, "text": "##A. First Issue"},
        {"block_id": "b_0002", "type": "paragraph", "text": "The first paragraph."},
    ]

    assert draft_structure.assemble_section_content(blocks) == (
        "**A. First Issue**\n\nThe first paragraph."
    )


def test_normalize_section_content_markdown_converts_hash_headings():
    assert draft_structure.normalize_section_content_markdown(
        "Intro.\n##A. First Issue\nText.\n\n### 1. Subpoint\nMore."
    ) == (
        "Intro.\n\n**A. First Issue**\n\nText.\n\n**1. Subpoint**\n\nMore."
    )


def test_structure_draft_text_preserves_preface_and_uses_source_text_only():
    raw_text = (
        "Caption before first heading\n"
        "\n"
        "<B>INTRODUCTION</B>\n"
        "This motion should be denied.\n"
        "The record confirms the issue.\n"
        "\n"
        "ARGUMENT\n"
        "A. Plaintiff Fails To Plead Reliance\n"
        "Plaintiff offers no reliance facts.\n"
        "12. Plaintiff alleges one numbered paragraph.\n"
        "13. Plaintiff alleges another numbered paragraph.\n"
        "== End of page 1 ==\n"
    )
    calls = []

    def fake_llm_json(prompt=None, model=None):
        calls.append((prompt, model))
        assert model == "gpt-5.5"
        if "Candidate heading lines" in prompt:
            return {"headings": [{"line": 3}, {"line": 7}]}
        if "Section name: INTRODUCTION" in prompt:
            return {
                "blocks": [
                    {
                        "type": "paragraph",
                        "start_line": 4,
                        "end_line": 5,
                        "paragraph_number": "999",
                    }
                ]
            }
        if "Section name: ARGUMENT" in prompt:
            return {
                "blocks": [
                    {"type": "heading", "start_line": 8, "end_line": 8, "level": 2},
                    {"type": "paragraph", "start_line": 9, "end_line": 11},
                ]
            }
        return {"blocks": []}

    result = draft_structure.structure_draft_text(
        raw_text,
        title="Brief",
        llm_json_fn=fake_llm_json,
    )

    assert result["title"] == "Brief"
    assert "schema_version" not in result
    assert [section["section_name"] for section in result["sections"]] == [
        "",
        "INTRODUCTION",
        "ARGUMENT",
    ]
    assert result["sections"][0]["blocks"][0]["text"] == "Caption before first heading"
    assert result["sections"][1]["blocks"][0]["text"] == (
        "This motion should be denied. The record confirms the issue."
    )
    assert result["sections"][1]["blocks"][0]["paragraph_number"] is None
    argument_blocks = result["sections"][2]["blocks"]
    assert [block["type"] for block in argument_blocks] == [
        "heading",
        "paragraph",
        "paragraph",
        "paragraph",
    ]
    assert argument_blocks[2]["paragraph_number"] == "12"
    assert argument_blocks[3]["paragraph_number"] == "13"
    assert "== End of page" not in result["sections"][2]["section_content"]
    assert all("LLM" not in block["text"] for section in result["sections"] for block in section["blocks"])
    assert len(calls) == 4


def test_structure_draft_text_uses_typed_parse_and_passes_effort(monkeypatch):
    calls = []

    def fake_parse(prompt, response_model, *, model, reasoning_effort, **kwargs):
        calls.append((response_model, model, reasoning_effort))
        if response_model is draft_structure._HeadingSelection:
            return response_model(headings=[{"line": 1}])
        return response_model(blocks=[{
            "type": "paragraph",
            "start_line": 2,
            "end_line": 2,
            "paragraph_number": None,
            "level": None,
        }])

    monkeypatch.setattr(draft_structure, "chat_completion_parse", fake_parse)

    result = draft_structure.structure_draft_text(
        "<B>INTRODUCTION</B>\nBody text.",
        model="gpt-6.1-sol",
        reasoning_effort="low",
    )

    assert [call[0] for call in calls] == [
        draft_structure._HeadingSelection,
        draft_structure._SectionBlocks,
    ]
    assert all(call[1:] == ("gpt-6.1-sol", "low") for call in calls)
    assert result["sections"][0]["blocks"][0]["text"] == "Body text."


def test_structure_draft_text_falls_back_when_parse_is_unavailable(monkeypatch):
    monkeypatch.setattr(draft_structure, "chat_completion_parse", lambda *args, **kwargs: None)

    result = draft_structure.structure_draft_text(
        "<B>INTRODUCTION</B>\nBody text.",
        model="gpt-6.1-sol",
        reasoning_effort="medium",
    )

    assert result["sections"][0]["section_name"] == ""
    assert "INTRODUCTION" in result["sections"][0]["section_content"]
    assert "Body text." in result["sections"][0]["section_content"]


def test_normalize_draft_structure_uses_typed_parse_and_passes_effort(monkeypatch):
    calls = []

    def fake_parse(prompt, response_model, *, model, reasoning_effort, **kwargs):
        calls.append((response_model, model, reasoning_effort))
        return response_model(blocks=[{
            "type": "paragraph",
            "start_line": 1,
            "end_line": 1,
            "paragraph_number": None,
            "level": None,
        }])

    monkeypatch.setattr(draft_structure, "chat_completion_parse", fake_parse)
    result = draft_structure.normalize_draft_structure(
        {"title": "Draft", "sections": [{"section_name": "Intro", "section_content": "Body."}]},
        model="gpt-6.1-sol",
        reasoning_effort="medium",
    )

    assert calls == [(draft_structure._SectionBlocks, "gpt-6.1-sol", "medium")]
    assert result["sections"][0]["blocks"][0]["text"] == "Body."


def test_validate_and_repair_paragraph_numbers_merges_orphan_number_line():
    lines = draft_structure.line_records_from_text("1.\nPlaintiff alleges facts.\n")
    blocks = [
        {"type": "paragraph", "start_line": 1, "end_line": 1},
        {"type": "paragraph", "start_line": 2, "end_line": 2},
    ]

    repaired = draft_structure.validate_and_repair_paragraph_numbers(blocks, lines)

    assert len(repaired) == 1
    assert repaired[0]["paragraph_index"] == 1
    assert repaired[0]["paragraph_number"] == "1"
    assert repaired[0]["text"] == "1. Plaintiff alleges facts."


def test_validate_and_repair_paragraph_numbers_splits_two_numbered_paragraphs():
    lines = draft_structure.line_records_from_text(
        "1. First paragraph starts here.\n"
        "First paragraph continues.\n"
        "2. Second paragraph starts here.\n"
    )
    blocks = [{"type": "paragraph", "start_line": 1, "end_line": 3}]

    repaired = draft_structure.validate_and_repair_paragraph_numbers(blocks, lines)

    assert [block["paragraph_index"] for block in repaired] == [1, 2]
    assert [block["paragraph_number"] for block in repaired] == ["1", "2"]
    assert repaired[0]["text"] == "1. First paragraph starts here. First paragraph continues."
    assert repaired[1]["text"] == "2. Second paragraph starts here."


def test_validate_draft_structure_reports_repeated_paragraph_numbers():
    structure = {
        "title": "Complaint",
        "sections": [
            {
                "section_id": "s_0001",
                "section_name": "",
                "level": 1,
                "blocks": [
                    {
                        "block_id": "b_0001",
                        "type": "paragraph",
                        "paragraph_index": 1,
                        "paragraph_number": "1",
                        "text": "1. First.",
                    },
                    {
                        "block_id": "b_0002",
                        "type": "paragraph",
                        "paragraph_index": 2,
                        "paragraph_number": "1",
                        "text": "1. Repeated.",
                    },
                ],
                "section_content": "1. First.\n\n1. Repeated.",
            }
        ],
    }

    report = draft_structure.validate_draft_structure(
        structure,
        draft_structure.line_records_from_text("1. First.\n1. Repeated."),
    )

    assert "repeated paragraph_number: 1" in report["warnings"]


def test_sanitize_draft_structure_preserves_valid_blocks_and_empty_section_name():
    draft = {
        "title": "Draft",
        "sections": [
            {
                "section_id": "intro",
                "section_name": "",
                "level": 1,
                "blocks": [
                    {
                        "block_id": "b_custom",
                        "type": "paragraph",
                        "text": "Preface paragraph.",
                        "paragraph_index": 99,
                        "paragraph_number": "999",
                    }
                ],
                "section_content": "Preface paragraph.",
            }
        ],
    }

    result = draft_structure.sanitize_draft_structure(draft)

    assert result["sections"][0]["section_name"] == ""
    assert result["sections"][0]["section_content"] == "Preface paragraph."
    assert result["sections"][0]["blocks"][0]["block_id"] == "b_custom"
    assert result["sections"][0]["blocks"][0]["paragraph_index"] == 1
    assert result["sections"][0]["blocks"][0]["paragraph_number"] is None


def test_sanitize_draft_structure_drops_stale_blocks_when_content_changed():
    draft = {
        "title": "Draft",
        "sections": [
            {
                "section_id": "1",
                "section_name": "INTRODUCTION",
                "blocks": [
                    {"block_id": "b_0001", "type": "paragraph", "text": "Old text."},
                ],
                "section_content": "New text.",
            }
        ],
    }

    result = draft_structure.sanitize_draft_structure(draft)

    assert "blocks" not in result["sections"][0]
    assert result["sections"][0]["section_content"] == "New text."


def test_normalize_draft_structure_creates_blocks_from_section_content():
    calls = []

    def fake_llm_json(prompt=None, model=None):
        calls.append(prompt)
        assert model == "gpt-5.5"
        assert "Candidate heading lines" not in prompt
        return {
            "blocks": [
                {"type": "heading", "start_line": 1, "end_line": 1, "level": 2},
                {"type": "paragraph", "start_line": 2, "end_line": 3},
            ]
        }

    result = draft_structure.normalize_draft_structure(
        {
            "title": "Draft",
            "sections": [
                {
                    "section_id": "7",
                    "section_name": "ARGUMENT",
                    "section_content": "A. First Issue\nThe first paragraph.\nIt continues.",
                }
            ],
        },
        llm_json_fn=fake_llm_json,
    )

    section = result["sections"][0]
    assert section["section_id"] == "7"
    assert section["section_name"] == "ARGUMENT"
    assert [block["type"] for block in section["blocks"]] == ["heading", "paragraph"]
    assert section["section_content"] == "**A. First Issue**\n\nThe first paragraph. It continues."
    assert len(calls) == 1


def test_draft_needs_normalization_detects_missing_valid_blocks():
    assert draft_structure.draft_needs_normalization({
        "title": "Draft",
        "sections": [{"section_id": "1", "section_name": "Intro", "section_content": "Body."}],
    }) is True
    assert draft_structure.draft_needs_normalization({
        "title": "Draft",
        "sections": [
            {
                "section_id": "1",
                "section_name": "Intro",
                "section_content": "Body.",
                "blocks": [{"block_id": "b_0001", "type": "paragraph", "text": "Body."}],
            }
        ],
    }) is False


def _parse_static_pdf_or_skip(monkeypatch, relative_path):
    import backend.constants as constants

    monkeypatch.setattr(constants, "PDF_OCR_BACKEND", "local")
    try:
        from backend.util.data_tools import get_text_from_extended_formats
    except Exception as exc:
        pytest.skip(f"extended parser unavailable: {exc}")

    path = Path("backend/static") / relative_path
    if not path.exists():
        pytest.skip(f"missing static fixture: {path}")
    try:
        parsed_text, _meta, _is_image = get_text_from_extended_formats(str(path))
    except Exception as exc:
        pytest.skip(f"could not parse static fixture {path}: {exc}")
    return parsed_text


def _fixture_llm(prompt=None, model=None):
    assert model == "gpt-5.5"
    if "Candidate heading lines" in prompt:
        # Use the first few detected candidates so the utility exercises sectioning.
        import json
        import re

        payload_match = re.search(r"\[\s*\{.*?\}\s*\]", prompt, re.S)
        if not payload_match:
            return {"headings": []}
        candidates = json.loads(payload_match.group(0))
        return {"headings": [{"line": item["line"]} for item in candidates[:3]]}
    return {"blocks": []}


def test_static_hallucinated_brief_fixture_structures_without_live_llm(monkeypatch):
    parsed_text = _parse_static_pdf_or_skip(monkeypatch, "Hallucinated Brief.pdf")
    excerpt = "\n".join(parsed_text.splitlines()[:220])

    result = draft_structure.structure_draft_text(
        excerpt,
        title="Hallucinated Brief",
        llm_json_fn=_fixture_llm,
    )

    assert result["title"] == "Hallucinated Brief"
    assert result["sections"]
    assert "schema_version" not in result
    for section in result["sections"]:
        assert "section_content" in section
        assert section["section_content"] == draft_structure.assemble_section_content(section["blocks"])
        assert {block["type"] for block in section["blocks"]}.issubset(draft_structure.BLOCK_TYPES)


def test_static_complaint_fixture_checks_paragraph_numbers(monkeypatch):
    parsed_text = _parse_static_pdf_or_skip(monkeypatch, "COMPLAINT.pdf")
    excerpt = "\n".join(parsed_text.splitlines()[:260])

    result = draft_structure.structure_draft_text(
        excerpt,
        title="Complaint",
        llm_json_fn=_fixture_llm,
    )

    paragraphs = [
        block
        for section in result["sections"]
        for block in section["blocks"]
        if block["type"] == "paragraph"
    ]
    numbered = [block for block in paragraphs if block.get("paragraph_number")]

    assert result["sections"]
    assert paragraphs
    assert all(block["paragraph_index"] >= 1 for block in paragraphs)
    assert all(
        block.get("paragraph_number") == draft_structure.detect_paragraph_number(block["text"])
        for block in numbered
    )


def test_structure_draft_file_parses_path_and_attaches_source(monkeypatch, tmp_path):
    import backend.util.data_tools as data_tools

    source_path = tmp_path / "uploaded brief.pdf"
    source_path.write_bytes(b"%PDF")

    def fake_get_text_from_extended_formats(path):
        assert path == str(source_path)
        return "INTRODUCTION\nBody line\nARGUMENT\nArgument line\nExtra line", "meta", False

    def fake_llm_json(prompt=None, model=None):
        assert model == "gpt-5.5"
        if "Candidate heading lines" in prompt:
            return {"headings": [{"line": 1}]}
        return {"blocks": []}

    monkeypatch.setattr(data_tools, "get_text_from_extended_formats", fake_get_text_from_extended_formats)

    result = draft_structure.structure_draft_file(
        source_path,
        max_lines=2,
        pdf_ocr_backend="local",
        llm_json_fn=fake_llm_json,
    )

    assert result["title"] == "uploaded brief"
    assert result["source"] == {
        "path": str(source_path),
        "metadata": "meta",
        "is_image": False,
        "max_lines": 2,
    }
    assert [section["section_name"] for section in result["sections"]] == ["INTRODUCTION"]


def test_main_prints_structured_json(monkeypatch, tmp_path, capsys):
    source_path = tmp_path / "brief.txt"
    source_path.write_text("INTRODUCTION\nBody", encoding="utf-8")

    def fake_structure_draft_file(file_path, **kwargs):
        assert file_path == str(source_path)
        assert kwargs["title"] == "My Brief"
        assert kwargs["max_lines"] == 25
        assert kwargs["pdf_ocr_backend"] == "local"
        return {"title": "My Brief", "sections": [], "source": {"path": file_path}}

    monkeypatch.setattr(draft_structure, "structure_draft_file", fake_structure_draft_file)

    exit_code = draft_structure.main([
        str(source_path),
        "--title",
        "My Brief",
        "--max-lines",
        "25",
        "--pdf-ocr-backend",
        "local",
    ])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert '"title": "My Brief"' in captured.out
    assert '"sections": []' in captured.out
