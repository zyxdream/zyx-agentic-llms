"""Exercise phase-2 output routing through the actual Responses helpers."""

from types import SimpleNamespace

import pytest

from backend import everything
from backend.ai import base as ai_base
from tests import test_everything_full_content as phase2_helpers


@pytest.mark.parametrize("mode,model,effort", [
    ("fast", "gpt-6-luna", "medium"),
    ("deep", "gpt-6.1-sol", "high"),
])
@pytest.mark.parametrize("action", [
    "answer_only", "create_new_draft", "edit_current_draft",
    "revise_uploaded_draft", "preserve_original",
])
def test_phase2_outputs_reach_provider_with_mode_effort(
    monkeypatch, mode, model, effort, action
):
    state = phase2_helpers._base_phase2_state(
        phase2_helpers._block_operation_sample_draft()
        if action == "edit_current_draft" else None
    )
    phase2_helpers._install_phase2_draft_action_mocks(monkeypatch, state)
    requests = []
    structure_calls = []
    brief_calls = []

    # Keep deterministic source/structure fixtures while leaving all output and
    # preserve-original selection/edit calls on the production provider helpers.
    normalize = phase2_helpers._fake_normalizer_factory([])

    def normalize_draft(draft, **kwargs):
        structure_calls.append(kwargs)
        return normalize(draft, **kwargs)

    def structure_source(text, *, title="", **kwargs):
        structure_calls.append(kwargs)
        return {
            "title": title,
            "sections": [{
                "section_id": "1", "section_name": "Intro",
                "section_content": text,
            }],
        }

    monkeypatch.setattr(everything, "normalize_draft_structure", normalize_draft)
    monkeypatch.setattr(everything, "structure_draft_text", structure_source)
    monkeypatch.setattr(everything, "load_primary_uploaded_original_text", lambda *args, **kwargs: {
        "error": False, "filename": "Uploaded Draft", "content": "Original passage.\n",
    })
    monkeypatch.setattr(everything, "_enqueue_brief_update", lambda *args, **kwargs: brief_calls.append(kwargs))
    monkeypatch.setattr(everything, "num_tokens_from_string", lambda *args, **kwargs: 10)

    def response(kwargs, parsed=None, text=""):
        return SimpleNamespace(
            id=f"phase2_output_{len(requests)}", model=kwargs["model"],
            status="completed", output_parsed=parsed, output_text=text,
            output=[], usage=SimpleNamespace(total_tokens=12),
        )

    def create(**kwargs):
        requests.append(kwargs)
        return response(kwargs, text="Completed answer.")

    def parse(**kwargs):
        requests.append(kwargs)
        schema = kwargs["text_format"]
        if schema is everything.DraftCreateResult:
            payload = {
                "response": "I drafted the document.",
                "draft": {"title": "New Draft", "sections": [{
                    "section_id": "1", "section_name": "Intro",
                    "section_content": "Drafted body.",
                }]},
            }
        elif schema is everything.DraftEditResult:
            payload = {
                "response": "I revised the draft.",
                "operations": [{
                    "op": "edit_section", "section_id": "1",
                    "section_name": "Intro", "section_content": "Revised body.",
                }],
            }
        elif schema is everything.PreserveOriginalTargetResult:
            payload = {
                "edit_scope": "targeted_blocks", "anchors": ["p_0001"],
                "confidence": "high", "reason": "The requested passage.",
                "needs_confirmation": False,
            }
        else:
            assert schema is everything.PreserveOriginalEditResult
            payload = {
                "response": "I revised the original passage.",
                "operations": [{
                    "op": "edit_preserved_block", "anchor": "p_0001",
                    "replacement_text": "Revised original passage.",
                    "change_summary": "Updated the passage.", "citation_changes": [],
                }],
            }
        return response(kwargs, parsed=schema.model_validate(payload))

    monkeypatch.setattr(ai_base, "get_openai_client", lambda: SimpleNamespace(
        responses=SimpleNamespace(create=create, parse=parse)
    ))
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", lambda *args, **kwargs: None)

    payload = phase2_helpers._base_phase2_payload(
        assistant_mode=mode,
        draft_action="revise_uploaded_draft" if action == "preserve_original" else action,
        create_new_draft=action == "create_new_draft",
        change_draft_requested=action not in ("answer_only", "create_new_draft"),
    )
    if action in ("revise_uploaded_draft", "preserve_original"):
        payload.update(
            use_uploaded_files=True,
            file_selection={"primary_draft_file_id": "draft-file"},
            verbatim_file_unique_ids=["draft-file"],
            file_extractions_str="Source context.",
            revision_mode="preserve_original" if action == "preserve_original" else "general_revision",
        )

    result = everything._everything_phase2_body(
        phase2_helpers._DummyTask(), payload, doc_id=1
    )

    assert not result.get("error", False)
    assert result["response"]
    assert len(state["chat_history"]) == 1
    assert len(requests) == (2 if action == "preserve_original" else 1)
    assert all(request["model"] == model for request in requests)
    if action == "preserve_original":
        assert requests[0]["text_format"] is everything.PreserveOriginalTargetResult
        assert requests[0]["reasoning"] == {"effort": "medium"}
    assert requests[-1]["reasoning"] == {"effort": effort}
    if action == "answer_only":
        assert "text_format" not in requests[-1]
    else:
        assert requests[-1]["text_format"] in (
            everything.DraftCreateResult, everything.DraftEditResult,
            everything.PreserveOriginalEditResult,
        )
        assert structure_calls
        assert all(call["model"] == model and call["reasoning_effort"] == "medium"
                   for call in structure_calls)
    assert brief_calls == [{"assistant_mode": mode}]
