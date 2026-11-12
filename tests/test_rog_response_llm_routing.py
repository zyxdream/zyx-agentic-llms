"""ROG Response provider routes and typed-output contracts."""

import json
import re
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from backend import rfa_drafter, rfp_drafter, rfp_response_drafter, rog_drafter
from backend import rog_response_drafter as rog_response
from backend.ai import base as ai_base
from backend.util.data_tools import invert_side


@pytest.fixture(autouse=True)
def guided_owner_boundary(monkeypatch):
    # Provider-contract tests use dict memory; authorization has real DB tests.
    from backend.util import account_access
    monkeypatch.setattr(account_access, 'chat_account_id', lambda _id: 1)
    monkeypatch.setattr(account_access, 'account_execution', lambda _id: nullcontext())


def test_typed_batch_prompt_examples_remain_valid_json():
    prompt_dir = Path("backend/ai/prompts/rog_response")
    values = {
        "side": "defendant", "side_b": "plaintiff",
        "side_name": "Bob", "side_b_name": "Alice",
        "side_dep_inst": "Strategy", "interrogatories": "Interrogatory",
        "rog_responses": "Lawyer notes",
    }
    suggestion = (prompt_dir / "SUGGEST_RESPONSE").read_text().format(**values)
    rewrite = (prompt_dir / "REWRITE_RESPONSE").read_text().format(**values)
    suggestion_example = json.loads(suggestion.split(
        "Output the above required fields in a list using the following JSON format. "
        "ALWAYS output complete answer, DO NOT output partial answer for brevity. \n", 1,
    )[1])
    rewrite_example = json.loads(rewrite.split("3. Output using the following JSON format:\n", 1)[1])
    assert [item["index_id"] for item in suggestion_example["objections"]] == [0, 1]
    assert [item["index_id"] for item in rewrite_example["objections_and_responses"]] == [0, 1]


def _provider_response(*, model, parsed=None, text="", status="completed"):
    return SimpleNamespace(
        id="resp_rog_test",
        model=model,
        status=status,
        output_parsed=parsed,
        output_text=text,
        output=[],
        usage=SimpleNamespace(total_tokens=10),
    )


@pytest.fixture
def provider(monkeypatch):
    create = Mock()
    parse = Mock()
    monkeypatch.setattr(
        ai_base,
        "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(create=create, parse=parse)),
    )
    monkeypatch.setattr(ai_base, "sleep", lambda _delay: None)
    monkeypatch.setattr(rog_response, "sleep", lambda _delay: None)
    return SimpleNamespace(create=create, parse=parse)


def _setup_memory(side="defendant", venue="UNITED STATES DISTRICT COURT"):
    return {
        "cover_text": f"Alice v. Bob; {venue}; First Interrogatories",
        "body_text": json.dumps([
            "INTERROGATORY NO. 3: Identify witnesses.",
            "INTERROGATORY NO. 9: Identify documents.",
        ]),
        "inputs/uploaded_filename": "interrogatories.pdf",
        "inputs/side_selection": side,
    }


@pytest.mark.parametrize("classification", ["FEDERAL", "STATE"])
def test_setup_routes_all_four_llm_calls_and_keeps_existing_output(
    provider, monkeypatch, classification
):
    memory = _setup_memory(venue="Cook County" if classification == "STATE" else "Federal Court")
    monkeypatch.setattr(rog_response, "num_tokens_from_string", lambda _prompt: 100)

    def parse_response(**request):
        schema = request["text_format"]
        if schema is ai_base.TitlePartiesVenueResult:
            parsed = schema(
                plaintiff=["Alice"], defendant=["Bob"],
                title="First Interrogatories", venue="Cook County" if classification == "STATE" else "Federal Court",
            )
        elif schema is rog_response.ROGIntroduction:
            parsed = schema(introduction="Alice responds to Bob's interrogatories.")
        else:
            assert schema is rog_response.ROGInterrogatoryList
            parsed = schema(interrogatories=[
                {"interrogatory_id": "INTERROGATORY NO. 3", "interrogatory": "Identify witnesses."},
                {"interrogatory_id": "INTERROGATORY NO. 9", "interrogatory": "Identify documents."},
            ])
        return _provider_response(model=request["model"], parsed=parsed)

    provider.parse.side_effect = parse_response
    provider.create.side_effect = lambda **request: _provider_response(
        model=request["model"], text=classification.lower()
    )

    result = rog_response.generate_rog_response_setup_from_memory.__wrapped__(
        memory, "draft-id"
    )

    assert result["message"] == "Form processed successfully"
    assert result["intro"] == "Alice responds to Bob's interrogatories."
    assert [item["interrogatory_id"] for item in result["interrogatories"]] == [
        "INTERROGATORY NO. 3\n", "INTERROGATORY NO. 9\n"
    ]
    assert result["interrogatories"][0]["objection"] == ""
    assert result["interrogatories"][0]["response"] == ""
    assert memory["inputs/is_federal"] == classification
    assert json.loads(memory["drafted_sections/interrogatories"]) == result["interrogatories"]
    assert provider.parse.call_count == 3
    assert provider.create.call_count == 1
    for call in provider.parse.call_args_list:
        request = call.kwargs
        schema = request["text_format"]
        expected_model = (
            "gpt-6.1-sol" if schema is rog_response.ROGIntroduction else "gpt-6-luna"
        )
        assert request["model"] == expected_model
        assert request["reasoning"] == {
            "effort": "high" if schema is rog_response.ROGIntroduction else "medium"
        }
        assert request["input"][0]["role"] == "system"
    intro_request = next(
        call.kwargs for call in provider.parse.call_args_list
        if call.kwargs["text_format"] is rog_response.ROGIntroduction
    )
    intro_prompt = intro_request["input"][1]["content"]
    assert f"is federal venue? {'YES' if classification == 'FEDERAL' else 'NO'}" in intro_prompt
    assert "Rule 33 of the Federal Rules of Civil Procedure" in intro_prompt
    assert "For a state venue, do not cite a federal rule" in intro_prompt
    classification_request = provider.create.call_args.kwargs
    assert classification_request["model"] == "gpt-6-luna"
    assert classification_request["reasoning"] == {"effort": "medium"}
    assert classification_request["input"][1]["content"].endswith(
        "Cook County" if classification == "STATE" else "Federal Court"
    )


@pytest.mark.parametrize(
    "failed_step, expected_message",
    [
        ("parties", "Could not extract parties"),
        ("classification_missing", "Could not determine"),
        ("classification_invalid", "Could not determine"),
        ("introduction_missing", "Could not generate"),
        ("introduction_empty", "Could not generate"),
        ("list_missing", "Could not extract interrogatories"),
        ("list_empty", "Could not extract interrogatories"),
    ],
)
def test_setup_rejects_missing_or_invalid_results(
    monkeypatch, failed_step, expected_message
):
    memory = _setup_memory()
    monkeypatch.setattr(rog_response, "num_tokens_from_string", lambda _prompt: 100)
    monkeypatch.setattr(
        rog_response, "extract_title_parties_venue",
        lambda *_args, **_kwargs: (
            ([], [], "", "") if failed_step == "parties"
            else (["Alice"], ["Bob"], "First Interrogatories", "Federal Court")
        ),
    )
    monkeypatch.setattr(
        rog_response, "chat_completion",
        lambda *_args, **_kwargs: (
            None if failed_step == "classification_missing"
            else "unknown" if failed_step == "classification_invalid" else "FEDERAL"
        ),
    )

    def parsed(_prompt, schema, **_kwargs):
        if schema is rog_response.ROGIntroduction:
            if failed_step == "introduction_missing":
                return None
            return schema(introduction="" if failed_step == "introduction_empty" else "Intro")
        assert schema is rog_response.ROGInterrogatoryList
        if failed_step == "list_missing":
            return None
        return schema(interrogatories=[])

    monkeypatch.setattr(rog_response, "chat_completion_parse", parsed)

    with pytest.raises(ValueError, match=expected_message):
        rog_response.generate_rog_response_setup_from_memory.__wrapped__(memory, "draft-id")
    assert "drafted_sections/interrogatories" not in memory


def test_continue_task_returns_setup_error_shape_and_clears_active_task(monkeypatch):
    memory = _setup_memory()
    memory[rog_response.ROG_RESPONSE_ACTIVE_CONTINUE_TASK_KEY] = "task-id"
    _patch_batch_memory(monkeypatch, memory)
    monkeypatch.setattr(
        rog_response,
        "extract_title_parties_venue",
        lambda *_args, **_kwargs: ([], [], "", ""),
    )

    result = rog_response.rog_response_continue_task.run({"draftId": "draft-id"})

    assert result["message"] == "error"
    assert "Could not extract parties" in result["error_message"]
    assert rog_response.ROG_RESPONSE_ACTIVE_CONTINUE_TASK_KEY not in memory


def _batch_memory(count=11):
    return {
        "drafted_sections/interrogatories": json.dumps([
            {
                "interrogatory_id": f"INTERROGATORY NO. {3 + index * 6}\n",
                "interrogatory": f"Question {index}\n",
                "objection": "",
                "response": "",
            }
            for index in range(count)
        ]),
        "inputs/side": "Defendant",
        "inputs/plaintiff": "Alice",
        "inputs/defendant": "Bob",
    }


def _patch_batch_memory(monkeypatch, memory):
    monkeypatch.setattr(rog_response.Drafts, "resolve_id", lambda _draft_id: 42)
    monkeypatch.setattr(rog_response, "DBMemory", lambda db, doc_id: memory)


def _capture_worker_limits(monkeypatch):
    limits = []
    executor = rog_response.concurrent.futures.ThreadPoolExecutor

    def tracked_executor(*args, **kwargs):
        limits.append(kwargs.get("max_workers"))
        return executor(*args, **kwargs)

    monkeypatch.setattr(rog_response.concurrent.futures, "ThreadPoolExecutor", tracked_executor)
    return limits


@pytest.mark.parametrize("count, expected_calls", [(30, 1), (31, 2), (100, 4)])
def test_suggestion_batch_boundaries_cover_all_interrogatories(
    provider, monkeypatch, count, expected_calls,
):
    memory = _batch_memory(count=count)
    _patch_batch_memory(monkeypatch, memory)
    limits = _capture_worker_limits(monkeypatch)
    batches = []

    def parse_response(**request):
        assert request["model"] == "gpt-6.1-sol"
        assert request["reasoning"] == {"effort": "high"}
        assert request["text_format"] is rog_response.ROGSuggestionBatch
        selected = request["input"][1]["content"].split(
            "=== Selected Interrogatories ===", 1,
        )[1].split("=== Thinking Strategies", 1)[0]
        indices = [int(index) for index in re.findall(r"index_id: (\d+),", selected)]
        batches.append(indices)
        parsed = rog_response.ROGSuggestionBatch(objections=[
            {"index_id": index, "objection": f"Objection {index}",
             "rationale": f"Rationale {index}", "tips": f"Tips {index}"}
            for index in indices
        ])
        return _provider_response(model=request["model"], parsed=parsed)

    provider.parse.side_effect = parse_response
    result = rog_response.state1.run.__wrapped__({
        "draftId": "draft-id", "introBody": "Intro", "generalBody": "General",
    })

    assert provider.parse.call_count == expected_calls
    assert sorted(index for batch in batches for index in batch) == list(range(count))
    assert all(len(batch) <= 30 for batch in batches)
    assert limits == [4]
    assert [item["objection"] for item in result["interrogatories"]] == [
        f"Objection {index}" for index in range(count)
    ]


@pytest.mark.parametrize("custom_count, expected_calls", [(0, 0), (30, 1), (31, 2), (100, 4)])
def test_rewrite_batches_only_custom_notes_with_four_worker_cap(
    provider, monkeypatch, custom_count, expected_calls,
):
    memory = _batch_memory(count=100)
    _patch_batch_memory(monkeypatch, memory)
    monkeypatch.setattr(rog_response.Drafts, "pdf_filename", lambda _draft_id: "draft.pdf")
    monkeypatch.setattr(rog_response, "draft_full_rog_response", lambda *_args: "ok")
    limits = _capture_worker_limits(monkeypatch)
    batches = []

    def parse_response(**request):
        assert request["model"] == "gpt-6.1-sol"
        assert request["reasoning"] == {"effort": "high"}
        assert request["text_format"] is rog_response.ROGRewriteBatch
        indices = [int(index) for index in re.findall(
            r"(?m)^index_id:(\d+)$", request["input"][1]["content"],
        )]
        batches.append(indices)
        parsed = rog_response.ROGRewriteBatch(objections_and_responses=[
            {"index_id": index, "objection": f"Revised {index}", "response": f"Response {index}"}
            for index in indices
        ])
        return _provider_response(model=request["model"], parsed=parsed)

    provider.parse.side_effect = parse_response
    data = {"draftId": "draft-id"}
    data.update({f"custom-{index}": f"Note {index}" for index in range(custom_count)})
    rog_response.state2.run.__wrapped__(data)

    assert provider.parse.call_count == expected_calls
    assert sorted(index for batch in batches for index in batch) == list(range(custom_count))
    assert all(len(batch) <= 30 for batch in batches)
    assert limits == [4]
    items = json.loads(memory["drafted_sections/interrogatories"])
    assert [item["response"] for item in items[:custom_count]] == [
        f"Response {index}" for index in range(custom_count)
    ]
    assert all(not item["response"] for item in items[custom_count:])


def test_suggestion_batches_use_typed_sol_high_and_order_indices(
    provider, monkeypatch
):
    memory = _batch_memory(count=31)
    _patch_batch_memory(monkeypatch, memory)

    def parse_response(**request):
        assert request["text_format"] is rog_response.ROGSuggestionBatch
        prompt = request["input"][1]["content"]
        selected = prompt.split("=== Selected Interrogatories ===", 1)[1].split(
            "=== Thinking Strategies", 1
        )[0]
        index = 30 if "index_id: 30" in selected else 0
        parsed = rog_response.ROGSuggestionBatch(objections=[
            {"index_id": index, "objection": f"Objection {index}",
             "rationale": f"Rationale {index}", "tips": f"Tips {index}"},
        ])
        return _provider_response(model=request["model"], parsed=parsed)

    provider.parse.side_effect = parse_response
    result = rog_response.state1.run.__wrapped__({
        "draftId": "draft-id", "introBody": "Intro", "generalBody": "General"
    })

    assert provider.parse.call_count == 2
    for call in provider.parse.call_args_list:
        assert call.kwargs["model"] == "gpt-6.1-sol"
        assert call.kwargs["reasoning"] == {"effort": "high"}
        assert call.kwargs["text_format"] is rog_response.ROGSuggestionBatch
    assert result["interrogatories"][0]["objection"] == "Objection 0"
    assert result["interrogatories"][30]["objection"] == "Objection 30"
    assert result["interrogatories"][30]["rationale"] == "Rationale 30"
    assert memory["drafted_sections/intro"] == "Intro"


def test_unavailable_suggestion_batch_preserves_original_items(provider, monkeypatch):
    memory = _batch_memory(count=1)
    _patch_batch_memory(monkeypatch, memory)
    provider.parse.return_value = _provider_response(model="gpt-6.1-sol", parsed=None)

    result = rog_response.state1.run.__wrapped__({
        "draftId": "draft-id", "introBody": "Intro", "generalBody": "General"
    })

    assert provider.parse.call_count == 4
    assert result["interrogatories"][0]["objection"] == ""
    assert result["interrogatories"][0]["response"] == ""


def test_rewrite_batches_use_typed_sol_high_and_preserve_contract(
    provider, monkeypatch
):
    memory = _batch_memory()
    _patch_batch_memory(monkeypatch, memory)
    monkeypatch.setattr(rog_response.Drafts, "pdf_filename", lambda _draft_id: "draft.pdf")
    export = Mock(return_value="ok")
    monkeypatch.setattr(rog_response, "draft_full_rog_response", export)

    def parse_response(**request):
        parsed = rog_response.ROGRewriteBatch(objections_and_responses=[
            {"index_id": 0, "objection": "Revised objection 0", "response": "Answer 0"},
            {"index_id": 10, "objection": "Revised objection 10", "response": "Answer 10"},
        ])
        return _provider_response(model=request["model"], parsed=parsed)

    provider.parse.side_effect = parse_response
    result = rog_response.state2.run.__wrapped__({
        "draftId": "draft-id", "custom-10": "Note ten", "custom-0": "Note zero"
    })

    assert provider.parse.call_count == 1
    request = provider.parse.call_args.kwargs
    assert request["model"] == "gpt-6.1-sol"
    assert request["reasoning"] == {"effort": "high"}
    assert request["text_format"] is rog_response.ROGRewriteBatch
    assert request["input"][1]["content"].index("index_id:0") < request["input"][1]["content"].index("index_id:10")
    items = json.loads(memory["drafted_sections/interrogatories"])
    assert items[0]["response"] == "Answer 0"
    assert items[10]["response"] == "Answer 10"
    assert "Revised objection 10" in memory["drafted_sections/rog_response_text"]
    assert result == {"file_name": f"{rog_response.API_URL}static/draft.pdf"}
    export.assert_called_once_with(memory, "backend/static/draft.pdf")


def test_unavailable_rewrite_batch_preserves_existing_text(provider, monkeypatch):
    memory = _batch_memory(count=1)
    memory["drafted_sections/interrogatories"] = json.dumps([{
        **json.loads(memory["drafted_sections/interrogatories"])[0],
        "objection": "Original objection", "response": "Original response",
    }])
    _patch_batch_memory(monkeypatch, memory)
    monkeypatch.setattr(rog_response.Drafts, "pdf_filename", lambda _draft_id: "draft.pdf")
    monkeypatch.setattr(rog_response, "draft_full_rog_response", lambda *_args: "ok")
    provider.parse.return_value = _provider_response(model="gpt-6.1-sol", parsed=None)

    rog_response.state2.run.__wrapped__({"draftId": "draft-id", "custom-0": "New note"})

    assert provider.parse.call_count == 4
    item = json.loads(memory["drafted_sections/interrogatories"])[0]
    assert item["objection"] == "Original objection"
    assert item["response"] == "Original response"


def test_response_writing_steps_keep_twenty_minute_limits():
    for task in (rog_response.state1, rog_response.state2):
        assert (task.soft_time_limit, task.time_limit) == (1200, 1260)


@pytest.mark.parametrize("step,data", [
    ("state1", {"draftId": "draft-id", "introBody": "Intro", "generalBody": "General"}),
    ("state2", {"draftId": "draft-id", "custom-0": "Lawyer note"}),
])
def test_response_writing_step_does_not_swallow_soft_timeout(monkeypatch, step, data):
    memory = _batch_memory(count=1)
    _patch_batch_memory(monkeypatch, memory)
    monkeypatch.setattr(rog_response, "sleep", lambda _delay: None)

    def time_out(*_args, **_kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(rog_response, "chat_completion_parse", time_out)
    with pytest.raises(SoftTimeLimitExceeded):
        getattr(rog_response, step).run.__wrapped__(data)


@pytest.mark.parametrize(
    "side, other_side",
    [
        ("Defendant", "plaintiff"),
        ("Defendants", "plaintiff"),
        ("defendant", "plaintiff"),
        ("Plaintiff", "defendant"),
        ("Plaintiffs", "defendant"),
    ],
)
def test_shared_opposing_side_for_guided_products(side, other_side):
    for product in (rog_drafter, rfa_drafter, rfp_drafter, rfp_response_drafter, rog_response):
        assert product.invert_side is invert_side
        assert product.invert_side(side) == other_side


@pytest.mark.parametrize("side, expected", [
    ("Defendant", True), ("Defendants", True), ("Plaintiff", False), ("Plaintiffs", False),
])
def test_rog_response_pdf_and_word_export_side_flags(monkeypatch, side, expected):
    memory = {
        "inputs/side": side,
        "inputs/plaintiff": "Alice",
        "inputs/defendant": "Bob",
    }
    class ExportMemory(dict):
        user = SimpleNamespace(contact_info="Counsel")
    memory = ExportMemory(memory)
    pdf = Mock(return_value="pdf done")
    word = Mock(return_value="word done")
    monkeypatch.setattr(rog_response, "create_rog_response_pdf", pdf)
    monkeypatch.setattr(rog_response, "create_rog_response_word", word)

    assert rog_response.draft_full_rog_response(memory, "draft.pdf") == "pdf done"
    assert rog_response.draft_full_rog_response_word(memory, "draft.docx") == "word done"
    assert pdf.call_args.kwargs["is_defendant"] is expected
    assert word.call_args.kwargs["is_defendant"] is expected
