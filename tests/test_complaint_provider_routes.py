"""Complaint Drafter's typed Responses and legacy application contracts."""

import io
import inspect
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openai.lib._pydantic import to_strict_json_schema

from backend import complaint_drafter
from backend.ai import base as ai_base, llm_call_j
from backend.ai.complaint_schemas import (
    ComplaintCauseSuggestions,
    ComplaintCitation,
    ComplaintDraftedCauses,
    ComplaintFactualAllegations,
    ComplaintFindings,
    ComplaintIntroduction,
    ComplaintPartiesVenue,
    ComplaintRegeneratedCitations,
    ComplaintReliefSuggestions,
)
from backend.util.analytics import capture_guided_llm_usage


@pytest.fixture
def provider(monkeypatch):
    parse = Mock()
    create = Mock()
    monkeypatch.setattr(
        ai_base,
        "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(parse=parse, create=create)),
    )
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *a, **kw: None)
    monkeypatch.setattr(ai_base, "sleep", lambda seconds: None)
    return SimpleNamespace(parse=parse, create=create)


def _provider_result(model, schema, payload):
    return SimpleNamespace(
        id="complaint_provider_test",
        model=model,
        status="completed",
        output_parsed=schema.model_validate(payload) if payload is not None else None,
        output_text="",
        output=[],
        usage=SimpleNamespace(total_tokens=17),
    )


@pytest.mark.parametrize(
    ("schema_name", "schema", "payload"),
    [
        ("complaint_facts", ComplaintFactualAllegations, {"Factual Allegations": "Facts."}),
        ("complaint_relief", ComplaintReliefSuggestions, {"Suggested Prayers for Relief": ["Damages."]}),
        ("complaint_introduction", ComplaintIntroduction, {"Introduction": "Intro."}),
        (
            "complaint_causes", ComplaintCauseSuggestions,
            {"Suggested Causes of Action": [{
                "name": "Breach of Contract", "description": "Agreement breached.",
                "citations": [{"case_name": "", "citation": "Cal. Civ. Code § 1632", "parenthetical": "(contracts)"}],
            }]},
        ),
        (
            "complaint_parties_venue", ComplaintPartiesVenue,
            {"Parties": ["Plaintiff A.", "Defendant B."], "Jurisdiction": "Federal question.", "Venue": "This district."},
        ),
        (None, ComplaintDraftedCauses, {"Causes of Action": ["FIRST CAUSE OF ACTION"]}),
        (None, ComplaintFindings, {"Findings": [{"label": "incorrect", "description": "Bad citation."}]}),
        (
            None, ComplaintRegeneratedCitations,
            {"citations": [{"case_name": "", "citation": "35 U.S.C. § 271", "parenthetical": "(infringement)"}]},
        ),
    ],
)
def test_complaint_json_calls_use_sol_high(
    provider, schema_name, schema, payload
):
    provider.parse.side_effect = lambda **kwargs: _provider_result(
        kwargs["model"], kwargs["text_format"], payload
    )

    expected_model = "gpt-6.1-sol"

    with capture_guided_llm_usage() as usage:
        if schema_name is None:
            result = complaint_drafter._parse_complaint_output(
                "Prompt", schema,
                model=complaint_drafter.COMPLAINT_WRITING_MODEL,
                reasoning_effort=complaint_drafter.COMPLAINT_WRITING_EFFORT,
            )
        else:
            result = llm_call_j._completion("Prompt", schema_name)

    assert result == schema.model_validate(payload).model_dump(by_alias=True)
    request = provider.parse.call_args.kwargs
    assert provider.parse.call_count == 1
    assert request["model"] == expected_model
    assert request["reasoning"] == {"effort": "high"}
    assert request["text_format"] is schema
    assert request["input"][-1] == {"role": "user", "content": "Prompt"}
    assert "tools" not in request and "text" not in request
    provider.create.assert_not_called()
    assert usage == [{"model": expected_model, "successful": True, "totalTokens": 17}]
    assert to_strict_json_schema(schema)["additionalProperties"] is False


def test_final_complaint_findings_audit_explicitly_requests_high(provider, monkeypatch):
    """The final audit is substantive review, not the base's medium formatting route."""
    class Memory(dict):
        user = SimpleNamespace(contact_info="Attorney")

    memory = Memory({"drafted_sections/causes": "FIRST CAUSE OF ACTION"})
    monkeypatch.setattr(complaint_drafter.Drafts, "resolve_id", lambda _draft_id: 42)
    monkeypatch.setattr(complaint_drafter.Drafts, "pdf_filename", lambda _draft_id: "complaint.pdf")
    monkeypatch.setattr(complaint_drafter, "DBMemory", lambda **kwargs: memory)
    monkeypatch.setattr(complaint_drafter, "create_complaint_pdf", lambda *args, **kwargs: (True, "", 4))
    monkeypatch.setattr(complaint_drafter, "get_text_from_pdf_fitz", lambda _path: ("Drafted complaint.", None, None))
    monkeypatch.setattr(complaint_drafter, "num_tokens_from_string", lambda _text: 5)
    provider.parse.side_effect = lambda **kwargs: _provider_result(
        kwargs["model"], kwargs["text_format"], {"Findings": []}
    )

    result = inspect.unwrap(complaint_drafter.state3.run)({
        "draftId": "draft-id", "useIntro": "no",
    })

    assert result["halls"] == []
    assert json.loads(memory["hallucination_check"]) == []
    request = provider.parse.call_args.kwargs
    assert request["model"] == "gpt-6.1-sol"
    assert request["reasoning"] == {"effort": "high"}
    assert request["text_format"] is ComplaintFindings
    assert "Drafted complaint." in request["input"][-1]["content"]


def test_subprocess_schema_envelope_and_legacy_raw_mode(provider, monkeypatch, capsys):
    provider.parse.return_value = _provider_result(
        "gpt-6.1-sol", ComplaintFactualAllegations, {"Factual Allegations": "Facts."}
    )
    monkeypatch.setattr(
        sys, "argv",
        ["llm_call_j.py", "--analytics-envelope", "--schema", "complaint_facts", "Prompt"],
    )
    runpy.run_path("backend/ai/llm_call_j.py", run_name="__main__")
    envelope = json.loads(capsys.readouterr().out)
    assert envelope == {
        "output": {"Factual Allegations": "Facts."},
        "usage": [{"model": "gpt-6.1-sol", "successful": True, "totalTokens": 17}],
    }

    monkeypatch.setattr(ai_base, "chat_completion_j", lambda prompt: '{"legacy": true}')
    monkeypatch.setattr(
        sys, "argv", ["llm_call_j.py", "--analytics-envelope", "Legacy prompt"]
    )
    runpy.run_path("backend/ai/llm_call_j.py", run_name="__main__")
    assert json.loads(capsys.readouterr().out) == {
        "output": '{"legacy": true}', "usage": []
    }

    monkeypatch.setattr(llm_call_j, "chat_completion_j", lambda prompt: '{"legacy": true}')
    assert llm_call_j._completion("Legacy prompt", None) == '{"legacy": true}'
    with pytest.raises(ValueError, match="Unknown LLM output schema"):
        llm_call_j._completion("Prompt", "unlisted_schema")


def test_step0_launcher_and_parent_usage_replay(monkeypatch):
    launched = []
    monkeypatch.setenv("PYTHONPATH", "")
    monkeypatch.setattr(
        complaint_drafter.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append((command, kwargs)) or object(),
    )
    complaint_drafter.launch_llm_call_process_j("Prompt", "complaint_facts")
    assert launched[0][0][-5:] == [
        "backend/ai/llm_call_j.py", "--analytics-envelope", "--schema", "complaint_facts", "Prompt"
    ]

    replayed = []
    monkeypatch.setattr(complaint_drafter, "replay_guided_llm_usage", replayed.append)
    usage = [{"model": "gpt-6.1-sol", "successful": True, "totalTokens": 17}]
    process = SimpleNamespace(stdout=io.BytesIO(json.dumps({
        "output": {"Factual Allegations": "Facts."}, "usage": usage,
    }).encode()))
    assert complaint_drafter._read_llm_process_output(process) == {"Factual Allegations": "Facts."}
    assert replayed == [usage]


def test_legacy_cause_shape_and_missing_parse_fallback(provider):
    suggestion = {"name": "Contract", "description": "Breach.", "citations": [
        ComplaintCitation(case_name="", citation="Cal. Civ. Code § 1632", parenthetical="(contracts)").model_dump()
    ]}
    assert complaint_drafter._legacy_cause_suggestions([suggestion]) == [
        {"Contract": {"description": "Breach.", "citations": suggestion["citations"]}}
    ]

    provider.parse.return_value = _provider_result("gpt-6.1-sol", ComplaintFindings, None)
    assert complaint_drafter._parse_complaint_output("Prompt", ComplaintFindings) is None
    assert provider.parse.call_count == ai_base.CHAT_COMPLETION_MAX_ATTEMPTS

    process = SimpleNamespace(stdout=io.BytesIO(json.dumps({"output": None, "usage": []}).encode()))
    assert complaint_drafter._read_llm_process_output(process) == ""
    assert complaint_drafter._legacy_cause_suggestions([]) == []


def _render_prompt_example(relative_path, marker, *args, **kwargs):
    template = (Path(__file__).resolve().parents[1] / relative_path).read_text()
    rendered = template.format(*args, **kwargs)
    example = rendered.rsplit(marker, 1)[1]
    return json.loads(example[example.index("{"):])


def test_complaint_cause_suggestion_example_preserves_original_wording():
    example = _render_prompt_example(
        "backend/ai/prompts/complaint/SUGGEST_COA",
        "The output should be a JSON with the following format.",
        venue="California",
        facts_and_parties="Case facts",
    )
    assert ComplaintCauseSuggestions.model_validate(example)
    assert example == {"Suggested Causes of Action": [
        {
            "name": "Breach of Contract",
            "description": "In order to create an account a user by abide by the TOS, the defendant created an account then breached the terms by using unauthorized means to collect data.",
            "citations": [{"case_name": "...", "citation": "....", "parenthetical": "(.....)"}],
        },
        {
            "name": "California Penal Code § 502",
            "description": "Defendant's access and use of Meta's computers was without permission because the defendant accessed the computers after correspondence was sent to the Defendant revoking access.",
            "citations": [{"case_name": "...", "citation": "....", "parenthetical": "(.....)"}],
        },
    ]}


def test_complaint_drafted_cause_example_preserves_original_wording():
    example = _render_prompt_example(
        "backend/ai/prompts/complaint/DRAFT_COA",
        "== Output Using the following JSON format:",
        "California", "Case facts", "Cause list", 2,
    )
    assert ComplaintDraftedCauses.model_validate(example)
    assert example == {"Causes of Action": [
        "**FIRST CAUSE OF ACTION**\n**(Trade Secret Misappropriation under 18 U.S.C. § 1836)**\n**(Against All Defendants)**\nPlaintiff incorporates and realleges paragraphs .....\n......",
        "**SECOND CAUSE OF ACTION**\n**(Patent Infringement Under 35 U.S.C. § 271 for U.S. Patent No. 8,836,922)**\n**(Plaintiffs ABC and XYZ Against Defendant 123 Only)**\n.........",
    ]}


def test_complaint_citation_finding_example_preserves_original_wording():
    example = _render_prompt_example(
        "backend/ai/prompts/answer/HALLUCINATION_GUARD",
        "The output should be a JSON with the following format:",
        text="Complaint text",
    )
    assert ComplaintFindings.model_validate(example)
    assert example == {"Findings": [
        {
            "label": "correct",
            "description": "The use of '28 U.S.C. § 1331' (paragraph 5) is appropriate as it refers to federal courts' jurisdiction to hear lawsuits arising under the laws of the United States.",
        },
        {
            "label": "incorrect",
            "description": "The citation '17 U.S.C. § 101 et seq.' (page 3) in the context of contributory copyright infringement isn't precisely accurate. Section 101 doesn't cover contributory infringement; it simply provides definitions for the Act.",
        },
    ]}
