"""Structured heading extraction shared by complaint and RFP workflows."""

import logging
from types import SimpleNamespace

import pytest

from backend.ai import base


HEADINGS = ["COVER", "VENUE", "FACTS", "COUNT I", "PRAYER", "SIGNATURE"]
LINE_NUMBERS = [0, 5, 10, 20, 30, 40]
BODY_LINES = [f"line {index}" for index in range(45)]


def _parsed(section_headings, next_heading):
    return base.HeadingSectionResult.model_validate({
        "section headings": section_headings,
        "first heading of next section": next_heading,
    })


def test_adjacent_heading_candidates_preserve_source_lines_and_default_behavior():
    lines = [
        "COVER", "Caption detail", "1. Body allegation", "FACTS", "",
        "SECOND CAUSE OF ACTION",
        "Violation of California Uniform Trade Secret Act, Cal. Civ. Code § 3426 et seq.",
        "(Against All Defendants)", "12, The defendants acted.",
        "COUNT III: BREACH OF CONTRACT", "13. Another body paragraph", "PRAYER",
    ]
    assert base.extract_headings(lines) == (
        ["COVER", "FACTS", "SECOND CAUSE OF ACTION", "COUNT III: BREACH OF CONTRACT", "PRAYER"],
        [0, 3, 5, 9, 11],
    )
    assert base.extract_headings(lines, include_adjacent_lines=True) == (
        [
            "COVER", "Caption detail", "FACTS", "SECOND CAUSE OF ACTION",
            "Violation of California Uniform Trade Secret Act, Cal. Civ. Code § 3426 et seq.",
            "(Against All Defendants)", "COUNT III: BREACH OF CONTRACT", "PRAYER",
        ],
        [0, 1, 3, 5, 6, 7, 9, 11],
    )
    assert base.extract_headings(
        ["COUNT I", "COUNT II", "Mixed-case title", "1. Body allegation"],
        include_adjacent_lines=True,
    ) == (["COUNT I", "COUNT II", "Mixed-case title"], [0, 1, 2])


def test_coa_boundary_prompt_includes_multiline_heading_with_original_offsets(monkeypatch):
    lines = [f"ordinary line {index}" for index in range(30)]
    lines[0] = "COVER"
    lines[3] = "FACTS"
    lines[10] = "SECOND CAUSE OF ACTION"
    lines[11] = "Violation of California Uniform Trade Secret Act, Cal. Civ. Code § 3426 et seq."
    lines[12] = "(Against All Defendants)"
    lines[13] = "14. Body paragraph"
    lines[20] = "PRAYER FOR RELIEF"
    lines[25] = "SIGNATURE"
    prompts = []

    def parsed(prompt, response_model, **_kwargs):
        prompts.append(prompt)
        assert response_model is base.HeadingSectionResult
        return _parsed("SECOND CAUSE OF ACTION (10)", "PRAYER FOR RELIEF (20)")

    monkeypatch.setattr(base, "chat_completion_parse", parsed)
    assert base.get_coa_loc(lines) == [10, 20]
    assert len(prompts) == 1
    assert "SECOND CAUSE OF ACTION\t(10)" in prompts[0]
    assert f"{lines[11]}\t(11)" in prompts[0]
    assert "(Against All Defendants)\t(12)" in prompts[0]
    assert "14. Body paragraph\t(13)" not in prompts[0]


def test_split_bold_cause_title_is_complete_in_prompt_and_info_log(monkeypatch, caplog):
    lines = [f"ordinary line {index}" for index in range(30)]
    lines[0] = "COVER"
    lines[3] = "FACTS"
    lines[10] = "<B>SECOND CAUSE OF ACTION</B>"
    lines[11] = "<B>Violation of California Uniform Trade Secret Act, Cal."
    lines[12] = "Civ."
    lines[13] = "Code § 3426 et seq.</B>"
    lines[14] = "<B>(Against All Defendants)</B>"
    lines[15] = "78. Body allegation"
    lines[20] = "PRAYER FOR RELIEF"
    lines[25] = "SIGNATURE"
    prompts = []

    def parsed(prompt, response_model, **_kwargs):
        prompts.append(prompt)
        assert response_model is base.HeadingSectionResult
        return _parsed("SECOND CAUSE OF ACTION (10)", "PRAYER FOR RELIEF (20)")

    monkeypatch.setattr(base, "chat_completion_parse", parsed)
    with caplog.at_level(logging.INFO, logger=base.__name__):
        assert base.get_coa_loc(lines) == [10, 20]

    assert len(prompts) == 1
    for line_num in range(10, 15):
        expected_line = lines[line_num].replace("<B>", "").replace("</B>", "")
        assert f"{expected_line}\t({line_num})" in prompts[0]
    assert "78. Body allegation\t(15)" not in prompts[0]
    log_messages = [record.getMessage() for record in caplog.records]
    assert any("get_coa_loc assembled heading prompt:" in message
               and prompts[0] in message for message in log_messages)


def test_adjacent_candidates_do_not_bypass_original_heading_count_fallback(monkeypatch):
    lines = ["COVER", "Caption", "FACTS", "Background", "COUNT I", "Claim title", "PRAYER"]
    monkeypatch.setattr(base, "chat_completion_parse", lambda *_a, **_kw: pytest.fail(
        "four original headings should use the existing whole-document fallback"
    ))
    assert base.get_coa_loc(lines) == [0, len(lines) - 1]


def test_heading_provider_uses_fixed_schema_and_default_sol_medium(monkeypatch):
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            id="heading-response",
            status="completed",
            output_parsed=_parsed("COUNT I (20)", "PRAYER (30)"),
            usage=SimpleNamespace(total_tokens=12),
            output=[],
        )

    monkeypatch.setattr(base, "get_openai_client", lambda: SimpleNamespace(
        responses=SimpleNamespace(parse=parse),
    ))
    monkeypatch.setattr(base, "record_scoped_llm_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(base, "record_guided_llm_response", lambda *_a, **_kw: None)

    assert base.start_end_from_headings("find section", HEADINGS, LINE_NUMBERS) == [20, 30]
    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-6.1-sol"
    assert calls[0]["reasoning"] == {"effort": "medium"}
    assert calls[0]["text_format"] is base.HeadingSectionResult
    assert "text" not in calls[0]
    assert calls[0]["input"][-1] == {"role": "user", "content": "find section"}
    schema = base.HeadingSectionResult.model_json_schema(by_alias=True)
    assert set(schema["required"]) == {
        "section headings", "first heading of next section",
    }


@pytest.mark.parametrize(
    ("wrapper", "section_headings", "next_heading", "expected"),
    [
        ("fa", "FACTS (10)", "COUNT I (20)", [10, 20]),
        ("coa", "COUNT I (20)", "PRAYER (30)", [20, 30]),
        ("rfp", "REQUESTS (20)", "SIGNATURE (40)", [20, 40]),
        ("venue", "VENUE (5)", "FACTS (10)", BODY_LINES[5:10]),
    ],
)
def test_all_heading_wrappers_preserve_outputs(
    monkeypatch, wrapper, section_headings, next_heading, expected,
):
    monkeypatch.setattr(base, "extract_headings", lambda _lines, **_kwargs: (HEADINGS, LINE_NUMBERS))
    calls = []

    def parsed(prompt, response_model, **kwargs):
        calls.append((prompt, response_model, kwargs))
        return _parsed(section_headings, next_heading)

    monkeypatch.setattr(base, "chat_completion_parse", parsed)
    if wrapper == "fa":
        result = base.get_fa_loc(BODY_LINES)
    elif wrapper == "coa":
        result = base.get_coa_loc(BODY_LINES, model="gpt-6.1-sol", reasoning_effort="medium")
    elif wrapper == "rfp":
        result = base.get_rfp_section_text(BODY_LINES, "specific requests for production")
        assert '"section headings"' in calls[0][0]
        assert '"specific requests for production headings"' not in calls[0][0]
    else:
        result = base.get_venue_text("\n".join(BODY_LINES))

    assert result == expected
    assert len(calls) == 1
    assert calls[0][1] is base.HeadingSectionResult


def test_heading_parser_keeps_multiple_boundaries_and_missing_next_heading(monkeypatch):
    monkeypatch.setattr(base, "chat_completion_parse", lambda *_a, **_kw: _parsed(
        "CAUSES (19); COUNT I (20); COUNT II (25)", "",
    ))
    assert base.start_end_from_headings("prompt", HEADINGS, LINE_NUMBERS) == [19, 25, 40]


@pytest.mark.parametrize("wrapper", ["fa", "coa", "rfp", "venue"])
def test_missing_parse_retries_then_preserves_whole_document_fallback(monkeypatch, wrapper):
    monkeypatch.setattr(base, "extract_headings", lambda _lines, **_kwargs: (HEADINGS, LINE_NUMBERS))
    attempts = []

    def missing(*_args, **_kwargs):
        attempts.append(1)
        return None

    monkeypatch.setattr(base, "chat_completion_parse", missing)
    if wrapper == "fa":
        result = base.get_fa_loc(BODY_LINES)
    elif wrapper == "coa":
        result = base.get_coa_loc(BODY_LINES)
    elif wrapper == "rfp":
        result = base.get_rfp_section_text(BODY_LINES, "specific requests")
    else:
        result = base.get_venue_text("\n".join(BODY_LINES))

    assert len(attempts) == 2
    if wrapper in {"fa", "coa"}:
        assert result == [0, len(BODY_LINES) - 1]
    elif wrapper == "rfp":
        assert result == [0, LINE_NUMBERS[-1]]
    else:
        assert result == BODY_LINES[:LINE_NUMBERS[-1]]


def test_wrapper_retries_missing_parse_then_uses_valid_boundaries(monkeypatch):
    monkeypatch.setattr(base, "extract_headings", lambda _lines, **_kwargs: (HEADINGS, LINE_NUMBERS))
    results = iter([None, _parsed("COUNT I (20)", "PRAYER (30)")])
    monkeypatch.setattr(base, "chat_completion_parse", lambda *_a, **_kw: next(results))
    assert base.get_coa_loc(BODY_LINES) == [20, 30]


def test_rfp_response_request_slice_stops_before_signature(monkeypatch):
    lines = BODY_LINES.copy()
    lines[20] = "REQUEST NO. 1: Produce contracts."
    lines[39] = "REQUEST NO. 2: Produce emails."
    lines[40] = "SIGNATURE"
    monkeypatch.setattr(base, "extract_headings", lambda _lines, **_kwargs: (HEADINGS, LINE_NUMBERS))
    monkeypatch.setattr(base, "chat_completion_parse", lambda *_a, **_kw: _parsed(
        "REQUESTS (20)", "SIGNATURE (40)",
    ))

    start, end = base.get_rfp_section_text(lines, "specific requests for production of Documents")
    requests_text = lines[start:end]
    assert requests_text[0] == "REQUEST NO. 1: Produce contracts."
    assert requests_text[-1] == "REQUEST NO. 2: Produce emails."
    assert "SIGNATURE" not in requests_text


@pytest.mark.parametrize("wrapper", ["fa", "coa", "rfp", "venue"])
def test_fewer_than_five_headings_keeps_existing_no_call_fallback(monkeypatch, wrapper):
    monkeypatch.setattr(base, "extract_headings", lambda _lines, **_kwargs: (["FACTS"], [5]))
    monkeypatch.setattr(base, "chat_completion_parse", lambda *_a, **_kw: pytest.fail(
        "provider should not be called for a heading-poor document"
    ))
    if wrapper == "fa":
        assert base.get_fa_loc(BODY_LINES) == [0, len(BODY_LINES) - 1]
    elif wrapper == "coa":
        assert base.get_coa_loc(BODY_LINES) == [0, len(BODY_LINES) - 1]
    elif wrapper == "rfp":
        assert base.get_rfp_section_text(BODY_LINES, "requests") == [0, len(BODY_LINES) - 1]
    else:
        assert base.get_venue_text("\n".join(BODY_LINES)) == BODY_LINES


def test_missing_section_headings_still_raises_for_wrapper_retry(monkeypatch):
    monkeypatch.setattr(base, "chat_completion_parse", lambda *_a, **_kw: _parsed("", ""))
    with pytest.raises(ValueError, match="No line number"):
        base.start_end_from_headings("prompt", HEADINGS, LINE_NUMBERS)
