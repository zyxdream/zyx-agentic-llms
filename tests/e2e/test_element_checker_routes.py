"""Provider routing and findings-shape regressions for Element Checker."""

import json
from types import SimpleNamespace

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from backend import element_checker
from backend.ai import base as ai_base
from backend.util.db import DBMemory


def _response(model, text="", parsed=None):
    return SimpleNamespace(
        id="element-checker-test",
        model=model,
        status="completed",
        output_text=text,
        output_parsed=parsed,
        output=[],
        usage=SimpleNamespace(total_tokens=7),
    )


def test_shared_heading_helpers_keep_their_existing_defaults(monkeypatch):
    raw_calls = []
    parsed_calls = []
    headings = ["FACTS", "COUNT I", "PRAYER", "A", "B", "C"]
    line_numbers = [0, 5, 10, 10, 10, 11]

    monkeypatch.setattr(
        ai_base,
        "extract_raw_headings_and_nextline",
        lambda _lines: (headings, line_numbers, [""] * len(headings)),
    )
    monkeypatch.setattr(
        ai_base, "extract_headings", lambda _lines, **_kwargs: (headings, line_numbers)
    )

    def raw_json(prompt, **kwargs):
        raw_calls.append(kwargs)
        return json.dumps({
            "headings before cause of action": "FACTS (0)",
            "first heading of cause of action": "COUNT I (5)",
        })

    def parsed(prompt, response_model, **kwargs):
        parsed_calls.append((response_model, kwargs))
        return response_model.model_validate({
            "section headings": "COUNT I (5)",
            "first heading of next section": "PRAYER (10)",
        })

    monkeypatch.setattr(ai_base, "chat_completion_j", raw_json)
    monkeypatch.setattr(ai_base, "chat_completion_parse", parsed)
    assert ai_base.get_headers_before_coa(["line"] * 12) == [0, 5]
    assert ai_base.get_coa_loc(["line"] * 12) == [5, 10]
    assert raw_calls == [{}]
    assert parsed_calls == [
        (ai_base.HeadingSectionResult, {
            "model": "gpt-6.1-sol", "reasoning_effort": None,
        }),
    ]


def test_cause_boundary_helper_preserves_multiple_counts(monkeypatch):
    monkeypatch.setattr(
        ai_base,
        "extract_headings",
        lambda _lines, **_kwargs: (
            ["FACTS", "COUNT I", "COUNT II", "PRAYER", "OTHER"],
            [0, 5, 10, 15, 17],
        ),
    )
    calls = []

    def parsed(prompt, response_model, **kwargs):
        calls.append((prompt, response_model, kwargs))
        return response_model.model_validate({
            "section headings": "COUNT I (5); COUNT II (10)",
            "first heading of next section": "PRAYER (15)",
        })

    monkeypatch.setattr(ai_base, "chat_completion_parse", parsed)
    assert ai_base.get_coa_loc(
        ["line"] * 18, model="gpt-6.1-sol", reasoning_effort="medium"
    ) == [5, 10, 15]
    assert len(calls) == 1
    assert calls[0][1] is ai_base.HeadingSectionResult
    assert calls[0][2] == {"model": "gpt-6.1-sol", "reasoning_effort": "medium"}


@pytest.mark.parametrize(
    ("heading", "following_lines", "expected_title", "summary_missing"),
    [
        ("COUNT II: BREACH OF CONTRACT", (), "COUNT II: BREACH OF CONTRACT", False),
        ("COUNT II", ("BREACH OF CONTRACT",), "COUNT II BREACH OF CONTRACT", False),
        (
            "SECOND CAUSE OF ACTION",
            (
                "Violation of California Uniform Trade Secret Act, Cal. Civ. Code § 3426 et seq.",
                "(Against All Defendants)",
            ),
            "SECOND CAUSE OF ACTION Violation of California Uniform Trade Secret Act, Cal. Civ. Code § 3426 et seq. (Against All Defendants)",
            False,
        ),
        ("BREACH OF CONTRACT", (), "BREACH OF CONTRACT", False),
        ("BREACH OF CONTRACT", ("(Against Defendant A)",), "BREACH OF CONTRACT (Against Defendant A)", False),
        ("COUNT III: OTHER CLAIM", (), "COUNT III: OTHER CLAIM", True),
    ],
)
def test_upload_analysis_uses_sol_for_cause_boundaries_and_luna_for_other_workloads(
    monkeypatch, e2e_database, heading, following_lines, expected_title, summary_missing
):
    app, draft = e2e_database
    requests = []
    lines = [f"Line {index}" for index in range(12)]
    lines[5] = heading
    for offset, following_line in enumerate(following_lines, start=6):
        lines[offset] = following_line

    def parse(**kwargs):
        requests.append(("parse", kwargs))
        prompt = kwargs["input"][-1]["content"]
        if '"section headings"' in prompt:
            parsed = ai_base.HeadingSectionResult.model_validate({
                "section headings": f"{heading} (5)",
                "first heading of next section": "PRAYER (10)",
            })
        elif "identify the exact name or title" in prompt:
            parsed = None if summary_missing else element_checker.CauseOfActionSummary.model_validate({
                "cause of action": expected_title, "description": ["Claim summary."],
            })
        else:
            parsed = ai_base.PartiesVenueResult(
                plaintiff=["Plaintiff"], defendant=["Defendant"], venue="California"
            )
        return _response(
            kwargs["model"],
            parsed=parsed,
        )

    def create(**kwargs):
        requests.append(("create", kwargs))
        prompt = kwargs["input"][-1]["content"]
        if "headings before cause of action" in prompt:
            output = json.dumps({
                "headings before cause of action": "FACTS (0)",
                "first heading of cause of action": f"{heading} (5)",
            })
        elif "Extract the key facts" in prompt:
            output = json.dumps({"summary": ["Fact (Compl. ¶ 1)."]})
        elif "You are a legal text summarizer" in prompt:
            output = "Consolidated facts."
        else:
            raise AssertionError(f"Unexpected Element Checker prompt: {prompt[:100]}")
        return _response(kwargs["model"], text=output)

    monkeypatch.setattr(
        ai_base,
        "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(parse=parse, create=create)),
    )
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *a, **kw: None)
    monkeypatch.setattr(
        element_checker,
        "get_complaint_text_with_line_metadata_from_file",
        lambda _path: ("Document", None, None, {}),
    )
    monkeypatch.setattr(element_checker, "get_last_ocr_warning", lambda: None)
    monkeypatch.setattr(
        element_checker,
        "prepare_complaint_text_for_memory",
        lambda *_args, **_kwargs: ("Cover text", lines, {}),
    )
    monkeypatch.setattr(
        element_checker, "log_complaint_text_metadata_preview", lambda *a: None
    )
    monkeypatch.setattr(
        ai_base,
        "extract_raw_headings_and_nextline",
        lambda _lines, **_kwargs: (
            ["FACTS", heading, "PRAYER", "A", "B", "C"],
            [0, 5, 10, 10, 10, 11],
            [""] * 6,
        ),
    )
    monkeypatch.setattr(
        ai_base,
        "extract_headings",
        lambda _lines, **_kwargs: (
            ["FACTS", heading, "PRAYER", "A", "B", "C"],
            [0, 5, 10, 10, 10, 11],
        ),
    )
    monkeypatch.setattr(
        element_checker, "split_into_chunks", lambda block, chunk_len: [block]
    )
    monkeypatch.setattr(element_checker, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_base, "sleep", lambda _seconds: None)

    with app.app_context():
        result = element_checker.state0.apply(
            args=["complaint.pdf", draft.public_id, "Defendant"], throw=True
        )
        assert result.successful()
        assert result.result["fa_short"] == "Consolidated facts."
        expected_coa = (
            "No cause of action title found.\nNo description provided\n"
            if summary_missing else f"{expected_title}\nClaim summary.\n"
        )
        assert result.result["coas"] == [expected_coa]
        assert result.result["coas"][0].split('\n', 1)[0] == (
            "No cause of action title found." if summary_missing else expected_title
        )
        memory = DBMemory(db=element_checker.db, doc_id=draft.id)
        assert memory["drafted_sections/fa_sum_final"] == "Consolidated facts."
        assert json.loads(memory["analysis/coa_sum"])["0"] == result.result["coas"][0]

    assert [kind for kind, _ in requests].count("parse") == (6 if summary_missing else 3)
    assert [kind for kind, _ in requests].count("create") == 3
    assert all(request["reasoning"] == {"effort": "medium"} for _, request in requests)
    assert sum(request.get("text_format") is ai_base.PartiesVenueResult for _, request in requests) == 1
    assert sum("text" in request for _, request in requests) == 2
    boundary_requests = [
        request for _, request in requests
        if request.get("text_format") is ai_base.HeadingSectionResult
    ]
    assert len(boundary_requests) == 1
    boundary_request = boundary_requests[0]
    assert boundary_request["model"] == "gpt-6.1-sol"
    assert "text" not in boundary_request
    assert boundary_request["input"][0] == {
        "role": "system", "content": ai_base.SYSTEM_PROMPT,
    }
    boundary_prompt = boundary_request["input"][-1]["content"]
    assert "pay attention to all COUNT or Claims" in boundary_prompt
    assert "return the original designation line and its line number as the boundary" in boundary_prompt
    assert (
        "Return the lines with the corresponding line number exactly as they were sent"
        in boundary_prompt
    )
    assert "first heading of next section" in boundary_prompt
    assert all(request["model"] == "gpt-6-luna"
               for _, request in requests if request is not boundary_request)
    summary_requests = [request for _, request in requests
                        if request.get("text_format") is element_checker.CauseOfActionSummary]
    assert len(summary_requests) == (4 if summary_missing else 1)
    assert "text" not in summary_requests[0]
    assert summary_requests[0]["model"] == "gpt-6-luna"
    summary_schema = element_checker.CauseOfActionSummary.model_json_schema(by_alias=True)
    assert set(summary_schema["required"]) == {"cause of action", "description"}
    assert summary_schema["properties"]["description"]["type"] == "array"
    summary_prompt = next(
        request["input"][-1]["content"] for _, request in requests
        if "identify the exact name or title" in request["input"][-1]["content"]
    )
    assert "include that designation exactly as written" in summary_prompt
    assert "Do not omit, change, infer, or invent a designation" in summary_prompt
    assert "include that line after the title in the same single-line 'cause of action' value" in summary_prompt
    assert "Do not add such a line if absent, and do not include numbered allegations" in summary_prompt
    expected_cause_block = "\n".join(lines[5:10])
    assert f"==\n{expected_cause_block}\n\n==" in summary_prompt


@pytest.mark.parametrize(
    ("findings", "expected"),
    [
        (
            [{"label": "incorrect", "description": "Unsupported citation."}],
            [["incorrect", "Unsupported citation."]],
        ),
        (["not a finding", ["correct", "Existing finding."]], [["correct", "Existing finding."]]),
        ([], []),
    ],
)
def test_final_review_keeps_legacy_findings_shape(
    monkeypatch, e2e_database, findings, expected
):
    app, draft = e2e_database
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            return _response(kwargs["model"], text="## Recommendations\nText.")
        return _response(kwargs["model"], text=json.dumps({"Findings": findings}))

    monkeypatch.setattr(
        ai_base,
        "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(create=create)),
    )
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *a, **kw: None)
    monkeypatch.setattr(element_checker, "create_analysis_pdf", lambda **kw: True)
    monkeypatch.setattr(
        element_checker,
        "get_text_from_pdf_fitz",
        lambda _path: ("Drafted analysis with citation.", None, None),
    )
    monkeypatch.setattr(element_checker, "num_tokens_from_string", lambda text: 5)

    with app.app_context():
        memory = DBMemory(db=element_checker.db, doc_id=draft.id)
        memory["drafted_sections/recommendation"] = "Recommendation."
        result = element_checker.state3.apply(args=[{"draftId": draft.public_id}], throw=True)
        assert result.successful()
        assert result.result["halls"] == expected
        assert json.loads(memory["hallucination_check"]) == expected

    assert len(requests) == 2
    assert all(request["model"] == "gpt-6.1-sol" for request in requests)
    assert requests[0]["reasoning"] == {"effort": "medium"}
    assert requests[1]["reasoning"] == {"effort": "high"}
    assert "text" not in requests[0]
    assert requests[1]["text"] == {"format": {"type": "json_object"}}


def test_later_analysis_recommendations_and_regeneration_use_sol_high(
    monkeypatch, e2e_database
):
    app, draft = e2e_database
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        prompt = kwargs["input"][-1]["content"]
        if "List the Required Legal Elements" in prompt:
            output = json.dumps({
                "legal_elements": [{"element": "Duty", "source": []}]
            })
        elif "Evaluate each and every legal element" in prompt:
            output = json.dumps({"element_analysis": [{
                "element": "Duty", "element_id": 1, "status": "Met",
                "complaint_references": "Compl. ¶ 1", "weakness": "",
                "defense": "", "citations": [],
            }]})
        elif "Output recommendation paragraphs" in prompt:
            output = json.dumps({
                "recommendations": ["Duty appears sufficiently pleaded."]
            })
        elif "Markdown Formatting Rules" in prompt:
            output = "## Duty\nDuty appears sufficiently pleaded."
        elif "provide additional legal citations" in prompt:
            output = json.dumps({"citations": []})
        else:
            raise AssertionError(f"Unexpected later-step prompt: {prompt[:100]}")
        return _response(kwargs["model"], text=output)

    monkeypatch.setattr(
        ai_base,
        "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(create=create)),
    )
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *a, **kw: None)
    monkeypatch.setattr(element_checker, "sleep", lambda _seconds: None)
    monkeypatch.setattr(element_checker, "create_analysis_pdf", lambda **kw: True)

    with app.app_context():
        memory = DBMemory(db=element_checker.db, doc_id=draft.id)
        memory["complaint_text"] = json.dumps(["COUNT I", "The claim."])
        memory["coa_loc"] = "0,2"
        memory["inputs/side"] = "Defendant"
        memory["inputs/venue"] = "California"
        memory["drafted_sections/fa_sum"] = "Fact (Compl. ¶ 1)."
        memory["drafted_sections/fa_sum_final"] = "Fact."
        memory["analysis/coa_sum"] = json.dumps({"0": "COUNT I\nClaim summary.\n"})

        analyzed = element_checker.state1.apply(
            args=[{"draftId": draft.public_id}], throw=True
        )
        assert analyzed.successful()
        assert analyzed.result["el_data"][0][1]["elements"][0]["satisfied"] is True

        recommended = element_checker.state2.apply(
            args=[{"draftId": draft.public_id}], throw=True
        )
        assert recommended.successful()
        assert "Duty appears sufficiently pleaded." in memory[
            "drafted_sections/recommendation"
        ]

        regenerated = element_checker.regenerate_citations.apply(args=[{
            "draftId": draft.public_id,
            "coa_id": "0",
            "element_index": 0,
            "element_name": "Duty",
        }], throw=True)
        assert regenerated.successful()
        assert regenerated.result["message"] == "No new citations generated"

    assert len(requests) == 5
    formatting_requests = [
        request for request in requests
        if "Markdown Formatting Rules" in request["input"][-1]["content"]
    ]
    assert len(formatting_requests) == 1
    assert formatting_requests[0]["model"] == "gpt-6.1-sol"
    assert formatting_requests[0]["reasoning"] == {"effort": "medium"}
    assert all(
        request["model"] == "gpt-6.1-sol" and request["reasoning"] == {"effort": "high"}
        for request in requests if request is not formatting_requests[0]
    )
    assert sum("text" in request for request in requests) == 4


def test_xhigh_element_checker_tasks_have_ten_minute_budget():
    for task in (
        element_checker.state1,
        element_checker.state2,
        element_checker.regenerate_citations,
    ):
        assert (task.soft_time_limit, task.time_limit) == (600, 630)
    assert element_checker.state0.soft_time_limit != 600
    assert element_checker.state3.soft_time_limit != 600


@pytest.mark.parametrize("step", ["state1", "state2", "regenerate_citations"])
def test_xhigh_element_checker_steps_propagate_soft_timeout(
    monkeypatch, e2e_database, step
):
    app, draft = e2e_database

    def timed_out(*_args, **_kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(element_checker, "chat_completion_j", timed_out)
    monkeypatch.setattr(element_checker, "sleep", lambda _seconds: None)

    with app.app_context():
        memory = DBMemory(db=element_checker.db, doc_id=draft.id)
        memory["complaint_text"] = json.dumps(["COUNT I", "The claim."])
        memory["coa_loc"] = "0,2"
        memory["inputs/side"] = "Defendant"
        memory["inputs/venue"] = "California"
        memory["drafted_sections/fa_sum"] = "Fact (Compl. ¶ 1)."
        memory["analysis/coa_sum"] = json.dumps({"0": "COUNT I\nClaim summary.\n"})
        memory["analysis/element_analysis"] = json.dumps({
            "0": {
                "title": "COUNT I",
                "description": ["Claim summary."],
                "elements": [{
                    "element_id": 1,
                    "name": "Duty",
                    "source": [],
                    "satisfied": True,
                }],
            },
        })

        if step == "regenerate_citations":
            payload = {
                "draftId": draft.public_id,
                "coa_id": "0",
                "element_index": 0,
                "element_name": "Duty",
            }
        else:
            payload = {"draftId": draft.public_id}

        with pytest.raises(SoftTimeLimitExceeded):
            getattr(element_checker, step).apply(args=[payload], throw=True)
