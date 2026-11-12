"""RFP Response typed writing batches and jurisdiction-sensitive prompts."""

import json
import re
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from backend import rfp_response_drafter as drafter
from backend.ai import base as ai_base
from backend.util import analytics


def _setup(monkeypatch, memory):
    from backend.util import account_access
    monkeypatch.setattr(account_access, 'chat_account_id', lambda _id: 1)
    monkeypatch.setattr(account_access, 'account_execution', lambda _id: nullcontext())
    monkeypatch.setattr(analytics, "start_step_analytics", lambda *_args: None)
    monkeypatch.setattr(drafter.Drafts, "resolve_id", lambda _draft_id: 42)
    monkeypatch.setattr(drafter.Drafts, "pdf_filename", lambda _draft_id: "rfp.pdf")
    monkeypatch.setattr(drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(drafter, "draft_full_rfp_response", lambda *_args: None)


def _request_memory(count, venue=""):
    return {
        "body_text": json.dumps([f"REQUEST NO. {i + 1}: Produce file {i + 1}." for i in range(count)]),
        "inputs/requests_start_end_i": f"0,{count}",
        "inputs/side": "defendant",
        "inputs/plaintiff": "Alice",
        "inputs/defendant": "Bob",
        "inputs/venue": venue,
    }


def _requests(count):
    return {"requests": [
        {"request_id": f"REQUEST NO. {i + 1}", "request": f"Produce file {i + 1}."}
        for i in range(count)
    ]}


def _run_state1():
    return drafter.state1.run({
        "draftId": "draft", "introBody": "Intro", "generalBody": "General",
        "objectionBody": "General objection",
    })


def _capture_worker_limits(monkeypatch):
    limits = []
    executor = drafter.concurrent.futures.ThreadPoolExecutor

    def tracked_executor(*args, **kwargs):
        limits.append(kwargs.get("max_workers"))
        return executor(*args, **kwargs)

    monkeypatch.setattr(drafter.concurrent.futures, "ThreadPoolExecutor", tracked_executor)
    return limits


def test_setup_keeps_extraction_medium_and_writes_definition_objections_high(monkeypatch):
    memory = {
        "cover_text": "Alice v. Bob; Superior Court",
        "body_text": json.dumps([
            "DEFINITIONS", "Documents means records.", "INSTRUCTIONS", "Identify withheld records.",
            "REQUEST NO. 1", "Produce contracts.", "Produce attachments.", "REQUEST NO. 2",
            "Produce invoices.", "Produce related records.", "SIGNATURE", "Counsel",
        ]),
        "inputs/uploaded_filename": "rfp.pdf",
        "inputs/side": "defendant",
    }
    calls = []
    monkeypatch.setattr(ai_base, "extract_headings", lambda _lines: (
        ["DEFINITIONS", "INSTRUCTIONS", "REQUEST NO. 1", "REQUEST NO. 2", "SIGNATURE"],
        [0, 2, 4, 7, 10],
    ))

    def response(**values):
        return SimpleNamespace(
            id="rfp-setup-response", status="completed", usage=None, **values,
        )

    class Responses:
        def parse(self, **request):
            calls.append(("parse", request))
            schema = request["text_format"]
            if schema is ai_base.PartiesVenueResult:
                parsed = schema(plaintiff=["Alice"], defendant=["Bob"], venue="Superior Court")
            else:
                assert schema is ai_base.HeadingSectionResult
                parsed = schema.model_validate({
                    "section headings": "REQUEST NO. 1 (4)",
                    "first heading of next section": "SIGNATURE (10)",
                })
            return response(output_parsed=parsed)

        def create(self, **request):
            calls.append(("create", request))
            section = "definition" if '"definition"' in request["input"][1]["content"] else "instruction"
            return response(output_text=json.dumps({f"{section}s": [{
                section: "Original source item", "objection": f"{section} objection.",
            }]}))

    monkeypatch.setattr(ai_base, "get_openai_client", lambda: SimpleNamespace(responses=Responses()))

    result = drafter.generate_rfp_response_setup_from_memory.__wrapped__(memory, "draft")

    assert result["message"] == "Form processed successfully"
    assert result["definition_objections"] == "definition objection.\n"
    assert result["instruction_objections"] == "instruction objection.\n"
    assert memory["inputs/requests_start_end_i"] == "4,10"
    assert [(kind, request["model"], request["reasoning"]) for kind, request in calls] == [
        ("parse", "gpt-6-luna", {"effort": "medium"}),
        ("parse", "gpt-6.1-sol", {"effort": "medium"}),
        ("create", "gpt-6.1-sol", {"effort": "high"}),
        ("create", "gpt-6.1-sol", {"effort": "high"}),
    ]


@pytest.mark.parametrize("count, expected_calls", [(30, 1), (31, 2), (100, 4)])
def test_suggestion_batches_cover_every_request_with_four_worker_cap(
    monkeypatch, count, expected_calls,
):
    memory = _request_memory(count)
    _setup(monkeypatch, memory)
    monkeypatch.setattr(drafter, "chat_completion_j_list_requests", lambda *_a, **_k: _requests(count))
    limits = _capture_worker_limits(monkeypatch)
    batches = []

    def parse(prompt, schema, **kwargs):
        assert schema is drafter.RFPObjectionBatch
        assert kwargs == {"model": "gpt-6.1-sol", "reasoning_effort": "high"}
        indices = [int(index) for index in re.findall(r"index_id: (\d+),", prompt)]
        batches.append(indices)
        return drafter.RFPObjectionBatch(objections=[
            drafter.RFPObjection(index_id=index, reason=f"Reason {index}", objection=f"Objection {index}")
            for index in indices
        ])

    monkeypatch.setattr(drafter, "chat_completion_parse", parse)
    result = _run_state1()

    assert len(batches) == expected_calls
    assert sorted(index for batch in batches for index in batch) == list(range(count))
    assert all(len(batch) <= 30 for batch in batches)
    assert limits == [4]
    assert [item["objection"] for item in result["rfp_response"]] == [
        f"Objection {index}" for index in range(count)
    ]


@pytest.mark.parametrize("count, expected_calls", [(30, 1), (31, 2), (100, 4)])
def test_rewrite_batches_cover_every_request_with_four_worker_cap(
    monkeypatch, count, expected_calls,
):
    memory = _request_memory(count)
    memory["drafted_sections/requests"] = json.dumps([{
        "request_id": f"REQUEST NO. {index + 1}", "request": f"Produce file {index + 1}.",
        "objection": "Scope objection.", "selection": "No Documents",
    } for index in range(count)])
    _setup(monkeypatch, memory)
    limits = _capture_worker_limits(monkeypatch)
    batches = []

    def parse(prompt, schema, **kwargs):
        assert schema is drafter.RFPResponseBatch
        assert kwargs == {"model": "gpt-6.1-sol", "reasoning_effort": "high"}
        indices = [int(index) for index in re.findall(r"index_id:(\d+), Request:", prompt)]
        batches.append(indices)
        return drafter.RFPResponseBatch(responses=[
            drafter.RFPResponse(index_id=index, response=f"Response {index}")
            for index in indices
        ])

    monkeypatch.setattr(drafter, "chat_completion_parse", parse)
    drafter.state2.run({"draftId": "draft"})

    assert len(batches) == expected_calls
    assert sorted(index for batch in batches for index in batch) == list(range(count))
    assert all(len(batch) <= 30 for batch in batches)
    assert limits == [4]
    assert [int(index) for index in re.findall(
        r"(?m)^Response (\d+)$", memory["drafted_sections/rfp_response_text"],
    )] == list(range(count))


@pytest.mark.parametrize("venue,uses_federal_rule", [
    ("United States District Court for the District of Columbia", True),
    ("U.S. District Court, Northern District of California", True),
    ("Superior Court of California, County of Los Angeles", False),
    ("Northern District of California", False),
    ("", False),
])
def test_suggestion_uses_saved_venue_without_assuming_federal_rules(
    monkeypatch, venue, uses_federal_rule,
):
    memory = _request_memory(1, venue)
    _setup(monkeypatch, memory)
    monkeypatch.setattr(drafter, "chat_completion_j_list_requests", lambda *_a, **_k: _requests(1))
    calls = []

    def parse(prompt, schema, **kwargs):
        calls.append((prompt, schema, kwargs))
        return drafter.RFPObjectionBatch(objections=[])

    monkeypatch.setattr(drafter, "chat_completion_parse", parse)
    result = _run_state1()

    assert result["rfp_response"][0]["objection"] == ""
    assert len(calls) == 1
    prompt, schema, kwargs = calls[0]
    assert f"Venue: {venue}" in prompt
    assert ("FRCP 26(b)(1)" in prompt) is uses_federal_rule
    assert schema is drafter.RFPObjectionBatch
    assert kwargs == {"model": "gpt-6.1-sol", "reasoning_effort": "high"}


def test_suggestion_keeps_successful_batch_and_skips_invalid_indices(monkeypatch):
    memory = _request_memory(31, "State Court")
    _setup(monkeypatch, memory)
    monkeypatch.setattr(drafter, "chat_completion_j_list_requests", lambda *_a, **_k: _requests(31))
    calls = []

    def parse(prompt, schema, **kwargs):
        calls.append(prompt)
        assert schema is drafter.RFPObjectionBatch
        assert kwargs == {"model": "gpt-6.1-sol", "reasoning_effort": "high"}
        if "index_id: 30," in prompt:
            return None
        return drafter.RFPObjectionBatch(objections=[
            drafter.RFPObjection(index_id=0, reason="Overbroad", objection="Objection 1"),
            drafter.RFPObjection(index_id=-1, reason="Bad", objection="Bad"),
            drafter.RFPObjection(index_id=31, reason="Bad", objection="Bad"),
        ])

    monkeypatch.setattr(drafter, "chat_completion_parse", parse)
    result = _run_state1()

    assert len(calls) == 2
    assert len(result["rfp_response"]) == 31
    assert result["rfp_response"][0]["objection"] == "Objection 1"
    assert result["rfp_response"][0]["reason"] == "Overbroad"
    assert result["rfp_response"][30]["objection"] == ""
    assert json.loads(memory["drafted_sections/requests"]) == result["rfp_response"]


def test_rewrite_keeps_successful_batch_and_skips_invalid_indices(monkeypatch):
    requests = [{
        "request_id": f"REQUEST NO. {i + 1}", "request": f"Produce file {i + 1}.",
        "objection": "Scope objection.", "selection": "No Documents",
    } for i in range(31)]
    memory = _request_memory(31)
    memory["drafted_sections/requests"] = json.dumps(requests)
    _setup(monkeypatch, memory)
    calls = []

    def parse(prompt, schema, **kwargs):
        calls.append(prompt)
        assert schema is drafter.RFPResponseBatch
        assert kwargs == {"model": "gpt-6.1-sol", "reasoning_effort": "high"}
        if "index_id:30," in prompt:
            return None
        return drafter.RFPResponseBatch(responses=[
            drafter.RFPResponse(index_id=0, response="Produce the file."),
            drafter.RFPResponse(index_id=-1, response="Bad"),
            drafter.RFPResponse(index_id=31, response="Bad"),
        ])

    monkeypatch.setattr(drafter, "chat_completion_parse", parse)
    result = drafter.state2.run({
        "draftId": "draft", "req-0": "Full Production", "req-1": "No Documents",
        "req-2": "Custom", "custom-2": "Produce only the final version.",
    })

    assert len(calls) == 2
    assert "Lawyer's response:Full Production" in calls[0]
    assert "Lawyer's response:No Documents" in calls[0]
    assert "Lawyer's response:Produce only the final version." in calls[0]
    assert "Produce the file." in memory["drafted_sections/rfp_response_text"]
    assert "Bad" not in memory["drafted_sections/rfp_response_text"]
    assert result["file_name"].endswith("rfp.pdf")


@pytest.mark.parametrize("step", ["suggestion", "rewrite"])
def test_soft_timeout_is_not_swallowed_or_saved_as_success(monkeypatch, step):
    memory = _request_memory(1)
    _setup(monkeypatch, memory)
    if step == "suggestion":
        monkeypatch.setattr(drafter, "chat_completion_j_list_requests", lambda *_a, **_k: _requests(1))
    else:
        memory["drafted_sections/requests"] = json.dumps([{
            "request_id": "REQUEST NO. 1", "request": "Produce file.",
            "objection": "Objection.", "selection": "No Documents",
        }])

    def timeout(*_args, **_kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(drafter, "chat_completion_parse", timeout)
    with pytest.raises(SoftTimeLimitExceeded):
        if step == "suggestion":
            _run_state1()
        else:
            drafter.state2.run({"draftId": "draft", "req-0": "No Documents"})
    assert "drafted_sections/rfp_response_text" not in memory
    if step == "suggestion":
        assert "drafted_sections/requests" not in memory


def test_typed_examples_are_valid_json_and_match_the_ui_labels():
    prompt_dir = Path("backend/ai/prompts/rfp_response")
    suggestion = (prompt_dir / "SUGGEST_RESPONSE").read_text().format(
        side="defendant", side_b="plaintiff", side_name="Bob", side_b_name="Alice",
        venue="State Court", side_dep_inst="Strategy", summary="", requests="RFP",
        proportionality_guidance="Assess burden.",
    )
    rewrite = (prompt_dir / "REWRITE_RESPONSE").read_text().format(
        side="defendant", side_b="plaintiff", side_name="Bob", side_b_name="Alice",
        rfp_responses="RFP",
    )
    suggestion_example = json.loads(suggestion.split("Output the above required fields in a list with the following JSON format\n")[1])
    rewrite_example = json.loads(rewrite.split("5. Output using the following JSON format:\n")[1])

    assert suggestion_example["objections"][0] == {
        "index_id": 0, "reason": ".......",
        "objection": ".......",
    }
    assert rewrite_example["responses"][0]["index_id"] == 0
    assert rewrite_example["responses"][0]["response"] == "........"
    assert "- Full Production" in rewrite
    assert "- No Documents" in rewrite
    assert "- Custom (" in rewrite
    assert "- Customize (" not in rewrite


def test_writing_steps_have_twenty_minute_task_budget():
    for task in (drafter.state1, drafter.state2):
        assert (task.soft_time_limit, task.time_limit) == (1200, 1260)
