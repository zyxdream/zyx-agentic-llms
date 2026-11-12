"""Provider-path coverage for Case Assistant's structured LLM steps."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from backend.ai import base as ai_base
from backend import everything
from backend.ai.case_assistant_schemas import (
    BriefUpdateResult,
    IntentDecisionResult,
    PreserveOriginalEditResult,
    PreserveOriginalTargetResult,
    RelevantFileExtractionResult,
)


@pytest.fixture
def parsed_provider(monkeypatch):
    requests = []
    payloads = {
        IntentDecisionResult: {
            "draft_action": "answer_only",
            "needs_confirmation": False,
            "confirmation_reason": None,
            "confirmation_options": [],
            "revision_mode": "general_revision",
            "selected_files": [],
            "context_policy": "none",
        },
        RelevantFileExtractionResult: {
            "file_name": "Evidence.pdf",
            "file_id": "file-1",
            "file_scope": "matter_background",
            "relevant_excerpts": "Exact passage.",
            "source_anchors": ["page 1"],
        },
        BriefUpdateResult: {
            "brief": "The issue is notice.",
            "constraints": {
                "tone": None,
                "exclusions": [],
                "special_instructions": None,
                "cases": [{
                    "case_label": "Smith v. Jones",
                    "jurisdiction": "California",
                    "court": None,
                    "case_type": None,
                    "party_names": ["Smith", "Jones"],
                    "key_exhibits": [],
                    "deadlines": [],
                }],
            },
        },
        PreserveOriginalTargetResult: {
            "edit_scope": "targeted_blocks",
            "anchors": ["p_0001"],
            "confidence": "high",
            "reason": "The passage contains the cited assertion.",
            "needs_confirmation": False,
        },
        PreserveOriginalEditResult: {
            "operations": [{
                "op": "edit_preserved_block",
                "anchor": "p_0001",
                "replacement_text": "Revised passage.",
                "change_summary": "Corrected the passage.",
                "citation_changes": [],
            }],
            "response": "I have made the following changes to the current draft to the right: corrected the passage.",
        },
    }

    def parse(**kwargs):
        requests.append(kwargs)
        result = kwargs["text_format"].model_validate(payloads[kwargs["text_format"]])
        return SimpleNamespace(
            id=f"resp_{len(requests)}",
            status="completed",
            output_parsed=result,
            output=[],
            usage=SimpleNamespace(total_tokens=9),
        )

    monkeypatch.setattr(
        ai_base,
        "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(parse=parse)),
    )
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", lambda *_args, **_kwargs: None)
    return requests


@pytest.mark.parametrize("mode,model", [("deep", "gpt-6.1-sol"), ("fast", "gpt-6-luna")])
def test_planner_and_file_extraction_provider_routes(parsed_provider, mode, model):
    assert everything._assistant_model(mode) == model
    extraction_effort = everything._extraction_effort(mode)
    assert extraction_effort == "medium"
    decision = everything.classify_intent_decision(
        {}, "Summarize the attachment.", {}, "Not Specified",
        [], [], [], "", "", model=model, reasoning_effort="medium",
    )
    extracted = everything._extract_one_relevant_file(
        "Summarize the attachment.",
        {"file_name": "Evidence.pdf", "file_id": "file-1", "file_scope": "matter_background",
         "chunks": [{"chunk_index": 0, "content": "Exact passage."}]},
        model=model,
        reasoning_effort=extraction_effort,
    )

    assert decision["draft_action"] == "answer_only"
    assert extracted["relevant_excerpts"] == "Exact passage."
    assert [(request["model"], request["reasoning"], request["text_format"])
            for request in parsed_provider] == [
        (model, {"effort": "medium"}, IntentDecisionResult),
        (model, {"effort": extraction_effort}, RelevantFileExtractionResult),
    ]


def test_background_brief_preserves_case_keyed_contract_at_medium_effort(parsed_provider):
    example = json.loads(everything.BRIEF_UPDATE_PROMPT.split("Return JSON: ", 1)[1])
    BriefUpdateResult.model_validate(example)
    brief, constraints = everything.update_conversation_brief(
        "", {}, "What happened?", "The issue is notice.",
        model=ai_base.DEFAULT_MODEL, reasoning_effort="medium",
    )

    assert brief == "The issue is notice."
    assert constraints["cases"]["Smith v. Jones"]["jurisdiction"] == "California"
    assert constraints["cases"]["Smith v. Jones"]["party_names"] == ["Smith", "Jones"]
    assert parsed_provider[0]["text_format"] is BriefUpdateResult
    assert parsed_provider[0]["model"] == ai_base.DEFAULT_MODEL
    assert parsed_provider[0]["reasoning"] == {"effort": "medium"}


@pytest.mark.parametrize("mode,model", [
    ("fast", "gpt-6-luna"), ("deep", "gpt-6.1-sol"), (None, "gpt-6.1-sol"),
])
def test_background_brief_routes_from_captured_turn_mode(parsed_provider, monkeypatch, mode, model):
    monkeypatch.setattr('backend.util.account_access.chat_account_id', lambda _id: 1)
    monkeypatch.setattr('backend.util.account_access.require_enabled_account', lambda _id: None)
    queued = []
    state = {"conversation_brief": "", "constraints": {}}
    monkeypatch.setattr(everything, "everything_update_brief_task", SimpleNamespace(
        apply_async=lambda **kwargs: queued.append(kwargs["args"][0]),
    ))
    monkeypatch.setattr(everything, "DBMemory", lambda **_kwargs: object())
    monkeypatch.setattr(everything, "load_state_from_memory", lambda _memory: state)
    monkeypatch.setattr(everything, "save_state_to_memory", lambda *_args: None)

    if mode is None:
        # Jobs queued before mode capture continue using the base model.
        payload = {"doc_id": 1, "user_message": "Question", "assistant_response": "Answer"}
    else:
        everything._enqueue_brief_update(1, "Question", "Answer", assistant_mode=mode)
        payload = queued[0]
        assert payload["assistant_mode"] == mode
    # A newer turn can use another mode before the deferred job starts.
    state["assistant_mode"] = "deep" if mode == "fast" else "fast"
    everything._run_brief_update(payload)

    assert len(parsed_provider) == 1
    assert parsed_provider[0]["model"] == model
    assert parsed_provider[0]["reasoning"] == {"effort": "medium"}
    assert state["constraints"]["cases"]["Smith v. Jones"]["jurisdiction"] == "California"


@pytest.mark.parametrize("mode,model", [("deep", "gpt-6.1-sol"), ("fast", "gpt-6-luna")])
def test_preserve_original_selection_and_edit_provider_routes(parsed_provider, mode, model):
    assert everything._assistant_model(mode) == model
    output_effort = everything._output_effort(mode)
    assert output_effort == ("high" if mode == "deep" else "medium")
    selected = everything.select_preserve_original_targets(
        "Correct the passage.", [{"anchor": "p_0001", "snippet": "Original passage."}],
        model=model, reasoning_effort="medium",
    )
    edited = everything.edit_preserve_original_blocks(
        "Correct the passage.",
        {"editable_blocks": [{"anchor": "p_0001", "text": "Original passage."}],
         "context_blocks": []},
        model=model, reasoning_effort=output_effort,
    )

    assert selected["anchors"] == ["p_0001"]
    assert edited["error"] is False
    assert edited["edits"][0]["replacement_text"] == "Revised passage."
    assert [(request["text_format"], request["reasoning"]) for request in parsed_provider] == [
        (PreserveOriginalTargetResult, {"effort": "medium"}),
        (PreserveOriginalEditResult, {"effort": output_effort}),
    ]
    assert all(request["model"] == model for request in parsed_provider)


def test_missing_typed_planner_and_edit_are_not_successes(monkeypatch):
    monkeypatch.setattr(everything, "chat_completion_parse", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="reasoning process"):
        everything.classify_intent_decision(
            {}, "Summarize the attachment.", {}, "Not Specified",
            [], [], [], "", "", model=ai_base.DEFAULT_MODEL,
            reasoning_effort="medium",
        )

    result = everything.edit_preserve_original_blocks(
        "Correct the passage.",
        {"editable_blocks": [{"anchor": "p_0001", "text": "Original passage."}],
         "context_blocks": []},
        model=ai_base.DEFAULT_MODEL,
        reasoning_effort="medium",
    )
    assert result["error"] is True
    assert result["edits"] == []


@pytest.mark.parametrize("missing_step", ["selection", "edit"])
def test_preserve_original_parse_failure_is_not_user_confirmation(monkeypatch, missing_step):
    monkeypatch.setattr(
        everything,
        "load_primary_uploaded_original_text",
        lambda *_args, **_kwargs: {
            "error": False,
            "content": "INTRODUCTION\n\nOriginal passage.\n",
            "filename": "Draft.txt",
        },
    )
    if missing_step == "edit":
        monkeypatch.setattr(
            everything,
            "select_preserve_original_targets",
            lambda *_args, **_kwargs: {
                "edit_scope": "targeted_blocks",
                "anchors": ["p_0002"],
                "confidence": "high",
                "reason": "The passage needs correction.",
                "needs_confirmation": False,
            },
        )
    monkeypatch.setattr(everything, "chat_completion_parse", lambda *_args, **_kwargs: None)

    result = everything.run_preserve_original_uploaded_revision(
        user_message="Correct the passage.",
        primary_draft_file_id="file-1",
        doc_id=1,
        project_id=None,
        model=ai_base.DEFAULT_MODEL,
        selection_reasoning_effort="medium",
        edit_reasoning_effort="medium",
        structure_reasoning_effort="medium",
    )

    assert result["error"] is True
    assert result["blocked"] is False
    assert result["draft"] is None


@pytest.mark.parametrize("mode", ["fast", "deep"])
def test_confirmation_resume_preserves_mode_through_both_phases(monkeypatch, mode):
    workflow_run_id = "wf-mode-test"
    original_turn_input = {
        "draftId": "draft-1",
        "query": "Revise the draft.",
        "assistant_mode": mode,
        "model": "gpt-5.5",  # A stale model field cannot override an explicit mode.
    }
    state = {
        "active_workflow": {
            "workflow_run_id": workflow_run_id,
            "status": "awaiting_user_confirmation",
            "pending_confirmation": {
                "workflow_run_id": workflow_run_id,
                "original_turn_input": original_turn_input,
                "original_decision": {},
            },
        }
    }
    observed = {}

    monkeypatch.setattr(everything.ChatThread, "resolve_id", lambda _draft_id: 1)
    monkeypatch.setattr(everything, "DBMemory", lambda **_kwargs: object())
    monkeypatch.setattr(everything, "load_state_from_memory", lambda _memory: state)
    monkeypatch.setattr(everything, "task_llm_usage_scope", lambda *_args: nullcontext())
    monkeypatch.setattr(everything, "_record_successful_chat_answer", lambda *_args: None)

    def fake_phase1(_task, turn_input, **_kwargs):
        observed["phase1_mode"] = turn_input["assistant_mode"]
        observed["phase1_resolved"] = everything.resolve_assistant_mode(turn_input)
        observed["phase1_model"] = everything._assistant_model(observed["phase1_resolved"])
        return {"doc_id": 1, "assistant_mode": observed["phase1_resolved"], "error": False}

    def fake_phase2(_task, payload, _doc_id):
        observed["phase2_mode"] = everything.resolve_assistant_mode(payload)
        observed["phase2_model"] = everything._assistant_model(observed["phase2_mode"])
        return {"response": "Done.", "text": {}, "error": False}

    monkeypatch.setattr(everything, "_everything_phase1_body", fake_phase1)
    monkeypatch.setattr(everything, "_everything_phase2_body", fake_phase2)

    result = everything.everything_confirmed_workflow_task.run({
        "draftId": "draft-1",
        "workflow_run_id": workflow_run_id,
        "decision": {},
    })

    assert result["error"] is False
    assert observed == {
        "phase1_mode": mode,
        "phase1_resolved": mode,
        "phase2_mode": mode,
        "phase1_model": "gpt-6-luna" if mode == "fast" else "gpt-6.1-sol",
        "phase2_model": "gpt-6-luna" if mode == "fast" else "gpt-6.1-sol",
    }
