import json
import re
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import answer_drafter
from tests.answer_parse_test_support import install_legacy_answer_json_mock


@pytest.fixture(autouse=True)
def enabled_account(monkeypatch):
    # Chunking behavior uses dictionary memory without persisted account owners.
    from backend.util import account_access
    monkeypatch.setattr(account_access, 'chat_account_id', lambda chat_id: 1)
    monkeypatch.setattr(account_access, 'account_execution', lambda user_id: nullcontext())


@pytest.fixture(autouse=True)
def _legacy_answer_json_mock(monkeypatch):
    install_legacy_answer_json_mock(monkeypatch)


class Memory(dict):
    user = SimpleNamespace(contact_info="Test contact")

    def get(self, key, default=None):
        return super().get(key, default)


def _make_lines(count: int) -> list[str]:
    return [f"{index}. Plaintiff alleges fact {index}." for index in range(1, count + 1)]


def _make_para_2_ln(count: int) -> dict[str, int]:
    return {str(index): index - 1 for index in range(1, count + 1)}


def _make_selections(count: int, choice: str = "deny") -> dict[int, dict]:
    return {
        index: {
            "field": f"para{index:04d}",
            "choice": choice,
            "custom_text": "",
            "prompt_value": choice,
        }
        for index in range(1, count + 1)
    }


def _base_memory(count: int = 4, state: str = "") -> Memory:
    return Memory({
        "complaint_text": json.dumps(_make_lines(count)),
        "paragraphs": json.dumps(_make_para_2_ln(count)),
        "outline": json.dumps(
            [["FACTUAL ALLEGATIONS", None]]
            + [[f"Paragraph {index}", index] for index in range(1, count + 1)]
        ),
        "inputs/plaintiff": "Acme Corp.",
        "inputs/defendant": "Beta LLC",
        "inputs/venue": "Test Court",
        "inputs/state": state,
    })


def _patch_state1_infrastructure(monkeypatch, memory: Memory) -> None:
    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter.Drafts, "pdf_filename", lambda draft_id: "answer-output.pdf")
    monkeypatch.setattr(answer_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(answer_drafter, "create_answer_pdf", lambda **kwargs: "ok")
    monkeypatch.setattr(answer_drafter, "sleep", lambda seconds: None)


def _answers_from_prompt(prompt: str) -> str:
    paragraphs = [
        int(match.group(1))
        for match in re.finditer(r"Paragraph(\d+):", prompt)
    ]
    return json.dumps({
        "Answers": [
            {
                "paragraph": str(paragraph),
                "response": f"Drafted response for Paragraph {paragraph}.",
            }
            for paragraph in paragraphs
        ]
    })


def test_answer_response_chunk_builder_groups_250_paragraphs_as_100_100_50(monkeypatch):
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: 1)
    selections = _make_selections(250)

    chunks = answer_drafter._build_answer_response_chunks(
        selections,
        _make_para_2_ln(250),
        _make_lines(250),
        "paragraphs:\n{paragraphs}\nchoices:\n{para_user_responses}",
        paras_per_call=100,
        max_prompt_tokens=60000,
    )

    assert [len(chunk["paragraph_numbers"]) for chunk in chunks] == [100, 100, 50]
    assert chunks[0]["paragraph_numbers"][0] == 1
    assert chunks[0]["paragraph_numbers"][-1] == 100
    assert chunks[1]["paragraph_numbers"][0] == 101
    assert chunks[2]["paragraph_numbers"][-1] == 250


def test_answer_response_runner_uses_max_8_workers_for_3000_paragraphs(monkeypatch):
    created_worker_counts = []
    real_executor = answer_drafter.concurrent.futures.ThreadPoolExecutor

    class RecordingExecutor(real_executor):
        def __init__(self, *args, **kwargs):
            created_worker_counts.append(kwargs.get("max_workers"))
            super().__init__(*args, **kwargs)

    chunks = [
        {
            "chunk_id": index + 1,
            "chunk_count": 30,
            "paragraph_numbers": list(range(index * 100 + 1, index * 100 + 101)),
            "prompt": "prompt",
            "prompt_tokens": 1,
        }
        for index in range(30)
    ]

    monkeypatch.setattr(answer_drafter.concurrent.futures, "ThreadPoolExecutor", RecordingExecutor)
    monkeypatch.setattr(
        answer_drafter,
        "_extract_answer_response_chunk",
        lambda chunk, selections, para_2_ln, text_by_lines, prompt_template: {
            "rows": {paragraph: f"Response {paragraph}" for paragraph in chunk["paragraph_numbers"]},
            "had_error": False,
        },
    )
    monkeypatch.setattr(answer_drafter, "sleep", lambda seconds: None)

    responses, had_error = answer_drafter._run_answer_response_chunks(
        chunks,
        _make_selections(3000),
        _make_para_2_ln(3000),
        _make_lines(3000),
        "prompt",
    )

    assert created_worker_counts == [8]
    assert had_error is False
    assert len(responses) == 3000


def test_answer_response_prompt_excludes_non_paragraph_fields_and_preserves_custom_text(monkeypatch):
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: 1)
    data = {
        "draftId": "draft-public-id",
        "appState": "1",
        "defendant0": "Beta LLC",
        "para0001": "deny",
        "para0002": "custom",
        "customResponse-para0002": "Admit contract date only.",
        "para0003": "admit",
    }
    selections = answer_drafter._answer_response_paragraph_selections(data)

    chunks = answer_drafter._build_answer_response_chunks(
        selections,
        _make_para_2_ln(3),
        _make_lines(3),
        "paragraphs:\n{paragraphs}\nchoices:\n{para_user_responses}",
        paras_per_call=100,
        max_prompt_tokens=60000,
    )

    assert len(chunks) == 1
    prompt = chunks[0]["prompt"]
    assert "draft-public-id" not in prompt
    assert "appState" not in prompt
    assert "Beta LLC" not in prompt
    assert "customResponse-para0002" not in prompt
    assert "Paragraph1: deny" in prompt
    assert "Paragraph2: custom: Admit contract date only." in prompt
    assert "Paragraph3: admit" in prompt
    assert "paragraph 1:\n1. Plaintiff alleges fact 1." in prompt
    assert "paragraph 2:\n2. Plaintiff alleges fact 2." in prompt


def test_answer_response_prompt_includes_completeness_contract(monkeypatch):
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: 1)
    prompt_template = (
        Path(__file__).resolve().parents[1] / "backend/ai/prompts/answer/DRAFT_RESPONSES"
    ).read_text()

    chunks = answer_drafter._build_answer_response_chunks(
        _make_selections(3),
        _make_para_2_ln(3),
        _make_lines(3),
        prompt_template,
        paras_per_call=100,
        max_prompt_tokens=60000,
    )

    assert len(chunks) == 1
    prompt = chunks[0]["prompt"]
    assert "Expected paragraph numbers:" in prompt
    assert "1, 2, 3" in prompt
    assert "Expected answer count:" in prompt
    assert "3" in prompt
    assert "Do not group paragraphs together." in prompt
    assert 'Do not say "same as above"' in prompt
    assert "appears exactly once" in prompt


def test_answer_response_json_selections_fill_default_deny_and_overlay_choices():
    outline = [["Facts", None], ["Paragraph 1", 1], ["Paragraph 2", 2], ["Paragraph 3", 3]]
    data = {
        "default_choice": "deny",
        "paragraph_selections": [
            {"outline_index": 2, "paragraph_number": 2, "choice": "admit"},
            {
                "outline_index": 3,
                "paragraph_number": 3,
                "choice": "custom",
                "custom_response": "Admit contract date only.",
            },
            {"outline_index": 4, "paragraph_number": 99, "choice": "admit"},
        ],
    }

    selections = answer_drafter._answer_response_paragraph_selections(data, outline=outline)

    assert list(selections) == [1, 2, 3]
    assert selections[1]["prompt_value"] == "deny"
    assert selections[2]["prompt_value"] == "admit"
    assert selections[3]["prompt_value"] == "custom: Admit contract date only."
    assert selections[1]["outline_index"] == 1
    assert selections[2]["outline_index"] == 2
    assert selections[3]["outline_index"] == 3


def test_answer_response_chunks_use_outline_order_when_available(monkeypatch):
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: 1)
    outline = [
        ["Facts", None],
        ["Paragraph 3065", 3065],
        ["Paragraph 30066", 30066],
        ["Paragraph 3067", 3067],
    ]
    selections = answer_drafter._answer_response_paragraph_selections(
        {"default_choice": "deny", "paragraph_selections": []},
        outline=outline,
    )

    chunks = answer_drafter._build_answer_response_chunks(
        selections,
        {"3065": 0, "30066": 1, "3067": 2},
        [
            "3065. First paragraph.",
            "30066. Suspect paragraph number.",
            "3067. Next paragraph.",
        ],
        "paragraphs:\n{paragraphs}\nchoices:\n{para_user_responses}",
        paras_per_call=100,
        max_prompt_tokens=60000,
    )

    assert chunks[0]["paragraph_numbers"] == [3065, 30066, 3067]


def test_answer_route_logic_dispatches_json_state1_payload(monkeypatch):
    captured = {}

    class JsonRequest:
        is_json = True
        form = {}

        def get_json(self, silent=True):
            return {
                "draftId": "draft-public-id",
                "appState": 1,
                "defendants": ["Beta LLC"],
                "default_choice": "deny",
                "paragraph_selections": [],
            }

    def fake_handle_state1(request, data=None):
        captured["request"] = request
        captured["data"] = data
        return {"result_id": "task-1"}, 200

    monkeypatch.setattr(answer_drafter, "handle_state1", fake_handle_state1)

    result = answer_drafter.route_answer_logic(JsonRequest())

    assert result == ({"result_id": "task-1"}, 200)
    assert captured["data"]["draftId"] == "draft-public-id"
    assert captured["data"]["paragraph_selections"] == []


def test_answer_response_chunk_builder_splits_oversized_prompt_by_paragraph(monkeypatch):
    selections = _make_selections(4)

    def fake_num_tokens(prompt):
        return len(re.findall(r"Paragraph\d+:", prompt)) * 100

    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", fake_num_tokens)

    chunks = answer_drafter._build_answer_response_chunks(
        selections,
        _make_para_2_ln(4),
        _make_lines(4),
        "paragraphs:\n{paragraphs}\nchoices:\n{para_user_responses}",
        paras_per_call=4,
        max_prompt_tokens=250,
    )

    assert [chunk["paragraph_numbers"] for chunk in chunks] == [[1, 2], [3, 4]]


def test_answer_response_chunk_retries_missing_answers_once(monkeypatch):
    selections = _make_selections(4)
    prompt_template = "paragraphs:\n{paragraphs}\nchoices:\n{para_user_responses}"
    chunks = answer_drafter._build_answer_response_chunks(
        selections,
        _make_para_2_ln(4),
        _make_lines(4),
        prompt_template,
        paras_per_call=4,
        max_prompt_tokens=60000,
    )
    calls = []

    def fake_chat_completion_j(prompt):
        calls.append(prompt)
        paragraphs = [
            int(match.group(1))
            for match in re.finditer(r"Paragraph(\d+):", prompt)
        ]
        if len(paragraphs) == 4:
            paragraphs = paragraphs[:2]
        return json.dumps({
            "Answers": [
                {
                    "paragraph": str(paragraph),
                    "response": f"Drafted response for Paragraph {paragraph}.",
                }
                for paragraph in paragraphs
            ]
        })

    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: 1)
    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)

    result = answer_drafter._extract_answer_response_chunk(
        chunks[0],
        selections,
        _make_para_2_ln(4),
        _make_lines(4),
        prompt_template,
    )

    assert len(calls) == 3
    assert result["retried"] is True
    assert result["had_error"] is False
    assert set(result["rows"]) == {1, 2, 3, 4}


def test_answer_response_chunk_fails_when_retry_still_missing_answers(monkeypatch):
    selections = _make_selections(2)
    prompt_template = "paragraphs:\n{paragraphs}\nchoices:\n{para_user_responses}"
    chunks = answer_drafter._build_answer_response_chunks(
        selections,
        _make_para_2_ln(2),
        _make_lines(2),
        prompt_template,
        paras_per_call=2,
        max_prompt_tokens=60000,
    )

    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: 1)
    monkeypatch.setattr(
        answer_drafter,
        "chat_completion_j",
        lambda prompt: json.dumps({"Answers": [{"paragraph": "1", "response": "Only one response."}]}),
    )

    result = answer_drafter._extract_answer_response_chunk(
        chunks[0],
        selections,
        _make_para_2_ln(2),
        _make_lines(2),
        prompt_template,
    )

    assert result["retried"] is True
    assert result["missing"] == [2]
    assert result["had_error"] is True


def test_answer_state1_uses_chunked_llm_outputs_and_keeps_outline_order(monkeypatch):
    memory = _base_memory(4)
    _patch_state1_infrastructure(monkeypatch, memory)
    prompts = []
    ads_suggestions = {"Failure to State a Claim": {"1": {"description": "Mock AD."}}}
    ensure_calls = []

    def fake_chat_completion_j(prompt):
        prompts.append(prompt)
        return _answers_from_prompt(prompt)

    def fake_ensure(memory_arg, **kwargs):
        ensure_calls.append((memory_arg, kwargs))
        return ads_suggestions

    monkeypatch.setattr(answer_drafter, "ANSWER_RESPONSE_PARAS_PER_CALL", 2)
    monkeypatch.setattr(answer_drafter, "ANSWER_RESPONSE_WORKERS", 1)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: 1)
    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)
    monkeypatch.setattr(answer_drafter, "ensure_answer_factual_digest_and_ads", fake_ensure)

    result = answer_drafter.state1.run({
        "draftId": "draft-public-id",
        "appState": "1",
        "defendant0": "Beta LLC",
        "para0004": "notEnoughInfo",
        "para0002": "custom",
        "customResponse-para0002": "Admit contract date only.",
        "para0001": "deny",
        "para0003": "admit",
    })

    assert result["message"] == "Form processed successfully"
    assert result["ads_suggestions"] == ads_suggestions
    assert len(ensure_calls) == 1
    assert ensure_calls[0][0] is memory
    assert ensure_calls[0][1]["text_by_lines"] == _make_lines(4)
    assert ensure_calls[0][1]["para_2_ln"] == _make_para_2_ln(4)
    assert len(prompts) == 2
    assert memory["inputs/paragraph_selections"] == (
        "Paragraph1: deny\n"
        "Paragraph2: custom: Admit contract date only.\n"
        "Paragraph3: admit\n"
        "Paragraph4: notEnoughInfo\n"
    )
    responses = memory["drafted_sections/responses"]
    assert "ANSWER TO FACTUAL ALLEGATIONS" in responses
    assert responses.index("Drafted response for Paragraph 1.") < responses.index("Drafted response for Paragraph 2.")
    assert responses.index("Drafted response for Paragraph 2.") < responses.index("Drafted response for Paragraph 3.")
    assert responses.index("Drafted response for Paragraph 3.") < responses.index("Drafted response for Paragraph 4.")


def test_answer_state1_json_payload_defaults_missing_paragraphs_to_deny(monkeypatch):
    memory = _base_memory(4)
    _patch_state1_infrastructure(monkeypatch, memory)
    prompts = []

    def fake_chat_completion_j(prompt):
        prompts.append(prompt)
        return _answers_from_prompt(prompt)

    monkeypatch.setattr(answer_drafter, "ANSWER_RESPONSE_PARAS_PER_CALL", 100)
    monkeypatch.setattr(answer_drafter, "ANSWER_RESPONSE_WORKERS", 1)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: 1)
    monkeypatch.setattr(answer_drafter, "chat_completion_j", fake_chat_completion_j)
    monkeypatch.setattr(answer_drafter, "ensure_answer_factual_digest_and_ads", lambda *args, **kwargs: {})

    result = answer_drafter.state1.run({
        "draftId": "draft-public-id",
        "appState": 1,
        "defendants": ["Beta LLC"],
        "default_choice": "deny",
        "paragraph_selections": [
            {
                "outline_index": 2,
                "paragraph_number": 2,
                "choice": "custom",
                "custom_response": "Admit contract date only.",
            },
            {"outline_index": 3, "paragraph_number": 3, "choice": "admit"},
        ],
    })

    assert result["message"] == "Form processed successfully"
    assert len(prompts) == 1
    assert "Paragraph1: deny" in prompts[0]
    assert "Paragraph2: custom: Admit contract date only." in prompts[0]
    assert "Paragraph3: admit" in prompts[0]
    assert "Paragraph4: deny" in prompts[0]
    assert memory["inputs/defendant"] == "Beta LLC"
    assert memory["inputs/paragraph_selections"] == (
        "Paragraph1: deny\n"
        "Paragraph2: custom: Admit contract date only.\n"
        "Paragraph3: admit\n"
        "Paragraph4: deny\n"
    )


def test_answer_state1_rejects_unprepared_draft_without_mutating_it(monkeypatch):
    memory = Memory()
    _patch_state1_infrastructure(monkeypatch, memory)

    result = answer_drafter.state1.run({
        "draftId": "empty-draft-public-id",
        "appState": 1,
        "defendants": ["Defendant copied from another draft"],
        "default_choice": "deny",
        "paragraph_selections": [],
    })

    assert result == {
        "message": "error",
        "error_message": "This Answer draft has not been prepared. Return to Upload Complaint and click Continue before drafting responses.",
    }
    assert memory == {}


def test_answer_state1_rejects_missing_prepared_paragraph_outline(monkeypatch):
    memory = Memory({"complaint_text": json.dumps(_make_lines(2))})
    _patch_state1_infrastructure(monkeypatch, memory)

    result = answer_drafter.state1.run({
        "draftId": "partially-prepared-draft-public-id",
        "appState": 1,
        "defendants": ["Beta LLC"],
        "default_choice": "deny",
        "paragraph_selections": [],
    })

    assert result == {
        "message": "error",
        "error_message": "This Answer draft is missing its prepared paragraph outline. Return to Upload Complaint and click Continue before drafting responses.",
    }
    assert "inputs/defendant" not in memory


def test_answer_state1_rejects_unprepared_general_denial_without_mutating_it(monkeypatch):
    memory = Memory({"complaint_text": json.dumps(_make_lines(2))})
    _patch_state1_infrastructure(monkeypatch, memory)

    result = answer_drafter.state1.run({
        "draftId": "parsed-but-not-prepared-draft-public-id",
        "appState": 1,
        "defendants": ["Defendant copied from another draft"],
        "generalDenial": "Defendant generally denies the complaint.",
    })

    assert result == {
        "message": "error",
        "error_message": "This Answer draft is missing its prepared general denial. Return to Upload Complaint and click Continue before drafting responses.",
    }
    assert "inputs/defendant" not in memory
    assert "drafted_sections/responses" not in memory


def test_answer_state1_returns_existing_error_when_chunk_retry_still_missing(monkeypatch):
    memory = _base_memory(2)
    _patch_state1_infrastructure(monkeypatch, memory)

    monkeypatch.setattr(answer_drafter, "ANSWER_RESPONSE_PARAS_PER_CALL", 2)
    monkeypatch.setattr(answer_drafter, "ANSWER_RESPONSE_WORKERS", 1)
    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: 1)
    monkeypatch.setattr(
        answer_drafter,
        "chat_completion_j",
        lambda prompt: json.dumps({"Answers": [{"paragraph": "1", "response": "Only one response."}]}),
    )
    monkeypatch.setattr(
        answer_drafter,
        "ensure_answer_factual_digest_and_ads",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("AD work should not run after response failure")),
    )

    result = answer_drafter.state1.run({
        "draftId": "draft-public-id",
        "appState": "1",
        "defendant0": "Beta LLC",
        "para0001": "deny",
        "para0002": "admit",
    })

    assert result == {
        "message": "error",
        "error_message": "Something went wrong when generating responses, please try again.",
    }
    assert "drafted_sections/responses" not in memory


def test_answer_state1_preserves_delaware_original_text_response_format(monkeypatch):
    memory = _base_memory(1, state="delaware")
    _patch_state1_infrastructure(monkeypatch, memory)

    monkeypatch.setattr(answer_drafter, "num_tokens_from_string", lambda prompt: 1)
    monkeypatch.setattr(answer_drafter, "chat_completion_j", lambda prompt: _answers_from_prompt(prompt))
    monkeypatch.setattr(answer_drafter, "ensure_answer_factual_digest_and_ads", lambda *args, **kwargs: {})

    result = answer_drafter.state1.run({
        "draftId": "draft-public-id",
        "appState": "1",
        "defendant0": "Beta LLC",
        "para0001": "deny",
    })

    assert result["message"] == "Form processed successfully"
    assert (
        "1. Plaintiff alleges fact 1.\n"
        "Response:\n"
        "Drafted response for Paragraph 1."
    ) in memory["drafted_sections/responses"]


def test_answer_state1_california_general_denial_reuses_saved_ad_suggestions(monkeypatch):
    memory = _base_memory(1, state="california")
    ads_suggestions = {"Equitable Estoppel": {"1": {"description": "Saved AD."}}}
    memory["inputs/is_federal"] = "state"
    memory["ads_suggestions"] = json.dumps(ads_suggestions)
    memory["analysis/general_denial"] = "Defendant generally denies the complaint."
    _patch_state1_infrastructure(monkeypatch, memory)

    monkeypatch.setattr(
        answer_drafter,
        "get_answer_factual_digest_from_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("saved CA/TX ads should be reused")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_ad_suggestions_from_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("saved CA/TX ads should be reused")),
    )

    result = answer_drafter.state1.run({
        "draftId": "draft-public-id",
        "appState": "1",
        "defendant0": "Beta LLC",
        "generalDenial": "Defendant generally denies the complaint.",
    })

    assert result["message"] == "Form processed successfully"
    assert result["ads_suggestions"] == ads_suggestions
    assert memory["drafted_sections/responses"] == "Defendant generally denies the complaint."
