import sys
import types
import copy
import json
import re
from types import SimpleNamespace

import pytest

try:
    import celery  # noqa: F401
except ImportError:
    sys.modules.setdefault(
        "celery",
        types.SimpleNamespace(shared_task=lambda *_args, **_kwargs: (lambda func: func)),
    )

import backend.everything as everything


@pytest.fixture(autouse=True)
def _stub_brief_update_task(monkeypatch):
    """The conversation-brief update is deferred to a background Celery task. Stub its enqueue
    so tests never attempt a real broker connection (apply_async would block on connect)."""
    monkeypatch.setattr(
        everything,
        "everything_update_brief_task",
        SimpleNamespace(apply_async=lambda *args, **kwargs: None),
        raising=False,
    )


def _passthrough_structure_draft_text(text, *, title="", model=None, reasoning_effort=None):
    """Test stub for structure_draft_text: keep all edited text in one section.

    The real multi-section structuring is covered in tests/util/test_draft_structure.py;
    preserve-original tests only need run_preserve_* to delegate reconstruction
    deterministically without extra LLM calls.
    """
    return {
        "title": title,
        "sections": [{"section_id": "1", "section_name": title, "section_content": text}],
    }


def _typed_parse_result(response_model, raw_result):
    """Turn a legacy test fixture into the required structured provider result."""
    payload = json.loads(raw_result) if isinstance(raw_result, str) else dict(raw_result)
    if response_model is everything.IntentDecisionResult:
        payload = {
            "draft_action": "answer_only",
            "needs_confirmation": False,
            "confirmation_reason": None,
            "confirmation_options": [],
            "revision_mode": "general_revision",
            "selected_files": [],
            "context_policy": "none",
            **payload,
        }
        for item in payload["selected_files"]:
            if item["method"] == "retrieve":
                item["method"] = "relevant"
    elif response_model is everything.RelevantFileExtractionResult:
        payload = {
            "file_name": "",
            "file_id": "",
            "file_scope": "",
            "relevant_excerpts": "",
            "source_anchors": [],
            **payload,
        }
    elif response_model is everything.PreserveOriginalTargetResult:
        payload = {
            "edit_scope": "targeted_blocks",
            "anchors": [],
            "confidence": "high",
            "reason": "",
            "needs_confirmation": False,
            **payload,
        }
    return response_model.model_validate(payload)


def _mock_chat_parse(raw_fn, calls=None):
    def parse(prompt, response_model, *, model, reasoning_effort, token_counter=None, **_kwargs):
        if calls is not None:
            calls.append({"model": model, "reasoning_effort": reasoning_effort,
                          "response_model": response_model, "prompt": prompt})
        return _typed_parse_result(response_model, raw_fn(prompt))

    return parse


def _mock_multichat_parse(raw_fn, calls=None):
    def parse(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return _typed_parse_result(kwargs["response_model"], raw_fn(**kwargs))

    return parse


@pytest.mark.parametrize(("payload", "expected"), [
    ({"assistant_mode": "deep"}, "deep"),
    ({"assistant_mode": "fast", "model": "gpt-5.5"}, "fast"),
    ({"model": "gpt-5.5"}, "deep"),
    ({"model": "gpt-6-sol"}, "fast"),
    ({"model": "gpt-6.1-sol"}, "fast"),
    ({"selected_model": "gpt-5.5"}, "deep"),
    ({"selected_model": "gpt-6-sol"}, "fast"),
    ({"selected_model": "gpt-6.1-sol"}, "fast"),
    ({"model": "gpt-5.4"}, "deep"),
    ({}, "deep"),
])
def test_resolve_assistant_mode_explicit_and_legacy(payload, expected):
    assert everything.resolve_assistant_mode(payload) == expected


@pytest.mark.parametrize("assistant_mode", [None, "", "turbo", "Deep"])
def test_resolve_assistant_mode_rejects_invalid_explicit_mode(assistant_mode):
    with pytest.raises(ValueError, match="Invalid Case Assistant mode"):
        everything.resolve_assistant_mode({"assistant_mode": assistant_mode})


def _make_row(row_id, source_unique_id, chunk_index=0):
    return SimpleNamespace(
        id=row_id,
        uploaded_file_id=row_id,
        chunk_index=chunk_index,
        content=f"content-{row_id}",
        token_count=10,
        source_filename=f"{source_unique_id}.txt",
        source_relative_path=None,
        source_unique_id=source_unique_id,
    )


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, query_results):
        self._query_results = list(query_results)
        self.query_calls = 0

    def query(self, *_args, **_kwargs):
        rows = self._query_results[self.query_calls]
        self.query_calls += 1
        return _FakeQuery(rows)


class _DummyTask:
    def update_state(self, state=None):
        return None


def test_get_full_content_chunks_backfills_only_unresolved_prefix_ids(monkeypatch):
    exact_row = _make_row(1, "123-secure")
    legacy_row = _make_row(2, "123-legacy")

    fake_session = _FakeSession([
        [exact_row],
        [exact_row, legacy_row],
    ])
    monkeypatch.setattr(everything.db, "session", fake_session)

    chunks = everything.get_full_content_chunks(
        draft_id=1,
        file_unique_ids=["123-secure", "123"],
    )

    assert fake_session.query_calls == 2
    assert [chunk["id"] for chunk in chunks] == [1, 2]
    assert [chunk["source_unique_id"] for chunk in chunks] == ["123-secure", "123-legacy"]


def test_select_full_content_rows_uses_round_robin_file_batches(monkeypatch):
    monkeypatch.setattr(everything, "MAX_FULL_CONTENT_CHUNKS", 24)

    rows = []
    row_id = 1
    for source_unique_id in ["file-a", "file-b", "file-c"]:
        for chunk_index in range(10):
            rows.append(_make_row(row_id, source_unique_id, chunk_index=chunk_index))
            row_id += 1

    selected_rows = everything._select_full_content_rows(
        rows,
        file_unique_ids=["file-a", "file-b", "file-c"],
        max_chunks=everything.MAX_FULL_CONTENT_CHUNKS,
    )

    assert len(selected_rows) == 24
    assert [row.source_unique_id for row in selected_rows[:15]] == (
        ["file-a"] * 5 + ["file-b"] * 5 + ["file-c"] * 5
    )
    assert [row.chunk_index for row in selected_rows[:15]] == [0, 1, 2, 3, 4] * 3
    assert [row.source_unique_id for row in selected_rows[15:]] == (
        ["file-a"] * 3 + ["file-b"] * 3 + ["file-c"] * 3
    )
    assert [row.chunk_index for row in selected_rows[15:]] == [5, 6, 7] * 3


def test_has_substantive_workspace_draft_detects_real_draft_only():
    assert everything.has_substantive_workspace_draft({}) is False
    assert everything.has_substantive_workspace_draft({"title": "", "sections": []}) is False
    assert everything.has_substantive_workspace_draft({"title": "Motion", "sections": []}) is True
    assert everything.has_substantive_workspace_draft({
        "title": "",
        "sections": [{"section_name": "", "section_content": ""}],
    }) is False
    assert everything.has_substantive_workspace_draft({
        "title": "",
        "sections": [{"section_name": "Facts", "section_content": ""}],
    }) is True
    assert everything.has_substantive_workspace_draft({
        "title": "",
        "sections": [{"section_name": "", "section_content": "Text"}],
    }) is True


def _assert_cases_cited_response_requirement(prompt: str):
    assert "Cases cited in that draft:" in prompt
    assert "using the same case/citation text as it appears in the draft" in prompt
    assert "prefix each listed case with [[CASE_START]]" in prompt
    assert "Do not put [[CASE_START]] in section_content" in prompt


def test_build_draft_edit_operations_prompt_includes_draft_menu_and_message():
    draft = {"title": "Draft", "sections": [{"section_id": "1", "section_name": "Intro", "section_content": "Body."}]}
    prompt = everything.build_draft_edit_operations_prompt(
        draft=draft,
        user_message="Fix the citations.",
        selected_section="Not Specified",
        file_extractions_str="some context",
    )
    assert "Fix the citations." in prompt
    assert "\"section_content\": \"Body.\"" in prompt  # draft embedded as JSON
    assert "'op':'edit_block'" in prompt
    assert "'op':'edit_section'" in prompt
    assert "No specific section" in prompt  # selected_section normalized
    _assert_cases_cited_response_requirement(prompt)


def test_phase2_prompt_shell_includes_cases_cited_response_requirement():
    create_prompt = everything._build_phase2_new_prompt_shell(
        prompt_kind="create_draft",
        user_message="Draft an answer.",
        selected_section="Not Specified",
        current_draft={},
        use_uploaded_files=True,
        verbatim_draft_hint="",
    )
    edit_prompt = everything._build_phase2_new_prompt_shell(
        prompt_kind="edit",
        user_message="Revise the draft.",
        selected_section="Not Specified",
        current_draft={"title": "Draft", "sections": []},
        use_uploaded_files=True,
        verbatim_draft_hint="",
    )
    _assert_cases_cited_response_requirement(create_prompt)
    _assert_cases_cited_response_requirement(edit_prompt)


def test_apply_draft_edit_operations_applies_edits_and_rejects_unknown():
    draft = {
        "title": "Draft",
        "sections": [
            {
                "section_id": "1",
                "section_name": "Intro",
                "blocks": [{"block_id": "b_0001", "type": "paragraph", "text": "Old paragraph.", "paragraph_index": 1, "paragraph_number": None}],
                "section_content": "Old paragraph.",
            },
            {"section_id": "2", "section_name": "Argument", "section_content": "Keep me."},
        ],
    }
    operations = [
        {"op": "edit_block", "section_id": "1", "block_id": "b_0001", "text": "New paragraph."},
        {"op": "edit_section", "section_id": "2", "section_name": "Argument", "section_content": "Edited argument."},
        {"op": "edit_block", "section_id": "1", "block_id": "b_9999", "text": "Should be rejected."},
    ]

    updated, applied, rejected = everything.apply_draft_edit_operations(draft, operations)

    # Original draft is not mutated (operations run on a copy).
    assert draft["sections"][0]["blocks"][0]["text"] == "Old paragraph."
    assert updated["sections"][0]["blocks"][0]["text"] == "New paragraph."
    assert everything._find_draft_section(updated, "2")["section_content"] == "Edited argument."
    assert any(r.get("block_id") == "b_9999" for r in rejected)
    assert "Should be rejected." not in json.dumps(updated)


def test_enqueue_brief_update_enqueues_on_success_and_skips_otherwise(monkeypatch):
    captured = []
    monkeypatch.setattr(
        everything,
        "everything_update_brief_task",
        SimpleNamespace(apply_async=lambda *args, **kwargs: captured.append((args, kwargs))),
        raising=False,
    )

    everything._enqueue_brief_update(7, "fix the citations", "Here is the answer.")
    assert len(captured) == 1
    payload = captured[0][1]["args"][0]
    assert payload == {
        "doc_id": 7,
        "user_message": "fix the citations",
        "assistant_response": "Here is the answer.",
        "assistant_mode": "deep",
    }

    # Skips on error or empty response.
    everything._enqueue_brief_update(7, "msg", "resp", error_flag=True)
    everything._enqueue_brief_update(7, "msg", "")
    assert len(captured) == 1


def test_run_brief_update_refreshes_brief_and_constraints(monkeypatch):
    monkeypatch.setattr('backend.util.account_access.chat_account_id', lambda _id: 1)
    monkeypatch.setattr('backend.util.account_access.require_enabled_account', lambda _id: None)
    stale_state = {
        "conversation_brief": "old",
        "constraints": {"jurisdiction": "CA"},
        "draft": {"title": "", "sections": []},
        "chat_history": [("old q", "old a")],
    }
    latest_state = {
        "conversation_brief": "old",
        "constraints": {"jurisdiction": "CA"},
        "draft": {
            "title": "Generated Draft",
            "sections": [{"section_name": "Scope", "section_content": "Body"}],
        },
        "chat_history": [("old q", "old a"), ("make draft", "done")],
    }
    loads = iter([stale_state, latest_state])
    saved = {}
    monkeypatch.setattr(everything, "DBMemory", lambda db, doc_id: {"doc_id": doc_id})
    monkeypatch.setattr(everything, "load_state_from_memory", lambda memory: next(loads))
    monkeypatch.setattr(everything, "save_state_to_memory", lambda s, memory, **kw: saved.update({"state": s}))
    monkeypatch.setattr(
        everything,
        "update_conversation_brief",
        lambda brief, constraints, user_message, assistant_response, **kw: ("new brief", {"jurisdiction": "NY"}),
    )

    everything._run_brief_update({"doc_id": 1, "user_message": "u", "assistant_response": "a"})

    assert stale_state["conversation_brief"] == "old"
    assert stale_state["constraints"] == {"jurisdiction": "CA"}
    assert saved["state"] is latest_state
    assert latest_state["conversation_brief"] == "new brief"
    assert latest_state["constraints"] == {"jurisdiction": "NY"}
    assert latest_state["draft"] == {
        "title": "Generated Draft",
        "sections": [{"section_name": "Scope", "section_content": "Body"}],
    }
    assert latest_state["chat_history"] == [("old q", "old a"), ("make draft", "done")]


def test_save_draft_to_state_preserves_blocks_and_empty_section_name():
    state = {}
    everything.save_draft_to_state(
        state,
        {
            "title": "Draft",
            "sections": [
                {
                    "section_id": "preface",
                    "section_name": "",
                    "section_content": "Preface text.",
                    "blocks": [{"block_id": "b_0001", "type": "paragraph", "text": "Preface text."}],
                }
            ],
        },
    )

    section = state["draft"]["sections"][0]
    assert section["section_name"] == ""
    assert section["section_content"] == "Preface text."
    assert section["blocks"][0]["paragraph_index"] == 1


def test_save_draft_to_state_drops_stale_blocks_after_manual_edit():
    state = {}
    everything.save_draft_to_state(
        state,
        {
            "title": "Draft",
            "sections": [
                {
                    "section_id": "1",
                    "section_name": "Intro",
                    "section_content": "New text.",
                    "blocks": [{"block_id": "b_0001", "type": "paragraph", "text": "Old text."}],
                }
            ],
        },
    )

    section = state["draft"]["sections"][0]
    assert section["section_content"] == "New text."
    assert "blocks" not in section


def _block_operation_sample_draft():
    return {
        "title": "Draft",
        "sections": [
            {
                "section_id": "1",
                "section_name": "Intro",
                "blocks": [
                    {"block_id": "b_0001", "type": "paragraph", "text": "First paragraph."},
                    {"block_id": "b_0002", "type": "paragraph", "text": "Second paragraph."},
                ],
                "section_content": "First paragraph.\n\nSecond paragraph.",
            },
            {
                "section_id": "2",
                "section_name": "Argument",
                "blocks": [
                    {"block_id": "b_0001", "type": "heading", "text": "A. Issue", "level": 2},
                    {"block_id": "b_0002", "type": "paragraph", "text": "Argument paragraph."},
                ],
                "section_content": "**A. Issue**\n\nArgument paragraph.",
            },
        ],
    }


def test_apply_block_operations_edit_add_delete_and_reassemble():
    draft, applied, rejected = everything.apply_block_operations_to_draft(
        _block_operation_sample_draft(),
        [
            {"op": "edit_block", "section_id": "1", "block_id": "b_0001", "text": "Revised first paragraph."},
            {"op": "add_block", "section_id": "1", "after_block_id": "b_0001", "block_type": "paragraph", "text": "Inserted paragraph."},
            {"op": "delete_block", "section_id": "1", "block_id": "b_0002"},
        ],
    )

    section = draft["sections"][0]
    assert rejected == []
    assert [item["op"] for item in applied] == ["edit_block", "add_block", "delete_block"]
    assert [block["block_id"] for block in section["blocks"]] == ["b_0001", "b_0003"]
    assert section["blocks"][0]["text"] == "Revised first paragraph."
    assert section["blocks"][1]["text"] == "Inserted paragraph."
    assert section["section_content"] == "Revised first paragraph.\n\nInserted paragraph."


def test_apply_block_operations_moves_block_across_sections_with_collision_safe_id():
    draft, applied, rejected = everything.apply_block_operations_to_draft(
        _block_operation_sample_draft(),
        [
            {
                "op": "move_block",
                "section_id": "1",
                "block_id": "b_0001",
                "target_section_id": "2",
                "after_block_id": "b_0001",
            }
        ],
    )

    assert rejected == []
    assert applied[0]["original_block_id"] == "b_0001"
    assert applied[0]["block_id"] == "b_0003"
    assert [block["text"] for block in draft["sections"][0]["blocks"]] == ["Second paragraph."]
    assert [block["block_id"] for block in draft["sections"][1]["blocks"]] == ["b_0001", "b_0003", "b_0002"]
    assert draft["sections"][1]["section_content"] == "**A. Issue**\n\nFirst paragraph.\n\nArgument paragraph."


def test_apply_block_operations_rejects_unknown_ids_and_blank_text():
    draft, applied, rejected = everything.apply_block_operations_to_draft(
        _block_operation_sample_draft(),
        [
            {"op": "edit_block", "section_id": "1", "block_id": "b_9999", "text": "Nope."},
            {"op": "add_block", "section_id": "1", "after_block_id": "b_0001", "block_type": "paragraph", "text": "   "},
            {"op": "delete_block", "section_id": "missing", "block_id": "b_0001"},
        ],
    )

    assert applied == []
    assert rejected == [
        {"op": "edit_block", "reason": "unknown_block", "section_id": "1", "block_id": "b_9999"},
        {"op": "add_block", "reason": "blank_text", "section_id": "1", "after_block_id": "b_0001"},
        {"op": "delete_block", "reason": "unknown_section", "section_id": "missing", "block_id": "b_0001"},
    ]
    assert draft["sections"][0]["section_content"] == "First paragraph.\n\nSecond paragraph."


def test_block_operations_rejected_against_content_only_sections_preserve_content():
    draft = {
        "title": "Draft",
        "sections": [
            {"section_id": "1", "section_name": "Intro", "section_content": "Keep this body."},
            {"section_id": "9", "section_name": "Section 9", "section_content": "Leave this too."},
        ],
    }

    updated, applied, rejected = everything.apply_block_operations_to_draft(
        draft,
        [
            {"op": "edit_block", "section_id": "1", "block_id": "b_0001", "text": "New text."},
            {"op": "delete_block", "section_id": "1", "block_id": "b_0001"},
            {"op": "move_block", "section_id": "1", "block_id": "b_0001", "target_section_id": "9", "after_block_id": ""},
        ],
    )

    assert applied == []
    assert [item["reason"] for item in rejected] == ["section_has_no_blocks"] * 3
    assert updated["sections"][0]["section_content"] == "Keep this body."
    assert updated["sections"][1]["section_content"] == "Leave this too."


def test_add_block_rejected_against_content_only_section_preserves_content():
    draft = {
        "title": "Draft",
        "sections": [
            {"section_id": "1", "section_name": "Intro", "section_content": "Existing body."},
        ],
    }

    updated, applied, rejected = everything.apply_block_operations_to_draft(
        draft,
        [{"op": "add_block", "section_id": "1", "after_block_id": "", "block_type": "paragraph", "text": "New paragraph."}],
    )

    assert applied == []
    assert rejected == [{"op": "add_block", "reason": "section_has_no_blocks", "section_id": "1", "after_block_id": ""}]
    assert updated["sections"][0]["section_content"] == "Existing body."
    assert "blocks" not in updated["sections"][0]


def test_move_block_rejected_into_content_only_target_preserves_both_sections():
    draft = {
        "title": "Draft",
        "sections": [
            _block_operation_sample_draft()["sections"][0],
            {"section_id": "2", "section_name": "Argument", "section_content": "Target body."},
        ],
    }

    updated, applied, rejected = everything.apply_block_operations_to_draft(
        draft,
        [{"op": "move_block", "section_id": "1", "block_id": "b_0001", "target_section_id": "2", "after_block_id": ""}],
    )

    assert applied == []
    assert rejected == [{
        "op": "move_block",
        "reason": "target_section_has_no_blocks",
        "section_id": "1",
        "target_section_id": "2",
        "block_id": "b_0001",
        "after_block_id": "",
    }]
    assert updated["sections"][0]["section_content"] == "First paragraph.\n\nSecond paragraph."
    assert updated["sections"][1]["section_content"] == "Target body."


def test_add_block_allowed_for_empty_no_block_section():
    draft = {
        "title": "Draft",
        "sections": [
            {"section_id": "1", "section_name": "Notes", "section_content": ""},
        ],
    }

    updated, applied, rejected = everything.apply_block_operations_to_draft(
        draft,
        [{"op": "add_block", "section_id": "1", "after_block_id": "", "block_type": "paragraph", "text": "New paragraph."}],
    )

    assert rejected == []
    assert applied == [{"op": "add_block", "section_id": "1", "block_id": "b_0001", "after_block_id": ""}]
    assert updated["sections"][0]["blocks"] == [
        {"block_id": "b_0001", "type": "paragraph", "text": "New paragraph.", "paragraph_index": 1, "paragraph_number": None}
    ]
    assert updated["sections"][0]["section_content"] == "New paragraph."


def test_normalize_intent_decision_answer_only_no_files():
    decision = everything._normalize_intent_decision(
        {
            "draft_action": "answer_only",
            "selected_files": [],
            "context_policy": "refresh",
            "needs_confirmation": False,
            "confirmation_reason": None,
        },
        matter_files=[],
        previous_chat_files=[],
        last_new_chat_files=[],
    )

    assert decision["draft_action"] == "answer_only"
    assert decision["selected_files"] == []
    assert decision["context_policy"] == "none"
    assert decision["use_uploaded_files"] is False
    assert everything._derive_legacy_intent_tuple(decision) == (
        False,
        False,
        "all_matter_and_chat",
        "relevant",
        [],
        [],
        False,
        False,
        [],
    )


def test_normalize_intent_decision_revise_uploaded_draft_primary_verbatim():
    decision = everything._normalize_intent_decision(
        {
            "draft_action": "revise_uploaded_draft",
            "revision_mode": "preserve_original",
            "selected_files": [
                {"file_id": "draft-file", "role": "primary_draft", "method": "verbatim"},
                {"file_id": "support-file", "role": "supporting_context", "method": "retrieve"},
            ],
            "context_policy": "refresh",
            "needs_confirmation": True,
            "confirmation_reason": "Existing draft may be overwritten.",
        },
        matter_files=[],
        previous_chat_files=[],
        last_new_chat_files=[
            {"name": "Draft.pdf", "unique_id": "draft-file"},
            {"name": "Support.pdf", "unique_id": "support-file"},
        ],
    )

    assert decision["draft_action"] == "revise_uploaded_draft"
    assert decision["revision_mode"] == "preserve_original"
    assert decision["file_selection"]["primary_draft_file_id"] == "draft-file"
    assert decision["verbatim_file_unique_ids"] == ["draft-file"]
    assert decision["target_file_unique_ids"] == ["draft-file", "support-file"]
    assert decision["file_scope"] == "recent_chat_files"
    assert decision["needs_confirmation"] is True
    assert decision["file_selection"]["selected_files"][0] == {
        "file_id": "draft-file",
        "scope": "current_prompt_attachment",
        "role": "primary_draft",
        "method": "verbatim",
    }


def test_augment_query_with_clarification_embeds_note_and_reason():
    augmented = everything._augment_query_with_clarification(
        "Ensure citations follow bluebook format",
        "just check brief.pdf and report, don't edit",
        "How should I handle this draft request?",
    )
    assert "Ensure citations follow bluebook format" in augmented
    assert "just check brief.pdf and report, don't edit" in augmented
    assert "How should I handle this draft request?" in augmented
    # Emphatic banner present and placed after the original query (query first, then clarification).
    assert "USER'S CONFIRMED CLARIFICATION" in augmented
    assert augmented.index("Ensure citations follow bluebook format") < augmented.index("USER'S CONFIRMED CLARIFICATION")


def test_augment_query_with_clarification_handles_empty_query_and_whitespace_note():
    augmented = everything._augment_query_with_clarification("", "   fix it   ", "how to proceed")
    assert augmented.startswith(">>> USER'S CONFIRMED CLARIFICATION")  # no leading blank lines when query is empty
    assert "fix it" in augmented
    assert "   fix it   " not in augmented  # note is trimmed


def test_confirmation_clarification_prefers_note_then_label():
    # Typed note wins.
    assert everything._confirmation_clarification(
        {"reclassify_note": "just check brief.pdf", "chosen_label": "Revise X"}
    ) == "just check brief.pdf"
    # Picked option: label is phrased authoritatively.
    labeled = everything._confirmation_clarification(
        {"chosen_label": "Check citations in Hallucinated Brief.pdf and report only"}
    )
    assert labeled.startswith("Check citations in Hallucinated Brief.pdf and report only")
    assert "proceed with this" in labeled
    # Neither -> empty (caller falls back to applying the fixed decision).
    assert everything._confirmation_clarification({"draft_action": "verify_draft"}) == ""
    assert everything._confirmation_clarification({}) == ""


def test_build_confirmation_request_includes_action_and_file_options():
    decision = everything._normalize_intent_decision(
        {
            "draft_action": "revise_uploaded_draft",
            "selected_files": [
                {"file_id": "draft-file", "role": "primary_draft", "method": "verbatim"},
            ],
            "context_policy": "refresh",
            "needs_confirmation": True,
            "confirmation_reason": "Uploaded draft or current draft?",
        },
        matter_files=[],
        previous_chat_files=[],
        last_new_chat_files=[{"name": "Draft.pdf", "unique_id": "draft-file"}],
    )

    confirmation = everything.build_confirmation_request(
        workflow_run_id="wf-1",
        intent_decision=decision,
        current_draft={"title": "Current", "sections": [{"section_id": "1"}]},
        matter_files=[],
        previous_chat_files=[],
        last_new_chat_files=[{"name": "Draft.pdf", "unique_id": "draft-file"}],
    )

    assert confirmation["workflow_run_id"] == "wf-1"
    assert confirmation["reason"] == "Uploaded draft or current draft?"
    option_ids = [option["id"] for option in confirmation["options"]]
    # Recommended (first) option matches the planner's chosen draft_action.
    assert option_ids[0] == "revise_uploaded_draft:draft-file"
    assert confirmation["options"][0]["draft_action"] == "revise_uploaded_draft"
    assert confirmation["options"][0]["primary_draft_file_id"] == "draft-file"
    assert "Draft.pdf" in confirmation["options"][0]["label"]
    # Report-only and the other routing choices are all offered.
    assert "verify_draft" in option_ids
    assert "edit_current_draft" in option_ids
    assert "create_new_draft" in option_ids


def test_build_confirmation_request_multi_attachment_offers_report_and_per_file():
    files = [
        {"name": "COMPLAINT.pdf", "unique_id": "complaint-id"},
        {"name": "brief.pdf", "unique_id": "brief-id"},
    ]
    decision = everything._normalize_intent_decision(
        {
            "draft_action": "verify_draft",
            "selected_files": [
                {"file_id": "complaint-id", "role": "citation_source", "method": "retrieve"},
                {"file_id": "brief-id", "role": "citation_source", "method": "retrieve"},
            ],
            "context_policy": "refresh",
            "needs_confirmation": True,
            "confirmation_reason": "Just report on citations, or fix them in a file?",
        },
        matter_files=[],
        previous_chat_files=[],
        last_new_chat_files=files,
    )

    confirmation = everything.build_confirmation_request(
        workflow_run_id="wf-2",
        intent_decision=decision,
        current_draft={"title": "", "sections": []},
        matter_files=[],
        previous_chat_files=[],
        last_new_chat_files=files,
    )

    options = confirmation["options"]
    option_ids = [option["id"] for option in options]
    # Recommended (first) option is the report-only choice matching the planner action.
    assert options[0]["id"] == "verify_draft"
    assert options[0]["draft_action"] == "verify_draft"
    # One revise option per candidate attachment, labeled by filename.
    assert "revise_uploaded_draft:complaint-id" in option_ids
    assert "revise_uploaded_draft:brief-id" in option_ids
    revise_labels = " ".join(
        option["label"] for option in options if option["draft_action"] == "revise_uploaded_draft"
    )
    assert "COMPLAINT.pdf" in revise_labels
    assert "brief.pdf" in revise_labels


def test_normalize_confirmation_options_validates_dedupes_and_injects_no_edit():
    raw = [
        {"label": "Revise A", "description": "Fix A.", "draft_action": "revise_uploaded_draft", "primary_draft_file_id": "A", "selected_file_ids": ["A"]},
        {"label": "Revise A again", "draft_action": "revise_uploaded_draft", "primary_draft_file_id": "A"},   # dup -> dropped
        {"label": "Revise ghost", "draft_action": "revise_uploaded_draft", "primary_draft_file_id": "ZZZ"},   # bad id -> dropped
        {"label": "Edit current", "draft_action": "edit_current_draft"},                                       # dropped (no workspace)
        {"label": "Create from B", "draft_action": "create_new_draft", "selected_file_ids": ["B", "ZZZ"]},     # ZZZ filtered out
        {"label": "Bad action", "draft_action": "frobnicate"},                                                 # dropped
        {"label": "", "draft_action": "verify_draft"},                                                         # empty label -> dropped
    ]
    opts = everything._normalize_confirmation_options(raw, available_ids={"A", "B"}, has_current_draft=False)
    keys = [(o["draft_action"], o["primary_draft_file_id"]) for o in opts]
    assert keys.count(("revise_uploaded_draft", "A")) == 1                  # deduped
    assert ("create_new_draft", None) in keys
    assert not any(action == "edit_current_draft" for action, _ in keys)    # dropped (no workspace draft)
    assert not any(primary == "ZZZ" for _, primary in keys)                 # bad revise target dropped
    create_opt = next(o for o in opts if o["draft_action"] == "create_new_draft")
    assert create_opt["selected_file_ids"] == ["B"]                          # unknown id filtered
    assert any(o["draft_action"] in ("answer_only", "verify_draft") for o in opts)  # no-edit injected


def test_normalize_confirmation_options_keeps_edit_current_with_workspace_draft():
    opts = everything._normalize_confirmation_options(
        [
            {"label": "Edit the current draft", "draft_action": "edit_current_draft"},
            {"label": "Just check and report", "draft_action": "verify_draft"},
        ],
        available_ids=set(),
        has_current_draft=True,
    )
    assert any(o["draft_action"] == "edit_current_draft" for o in opts)


def test_build_confirmation_request_uses_llm_proposed_options():
    files = [
        {"name": "COMPLAINT.pdf", "unique_id": "complaint-id"},
        {"name": "Hallucinated Brief.pdf", "unique_id": "brief-id"},
    ]
    decision = everything._normalize_intent_decision(
        {
            "draft_action": "verify_draft",
            "needs_confirmation": True,
            "confirmation_reason": "Report, or fix in which file?",
            "confirmation_options": [
                {"label": "Check citations and report (no edits)", "description": "Report only.",
                 "draft_action": "verify_draft", "selected_file_ids": ["complaint-id", "brief-id"]},
                {"label": "Revise COMPLAINT.pdf in place", "description": "Fix citations in the complaint.",
                 "draft_action": "revise_uploaded_draft", "primary_draft_file_id": "complaint-id", "selected_file_ids": ["complaint-id"]},
                {"label": "Revise Hallucinated Brief.pdf in place", "description": "Fix citations in the brief.",
                 "draft_action": "revise_uploaded_draft", "primary_draft_file_id": "brief-id", "selected_file_ids": ["brief-id"]},
            ],
            "selected_files": [],
            "context_policy": "none",
        },
        matter_files=[],
        previous_chat_files=files,
        last_new_chat_files=[],
    )

    confirmation = everything.build_confirmation_request(
        workflow_run_id="wf-3",
        intent_decision=decision,
        current_draft={"title": "", "sections": []},
        matter_files=[],
        previous_chat_files=files,
        last_new_chat_files=[],
    )

    options = confirmation["options"]
    ids = [o["id"] for o in options]
    labels = [o["label"] for o in options]
    assert "Check citations and report (no edits)" in labels      # LLM label, not the hardcoded one
    assert "revise_uploaded_draft:complaint-id" in ids
    assert "revise_uploaded_draft:brief-id" in ids
    assert options[0]["draft_action"] == "verify_draft"           # recommended first
    # LLM-authored descriptions flow into the rendered question options.
    q_opts = confirmation["questions"][0]["options"]
    assert any(o.get("description") == "Fix citations in the complaint." for o in q_opts)


def test_apply_confirmed_workflow_decision_preserves_revision_mode():
    original = everything._normalize_intent_decision(
        {
            "draft_action": "revise_uploaded_draft",
            "revision_mode": "preserve_original",
            "selected_files": [
                {"file_id": "draft-file", "role": "primary_draft", "method": "verbatim"},
            ],
            "context_policy": "refresh",
        },
        matter_files=[],
        previous_chat_files=[],
        last_new_chat_files=[{"name": "Draft.pdf", "unique_id": "draft-file"}],
    )

    confirmed = everything.apply_confirmed_workflow_decision(
        original,
        {
            "draft_action": "revise_uploaded_draft",
            "primary_draft_file_id": "draft-file",
            "selected_file_ids": ["draft-file"],
        },
        matter_files=[],
        previous_chat_files=[],
        last_new_chat_files=[{"name": "Draft.pdf", "unique_id": "draft-file"}],
    )

    assert confirmed["revision_mode"] == "preserve_original"


def test_build_confirmation_request_omits_edit_option_for_empty_workspace_draft():
    decision = everything._normalize_intent_decision(
        {
            "draft_action": "revise_uploaded_draft",
            "selected_files": [
                {"file_id": "draft-file", "role": "primary_draft", "method": "verbatim"},
            ],
            "context_policy": "refresh",
            "needs_confirmation": True,
        },
        matter_files=[],
        previous_chat_files=[{"name": "Draft.pdf", "unique_id": "draft-file"}],
        last_new_chat_files=[],
    )

    empty_confirmation = everything.build_confirmation_request(
        workflow_run_id="wf-1",
        intent_decision=decision,
        current_draft={"title": "", "sections": []},
        matter_files=[],
        previous_chat_files=[{"name": "Draft.pdf", "unique_id": "draft-file"}],
        last_new_chat_files=[],
    )
    real_confirmation = everything.build_confirmation_request(
        workflow_run_id="wf-1",
        intent_decision=decision,
        current_draft={"title": "Current", "sections": [{"section_name": "Facts"}]},
        matter_files=[],
        previous_chat_files=[{"name": "Draft.pdf", "unique_id": "draft-file"}],
        last_new_chat_files=[],
    )

    assert "edit_current_draft" not in [option["id"] for option in empty_confirmation["options"]]
    assert "edit_current_draft" in [option["id"] for option in real_confirmation["options"]]


def test_apply_confirmed_workflow_decision_forces_primary_draft_verbatim():
    original = everything._normalize_intent_decision(
        {
            "draft_action": "create_new_draft",
            "selected_files": [
                {"file_id": "draft-file", "role": "supporting_context", "method": "retrieve"},
                {"file_id": "support-file", "role": "supporting_context", "method": "retrieve"},
            ],
            "context_policy": "refresh",
        },
        matter_files=[],
        previous_chat_files=[],
        last_new_chat_files=[
            {"name": "Draft.pdf", "unique_id": "draft-file"},
            {"name": "Support.pdf", "unique_id": "support-file"},
        ],
    )

    confirmed = everything.apply_confirmed_workflow_decision(
        original,
        {
            "draft_action": "revise_uploaded_draft",
            "primary_draft_file_id": "draft-file",
            "selected_file_ids": ["support-file"],
        },
        matter_files=[],
        previous_chat_files=[],
        last_new_chat_files=[
            {"name": "Draft.pdf", "unique_id": "draft-file"},
            {"name": "Support.pdf", "unique_id": "support-file"},
        ],
    )

    assert confirmed["draft_action"] == "revise_uploaded_draft"
    assert confirmed["file_selection"]["primary_draft_file_id"] == "draft-file"
    assert confirmed["verbatim_file_unique_ids"] == ["draft-file"]
    assert confirmed["selected_files"][0]["role"] == "primary_draft"
    assert confirmed["selected_files"][0]["method"] == "verbatim"


def test_normalize_intent_decision_verify_draft_reads_selected_files_verbatim():
    decision = everything._normalize_intent_decision(
        {
            "draft_action": "verify_draft",
            "selected_files": [
                {"file_id": "brief-file", "role": "supporting_context", "method": "retrieve"},
            ],
            "context_policy": "refresh",
        },
        matter_files=[],
        previous_chat_files=[{"name": "Brief.pdf", "unique_id": "brief-file"}],
        last_new_chat_files=[],
    )

    # Checking citations needs the document text, not BM25 retrieve.
    assert decision["selected_files"][0]["method"] == "verbatim"
    assert decision["verbatim_file_unique_ids"] == ["brief-file"]
    assert decision["use_uploaded_files"] is True
    assert decision["refresh_context"] is True


def test_apply_confirmed_workflow_decision_verify_draft_uses_verbatim():
    # Mirrors the logged failure: planner deferred (empty selected_files), the user picked a
    # report-only verify_draft option; the resolved file must be read verbatim, not retrieve.
    original = everything._normalize_intent_decision(
        {
            "draft_action": "verify_draft",
            "needs_confirmation": True,
            "confirmation_reason": "Report or fix, and which file?",
            "selected_files": [],
            "context_policy": "none",
        },
        matter_files=[],
        previous_chat_files=[{"name": "Hallucinated Brief.pdf", "unique_id": "brief-file"}],
        last_new_chat_files=[],
    )

    confirmed = everything.apply_confirmed_workflow_decision(
        original,
        {
            "draft_action": "verify_draft",
            "primary_draft_file_id": None,
            "selected_file_ids": ["brief-file"],
        },
        matter_files=[],
        previous_chat_files=[{"name": "Hallucinated Brief.pdf", "unique_id": "brief-file"}],
        last_new_chat_files=[],
    )

    assert confirmed["draft_action"] == "verify_draft"
    assert confirmed["selected_files"][0]["file_id"] == "brief-file"
    assert confirmed["selected_files"][0]["method"] == "verbatim"
    assert confirmed["verbatim_file_unique_ids"] == ["brief-file"]
    assert confirmed["use_uploaded_files"] is True


def test_normalize_intent_decision_maps_legacy_methods_to_relevant():
    decision = everything._normalize_intent_decision(
        {
            "draft_action": "answer_only",
            "selected_files": [
                {"file_id": "f1", "role": "supporting_context", "method": "full_content"},
                {"file_id": "f2", "role": "background_context", "method": "retrieve"},
                {"file_id": "f3", "role": "supporting_context", "method": "bogus"},
            ],
            "context_policy": "refresh",
        },
        matter_files=[],
        previous_chat_files=[
            {"name": "F1.pdf", "unique_id": "f1"},
            {"name": "F2.pdf", "unique_id": "f2"},
            {"name": "F3.pdf", "unique_id": "f3"},
        ],
        last_new_chat_files=[],
    )
    assert {f["method"] for f in decision["selected_files"]} == {"relevant"}
    assert decision["extraction_method"] == "relevant"
    assert decision["verbatim_file_unique_ids"] == []


def test_sample_begin_middle_end_samples_when_over_cap(monkeypatch):
    # Deterministic token counting: 1 token per word.
    monkeypatch.setattr(everything, "num_tokens_from_string", lambda s: len((s or "").split()))
    chunks = [{"chunk_index": i, "content": f"BEGINMARK_{i} w w"} for i in range(6)]
    chunks[-1]["content"] = "w w ENDMARK"  # known tail

    # Under cap -> whole content unchanged (no dots).
    whole = everything._sample_begin_middle_end(chunks, token_cap=1000)
    assert everything.RELEVANT_EXTRACT_DOTS not in whole
    assert "ENDMARK" in whole

    # Over cap -> sampled begin+middle+end with dotted gaps; tail preserved; within cap.
    sampled = everything._sample_begin_middle_end(chunks, token_cap=9)
    assert everything.RELEVANT_EXTRACT_DOTS in sampled
    assert "BEGINMARK_0" in sampled            # beginning kept
    assert "ENDMARK" in sampled                # tail not dropped
    assert len(sampled.split()) <= 9 + 6       # roughly within cap (+ small slack)


def test_extract_one_relevant_file_preserves_verbatim_excerpts(monkeypatch):
    monkeypatch.setattr(
        everything,
        "chat_completion_parse",
        _mock_chat_parse(lambda prompt: '{"file_name":"Brief.pdf","file_id":"f1","file_scope":"matter_files",'
                         '"relevant_excerpts":"Smith v. Jones, 12 F.3d 1 (9th Cir. 1994).","source_anchors":["Section II"]}'),
    )
    res = everything._extract_one_relevant_file(
        "check citations",
        {"file_id": "f1", "file_name": "Brief.pdf", "file_scope": "matter_files",
         "chunks": [{"chunk_index": 0, "content": "Plaintiff cites Smith v. Jones, 12 F.3d 1 (9th Cir. 1994)."}]},
    )
    assert res["relevant_excerpts"] == "Smith v. Jones, 12 F.3d 1 (9th Cir. 1994)."
    assert res["source_anchors"] == ["Section II"]
    assert res["file_id"] == "f1"


def test_build_relevant_extraction_per_file_no_bm25(monkeypatch):
    monkeypatch.setattr(
        everything,
        "_build_full_content_file_entries",
        lambda **kwargs: [
            {"file_id": "f1", "file_name": "A.pdf", "file_scope": "matter_files",
             "chunks": [{"chunk_index": 0, "content": "alpha"}]},
            {"file_id": "f2", "file_name": "B.pdf", "file_scope": "matter_files",
             "chunks": [{"chunk_index": 0, "content": "beta"}]},
        ],
    )
    monkeypatch.setattr(
        everything,
        "chat_completion_parse",
        _mock_chat_parse(lambda prompt: '{"relevant_excerpts":"excerpt for ' + ("A" if "A.pdf" in prompt else "B")
                         + '","source_anchors":["p1"]}'),
    )
    bm25_called = []
    monkeypatch.setattr(everything, "get_relevant_content_bm25", lambda *a, **k: bm25_called.append(True) or "")

    combined, cache_key = everything.build_relevant_extraction(
        user_message="q", draft_id=1, project_id=None, state={},
        file_scope="all_chat_files", file_unique_ids=["f1", "f2"],
    )
    assert "[relevant_excerpts]:" in combined
    assert "excerpt for A" in combined and "excerpt for B" in combined
    assert cache_key.startswith("relevant_extract_v1")
    assert bm25_called == []  # no BM25 in the relevant path


def test_normalize_intent_decision_filters_unknown_file_ids():
    decision = everything._normalize_intent_decision(
        {
            "draft_action": "answer_only",
            "selected_files": [
                {"file_id": "unknown-file", "role": "supporting_context", "method": "retrieve"},
                {"file_id": "known-file", "role": "supporting_context", "method": "retrieve"},
            ],
            "context_policy": "refresh",
        },
        matter_files=[{"name": "Known.pdf", "unique_id": "known-file"}],
        previous_chat_files=[],
        last_new_chat_files=[],
    )

    assert [f["file_id"] for f in decision["selected_files"]] == ["known-file"]
    assert decision["file_selection"]["selected_files"][0]["scope"] == "matter_background"
    assert decision["target_file_unique_ids"] == ["known-file"]


def test_build_available_file_preview_includes_ready_metadata_only():
    preview = everything._build_available_file_preview(
        "Files",
        [
            {
                "name": "Ready.pdf",
                "unique_id": "ready-id",
                "metadata": {
                    "status": "ready",
                    "summary": "A short summary.",
                    "document_kind": "report",
                },
            },
            {
                "name": "Pending.pdf",
                "unique_id": "pending-id",
                "metadata": {
                    "status": "pending",
                    "summary": "Should not appear.",
                    "document_kind": "memo",
                },
            },
        ],
        "current_prompt_attachment",
        True,
    )

    cards = [json.loads(line) for line in preview.splitlines()[1:]]
    assert cards[0]['metadata_status'] == 'ready' and cards[0]['document_kind'] == 'report'
    assert cards[0]['preview'] == 'A short summary.'
    assert cards[1]['metadata_status'] == 'pending'
    assert "Should not appear" not in preview


def test_classify_intent_decision_prompt_orders_context_by_priority(monkeypatch):
    captured = {}

    def _fake_chat_completion_j(prompt, model="gpt-5.4", token_counter=None):
        captured["prompt"] = prompt
        return """{
            "draft_action": "answer_only",
            "selected_files": [],
            "context_policy": "none",
            "needs_confirmation": false,
            "confirmation_reason": null
        }"""

    monkeypatch.setattr(everything, "chat_completion_parse", _mock_chat_parse(_fake_chat_completion_j))

    cached_context = "cached-start " + ("middle " * 80) + "cached-end"
    decision = everything.classify_intent_decision(
        state={},
        user_message="Please summarize this attached document.",
        current_draft={
            "title": "Workspace Draft",
            "sections": [{"section_name": "Facts"}],
        },
        selected_section="Facts",
        matter_files=[{
            "name": "Matter.pdf",
            "unique_id": "matter-id",
            "metadata": {
                "status": "ready",
                "summary": "Matter background summary.",
                "document_kind": "report",
            },
        }],
        previous_chat_files=[{
            "name": "Prior.pdf",
            "unique_id": "prior-id",
            "metadata": {"status": "missing"},
        }],
        last_new_chat_files=[{
            "name": "Current.pdf",
            "unique_id": "current-id",
            "metadata": {"status": "pending"},
        }],
        file_extractions_str=cached_context,
        previous_user_questions="What is the procedural posture?",
    )

    prompt = captured["prompt"]
    section_order = [
        "==== Current workspace draft context:",
        "==== Matter-level background files:",
        "==== Prior prompt attachment files:",
        "==== Current prompt attachment files:",
        "==== Attachment resolution:",
        "==== Previous user questions (if any):",
        "==== Cached extracted context:",
        "==== Current user message:",
        "Your task is to classify the user's intent",
    ]
    assert [prompt.index(section) for section in section_order] == sorted(
        prompt.index(section) for section in section_order
    )
    # Unified two-method extraction: verbatim | relevant (full_content/retrieve removed).
    assert '"method": "verbatim|relevant"' in prompt
    assert '"method": "verbatim|retrieve|full_content"' not in prompt
    assert "- \"relevant\": use for topic/fact/clause-specific questions" in prompt
    # Planner weighs the context cost of verbatim without a hard default toward relevant.
    assert "consumes far more of the model's context window" in prompt
    assert "Reach for \"verbatim\" only when the whole/exact text is genuinely needed" in prompt
    assert "Priority: broad background." in prompt
    assert "Priority: medium/recent." in prompt
    assert "Priority: highest for \"this file\"" in prompt
    assert "First look for a matching current prompt attachment." in prompt
    assert "look for a recent/prior prompt attachment" in prompt
    assert "Use current attachments over prior attachments" in prompt
    assert "Do not use rigid keyword-only rules." in prompt
    assert "Draft-routing rules:" in prompt
    assert "This replaces the old create_new_draft/change_draft_requested booleans" in prompt
    assert "If selected_section is a real section name" in prompt
    assert "choose \"edit_current_draft\"" in prompt
    assert "Use \"revise_uploaded_draft\" when the user asks to revise, redraft, update, or improve" in prompt
    assert "**Revision mode**" in prompt
    assert "\"preserve_original\": preserve the uploaded document as the source text" in prompt
    assert "correct citations without rewriting" in prompt
    assert "uploaded files are only reference/supporting material" in prompt
    # Confirmation block: bias-toward-confirming guidance and folded ambiguity rules.
    assert "**Confirmation (bias toward confirming when genuinely ambiguous)**" in prompt
    assert "Action-mode ambiguity:" in prompt
    assert "bluebook format" in prompt
    assert "Multi-attachment document work:" in prompt
    assert "Revise-vs-create ambiguity: the request could revise the uploaded document itself OR create a new draft using it as reference." in prompt
    assert "verify_draft produces a report and does not edit the document" in prompt
    # LLM-proposed confirmation options instruction + schema field.
    assert "**Confirmation options (fill confirmation_options ONLY when needs_confirmation is true" in prompt
    assert "include a SEPARATE option for EACH candidate document and name that document explicitly" in prompt
    assert '"confirmation_options"' in prompt
    # Always answer completely (selected_files + context) even when confirming; verbatim is a
    # task-driven method usable for non-primary references.
    assert "Always populate selected_files (with role and method) and context_policy for your single recommended action" in prompt
    assert "answering paragraph-by-paragraph to an uploaded complaint reads that complaint \"verbatim\"" in prompt
    # The guardrail for empty-workspace + single clear attachment is preserved.
    assert "proceed without confirmation merely because an empty workspace draft object exists" in prompt
    # Output schema decides confirmation right after the action, before file selection.
    json_section = prompt.split("Output your answer in the following JSON format")[1]
    assert (
        json_section.index('"draft_action"')
        < json_section.index('"needs_confirmation"')
        < json_section.index('"revision_mode"')
    )
    assert json_section.index('"confirmation_reason"') < json_section.index('"selected_files"')
    assert "Cached context may be stale." in prompt
    assert "Do not select files primarily from cached text when current prompt attachments are present." in prompt
    assert "cached_context_available: true" in prompt
    assert f"cached_context_length: {len(cached_context)}" in prompt
    assert "Previously extracted context available" not in prompt
    assert decision["draft_action"] == "answer_only"


def test_classify_intent_decision_selects_current_attachment_for_revise_this(monkeypatch):
    def _fake_chat_completion_j(prompt, model="gpt-5.4", token_counter=None):
        return """{
            "draft_action": "revise_uploaded_draft",
            "selected_files": [
                {"file_id": "current-file", "role": "primary_draft", "method": "verbatim"}
            ],
            "context_policy": "refresh",
            "needs_confirmation": false,
            "confirmation_reason": null
        }"""

    monkeypatch.setattr(everything, "chat_completion_parse", _mock_chat_parse(_fake_chat_completion_j))

    decision = everything.classify_intent_decision(
        state={},
        user_message="Please revise this.",
        current_draft={"title": "", "sections": []},
        selected_section="Not Specified",
        matter_files=[],
        previous_chat_files=[{"name": "Prior.pdf", "unique_id": "prior-file"}],
        last_new_chat_files=[{"name": "Current.pdf", "unique_id": "current-file"}],
        file_extractions_str="",
        previous_user_questions="",
    )

    assert decision["draft_action"] == "revise_uploaded_draft"
    assert decision["file_selection"]["primary_draft_file_id"] == "current-file"
    assert decision["selected_files"][0] == {
        "file_id": "current-file",
        "scope": "current_prompt_attachment",
        "role": "primary_draft",
        "method": "verbatim",
    }


def test_classify_intent_decision_prompt_treats_empty_draft_as_no_current_draft(monkeypatch):
    captured = {}

    def _fake_chat_completion_j(prompt, model="gpt-5.4", token_counter=None):
        captured["prompt"] = prompt
        return """{
            "draft_action": "revise_uploaded_draft",
            "selected_files": [
                {"file_id": "draft-file", "role": "primary_draft", "method": "verbatim"}
            ],
            "context_policy": "refresh",
            "needs_confirmation": false,
            "confirmation_reason": null
        }"""

    monkeypatch.setattr(everything, "chat_completion_parse", _mock_chat_parse(_fake_chat_completion_j))

    decision = everything.classify_intent_decision(
        state={},
        user_message="Please revise the uploaded draft.",
        current_draft={"title": "", "sections": []},
        selected_section="Not Specified",
        matter_files=[],
        previous_chat_files=[{"name": "Draft.pdf", "unique_id": "draft-file"}],
        last_new_chat_files=[],
        file_extractions_str="",
        previous_user_questions="",
    )

    prompt = captured["prompt"]
    assert "has_current_draft: False" in prompt
    assert "current_draft_title: No draft title" in prompt
    assert "default empty draft state do not count" in prompt
    assert "do not ask for confirmation merely because an empty workspace draft object exists" in prompt
    assert decision["draft_action"] == "revise_uploaded_draft"
    assert decision["needs_confirmation"] is False


def test_classify_intent_decision_keeps_create_new_when_prior_file_is_reference(monkeypatch):
    def _fake_chat_completion_j(prompt, model="gpt-5.4", token_counter=None):
        return """{
            "draft_action": "create_new_draft",
            "selected_files": [
                {"file_id": "prior-file", "role": "supporting_context", "method": "retrieve"}
            ],
            "context_policy": "refresh",
            "needs_confirmation": false,
            "confirmation_reason": null
        }"""

    monkeypatch.setattr(everything, "chat_completion_parse", _mock_chat_parse(_fake_chat_completion_j))

    decision = everything.classify_intent_decision(
        state={},
        user_message="Draft a new brief using that file as reference.",
        current_draft={"title": "", "sections": []},
        selected_section="Not Specified",
        matter_files=[],
        previous_chat_files=[{"name": "Prior.pdf", "unique_id": "prior-file"}],
        last_new_chat_files=[],
        file_extractions_str="",
        previous_user_questions="",
    )

    assert decision["draft_action"] == "create_new_draft"
    assert decision["file_selection"]["primary_draft_file_id"] is None
    # Legacy "retrieve" method is normalized to the unified "relevant".
    assert decision["selected_files"][0] == {
        "file_id": "prior-file",
        "scope": "prior_prompt_attachment",
        "role": "supporting_context",
        "method": "relevant",
    }


def test_classify_intent_decision_marks_multiple_prior_revise_candidates_ambiguous(monkeypatch):
    def _fake_chat_completion_j(prompt, model="gpt-5.4", token_counter=None):
        return """{
            "draft_action": "revise_uploaded_draft",
            "selected_files": [
                {"file_id": "prior-a", "role": "primary_draft", "method": "verbatim"},
                {"file_id": "prior-b", "role": "primary_draft", "method": "verbatim"}
            ],
            "context_policy": "refresh",
            "needs_confirmation": true,
            "confirmation_reason": "Multiple recent attachments could be the uploaded document to revise."
        }"""

    monkeypatch.setattr(everything, "chat_completion_parse", _mock_chat_parse(_fake_chat_completion_j))

    decision = everything.classify_intent_decision(
        state={},
        user_message="Revise the uploaded document.",
        current_draft={"title": "", "sections": []},
        selected_section="Not Specified",
        matter_files=[],
        previous_chat_files=[
            {"name": "Prior A.pdf", "unique_id": "prior-a"},
            {"name": "Prior B.pdf", "unique_id": "prior-b"},
        ],
        last_new_chat_files=[],
        file_extractions_str="",
        previous_user_questions="",
    )

    assert decision["draft_action"] == "revise_uploaded_draft"
    assert decision["needs_confirmation"] is True
    assert decision["file_selection"]["primary_draft_file_id"] == "prior-a"
    assert decision["selected_files"][0]["role"] == "primary_draft"
    assert decision["selected_files"][1]["role"] == "supporting_context"
    assert decision["selected_files"][1]["method"] == "verbatim"


def test_preserve_original_segmentation_and_target_map_preserve_order_and_signals():
    original = (
        "INTRODUCTION\n\n"
        "Plaintiff cites Smith v. Jones, 12 F.3d 1 (9th Cir. 1994).\n\n"
        "ARGUMENT\n\n"
        "The requested damages are $5,000 and were calculated in 2024."
    )

    blocks = everything.segment_preserve_original_text(original)
    target_map = everything.build_smart_target_map(blocks)

    assert "".join(block["text"] for block in blocks) == original
    # Headings are no longer specially tagged; every non-blank part is a paragraph anchor.
    assert [block["anchor"] for block in blocks if block["kind"] != "blank"] == [
        "p_0001",
        "p_0002",
        "p_0003",
        "p_0004",
    ]
    citation_preview = next(item for item in target_map if item["anchor"] == "p_0002")
    damages_preview = next(item for item in target_map if item["anchor"] == "p_0004")
    assert citation_preview["signals"]["has_citation"] is True
    assert damages_preview["signals"]["has_amount"] is True
    assert damages_preview["signals"]["has_date"] is True


def test_apply_anchor_replacements_changes_only_known_nonblank_edits():
    original = "INTRODUCTION\n\nOriginal first paragraph.\n\nOriginal second paragraph."
    blocks = everything.segment_preserve_original_text(original)

    updated, rejected = everything.apply_anchor_replacements(
        blocks,
        [
            {"anchor": "p_0002", "replacement_text": "Corrected first paragraph."},
            {"anchor": "p_9999", "replacement_text": "Unknown."},
            {"anchor": "p_0003", "replacement_text": "   "},
        ],
    )

    assert "".join(block["text"] for block in updated) == (
        "INTRODUCTION\n\nCorrected first paragraph.\n\nOriginal second paragraph."
    )
    assert {"anchor": "p_9999", "reason": "unknown_anchor"} in rejected
    assert {"anchor": "p_0003", "reason": "blank_replacement"} in rejected


def test_select_preserve_original_targets_uses_target_map_not_full_document(monkeypatch):
    captured = {}

    def _fake_chat_completion_j(prompt, model="gpt-5.4", token_counter=None):
        captured["prompt"] = prompt
        return """{
            "edit_scope": "targeted_blocks",
            "anchors": ["p_0001"],
            "confidence": "high",
            "reason": "The first paragraph discusses the requested issue.",
            "needs_confirmation": false
        }"""

    monkeypatch.setattr(everything, "chat_completion_parse", _mock_chat_parse(_fake_chat_completion_j))
    target_map = [{
        "anchor": "p_0001",
        "kind": "paragraph",
        "snippet_start": "Short preview.",
        "snippet_end": "",
        "signals": {"keywords": ["standing"]},
    }]

    selection = everything.select_preserve_original_targets(
        "Only fix the standing discussion. SECRET FULL DOCUMENT TEXT should not be source content.",
        target_map,
    )

    assert selection["anchors"] == ["p_0001"]
    assert "Anchored target map:" in captured["prompt"]
    assert "Short preview." in captured["prompt"]
    assert '"text":' not in captured["prompt"]


def test_edit_preserve_original_blocks_receives_only_selected_blocks(monkeypatch):
    captured = {}

    def _fake_chat_completion_j(prompt, model="gpt-5.4", token_counter=None):
        captured["prompt"] = prompt
        return """{
            "operations": [
                {
                    "op": "edit_preserved_block",
                    "anchor": "p_0002",
                    "replacement_text": "Corrected first paragraph.",
                    "change_summary": "Corrected the first paragraph.",
                    "citation_changes": []
                }
            ],
            "response": "I have made the following changes to the current draft to the right: corrected the first paragraph."
        }"""

    monkeypatch.setattr(everything, "chat_completion_parse", _mock_chat_parse(_fake_chat_completion_j))
    blocks = everything.segment_preserve_original_text(
        "INTRODUCTION\n\nOriginal first paragraph.\n\nDo not send this untouched paragraph."
    )
    payload = everything.build_preserve_original_edit_payload(blocks, ["p_0002"])

    result = everything.edit_preserve_original_blocks("Fix the first paragraph only.", payload)

    assert result["edits"][0]["anchor"] == "p_0002"
    assert result["operations"][0]["op"] == "edit_preserved_block"
    assert "Original first paragraph." in captured["prompt"]
    assert "Do not send this untouched paragraph." not in captured["prompt"]
    assert "Start response with 'I have made the following changes to the current draft to the right:" in captured["prompt"]
    assert "each and every cited legal case used in replacement_text MUST be included" in captured["prompt"]


def test_edit_preserve_original_blocks_accepts_legacy_edits_shape(monkeypatch):
    operations, edits = everything._extract_preserve_original_edit_operations({
        "edits": [{"anchor": "p_0001", "replacement_text": "Corrected first paragraph."}],
        "response": "I preserved the uploaded draft and revised the targeted passages.",
    })

    assert edits == [{"anchor": "p_0001", "replacement_text": "Corrected first paragraph."}]
    assert operations[0]["op"] == "edit_preserved_block"
    assert operations[0]["anchor"] == "p_0001"


def test_run_preserve_original_uploaded_revision_reconstructs_full_original_with_targeted_edit(monkeypatch):
    original = (
        "INTRODUCTION\n\n"
        "Original first paragraph with incorrect citation.\n\n"
        "Original second paragraph must remain exact."
    )
    calls = []

    def _fake_load(primary_draft_file_id, *, doc_id, project_id):
        return {
            "error": False,
            "file_id": primary_draft_file_id,
            "filename": "Uploaded Draft.txt",
            "content": original,
        }

    def _fake_chat_completion_j(prompt, model="gpt-5.4", token_counter=None):
        calls.append(prompt)
        if "Do not edit yet." in prompt:
            return """{
                "edit_scope": "targeted_blocks",
                "anchors": ["p_0002"],
                "confidence": "high",
                "reason": "The first body paragraph contains the requested citation issue.",
                "needs_confirmation": false
            }"""
        return """{
            "operations": [
                {"op": "edit_preserved_block", "anchor": "p_0002",
                 "replacement_text": "Corrected first paragraph with corrected citation.",
                 "change_summary": "Corrected the citation.", "citation_changes": []}
            ],
            "response": "I preserved the uploaded draft and revised the targeted passages."
        }"""

    monkeypatch.setattr(everything, "load_primary_uploaded_original_text", _fake_load)
    monkeypatch.setattr(everything, "chat_completion_parse", _mock_chat_parse(_fake_chat_completion_j))
    monkeypatch.setattr(everything, "structure_draft_text", _passthrough_structure_draft_text)

    result = everything.run_preserve_original_uploaded_revision(
        user_message="Make a copy and only correct the citation.",
        primary_draft_file_id="draft-file",
        doc_id=1,
        project_id=None,
    )

    section_content = result["draft"]["sections"][0]["section_content"]
    assert result["error"] is False
    assert "Corrected first paragraph with corrected citation." in section_content
    assert "Original second paragraph must remain exact." in section_content
    assert "Original first paragraph with incorrect citation." not in section_content
    assert result["operations"][1]["edits_applied"] == 1
    assert len(calls) == 2


def test_run_preserve_original_uploaded_revision_synthesizes_generic_response(monkeypatch):
    original = "INTRODUCTION\n\nOriginal first paragraph with bad citation."

    def _fake_load(primary_draft_file_id, *, doc_id, project_id):
        return {
            "error": False,
            "file_id": primary_draft_file_id,
            "filename": "Uploaded Draft.txt",
            "content": original,
        }

    def _fake_chat_completion_j(prompt, model="gpt-5.4", token_counter=None):
        if "Do not edit yet." in prompt:
            return """{
                "edit_scope": "targeted_blocks",
                "anchors": ["p_0002"],
                "confidence": "high",
                "reason": "The first body paragraph contains the requested citation issue.",
                "needs_confirmation": false
            }"""
        return """{
            "operations": [
                {
                    "op": "edit_preserved_block",
                    "anchor": "p_0002",
                    "replacement_text": "Corrected first paragraph with Smith v. Jones, 12 F.3d 1 (9th Cir. 1994).",
                    "change_summary": "Corrected the bad citation in the first paragraph.",
                    "citation_changes": [
                        {
                            "original": "Smyth v. Jones, 12 F.3d 1",
                            "action": "corrected",
                            "replacement": "Smith v. Jones, 12 F.3d 1 (9th Cir. 1994)",
                            "reason": "The case name was misspelled."
                        }
                    ]
                }
            ],
            "response": "I preserved the uploaded draft and revised the targeted passages."
        }"""

    monkeypatch.setattr(everything, "load_primary_uploaded_original_text", _fake_load)
    monkeypatch.setattr(everything, "chat_completion_parse", _mock_chat_parse(_fake_chat_completion_j))
    monkeypatch.setattr(everything, "structure_draft_text", _passthrough_structure_draft_text)

    result = everything.run_preserve_original_uploaded_revision(
        user_message="Make a copy and only correct the citation.",
        primary_draft_file_id="draft-file",
        doc_id=1,
        project_id=None,
    )

    assert result["message"].startswith(everything.PRESERVE_ORIGINAL_RESPONSE_PREFIX)
    assert "Applied 1 targeted edit to the uploaded draft." in result["message"]
    assert "Corrected the bad citation in the first paragraph." in result["message"]
    assert "[[CASE_START]]Smith v. Jones, 12 F.3d 1 (9th Cir. 1994)" in result["message"]
    assert result["operations"][1]["change_summaries"] == ["Corrected the bad citation in the first paragraph."]
    assert result["operations"][1]["edits_applied"] == 1


def test_run_preserve_original_uploaded_revision_reports_rejected_edits(monkeypatch):
    original = "INTRODUCTION\n\nOriginal first paragraph."

    def _fake_load(primary_draft_file_id, *, doc_id, project_id):
        return {
            "error": False,
            "file_id": primary_draft_file_id,
            "filename": "Uploaded Draft.txt",
            "content": original,
        }

    def _fake_chat_completion_j(prompt, model="gpt-5.4", token_counter=None):
        if "Do not edit yet." in prompt:
            return """{
                "edit_scope": "targeted_blocks",
                "anchors": ["p_0002"],
                "confidence": "high",
                "reason": "The first body paragraph should be edited.",
                "needs_confirmation": false
            }"""
        return """{
            "operations": [
                {
                    "op": "edit_preserved_block",
                    "anchor": "p_0002",
                    "replacement_text": "Corrected first paragraph.",
                    "change_summary": "Corrected the first paragraph.",
                    "citation_changes": []
                },
                {
                    "op": "edit_preserved_block",
                    "anchor": "p_9999",
                    "replacement_text": "Should not apply.",
                    "change_summary": "Tried to edit an unknown paragraph.",
                    "citation_changes": []
                }
            ],
            "response": "I preserved the uploaded draft and revised the targeted passages."
        }"""

    monkeypatch.setattr(everything, "load_primary_uploaded_original_text", _fake_load)
    monkeypatch.setattr(everything, "chat_completion_parse", _mock_chat_parse(_fake_chat_completion_j))
    monkeypatch.setattr(everything, "structure_draft_text", _passthrough_structure_draft_text)

    result = everything.run_preserve_original_uploaded_revision(
        user_message="Fix the first paragraph.",
        primary_draft_file_id="draft-file",
        doc_id=1,
        project_id=None,
    )

    assert result["operations"][1]["edits_applied"] == 1
    assert result["operations"][1]["edits_rejected"] == [{"anchor": "p_9999", "reason": "unknown_anchor"}]
    assert "1 proposed edit was rejected by validation" in result["message"]
    assert "Should not apply." not in result["draft"]["sections"][0]["section_content"]


def test_run_preserve_original_uploaded_revision_structures_multiple_sections(monkeypatch):
    original = (
        "INTRODUCTION\n\n"
        "Plaintiff cites Smyth v. Jones, 12 F.3d 1.\n\n"
        "ARGUMENT\n\n"
        "The claim must be dismissed."
    )

    def _fake_load(primary_draft_file_id, *, doc_id, project_id):
        return {
            "error": False,
            "file_id": primary_draft_file_id,
            "filename": "COMPLAINT.pdf",
            "content": original,
        }

    def _fake_chat_completion_j(prompt, model="gpt-5.4", token_counter=None):
        if "Do not edit yet." in prompt:
            return """{
                "edit_scope": "targeted_blocks",
                "anchors": ["p_0002"],
                "confidence": "high",
                "reason": "Fix the citation format.",
                "needs_confirmation": false
            }"""
        return """{
            "operations": [
                {"op": "edit_preserved_block", "anchor": "p_0002",
                 "replacement_text": "Plaintiff cites Smith v. Jones, 12 F.3d 1 (9th Cir. 1994).",
                 "change_summary": "Corrected the case citation.", "citation_changes": []}
            ],
            "response": "I preserved the uploaded draft and revised the targeted passages."
        }"""

    captured = {}

    def _fake_structure(text, *, title="", model=None, reasoning_effort=None):
        captured["text"] = text
        captured["title"] = title
        return {
            "title": title,
            "sections": [
                {"section_id": "1", "section_name": "INTRODUCTION", "section_content": "INTRODUCTION"},
                {"section_id": "2", "section_name": "ARGUMENT", "section_content": "ARGUMENT"},
            ],
        }

    monkeypatch.setattr(everything, "load_primary_uploaded_original_text", _fake_load)
    monkeypatch.setattr(everything, "chat_completion_parse", _mock_chat_parse(_fake_chat_completion_j))
    monkeypatch.setattr(everything, "structure_draft_text", _fake_structure)

    result = everything.run_preserve_original_uploaded_revision(
        user_message="Revise the complaint and fix the citation format.",
        primary_draft_file_id="draft-file",
        doc_id=1,
        project_id=None,
    )

    # run_preserve now delegates reconstruction to the LLM-based structurer -> multiple sections.
    assert [s["section_name"] for s in result["draft"]["sections"]] == ["INTRODUCTION", "ARGUMENT"]
    assert result["draft"]["title"] == "COMPLAINT.pdf"
    assert captured["title"] == "COMPLAINT.pdf"
    # The edited full text handed to the structurer keeps untouched text verbatim and applies the fix.
    assert "Plaintiff cites Smith v. Jones, 12 F.3d 1 (9th Cir. 1994)." in captured["text"]
    assert "Plaintiff cites Smyth v. Jones, 12 F.3d 1." not in captured["text"]
    assert "The claim must be dismissed." in captured["text"]


def test_edit_preserve_original_blocks_returns_graceful_token_cap(monkeypatch):
    monkeypatch.setattr(
        everything,
        "num_tokens_from_string",
        lambda _text, encoding_name="cl100k_base": everything.PRESERVE_ORIGINAL_MAX_EDIT_TOKENS,
    )

    result = everything.edit_preserve_original_blocks(
        "Improve grammar throughout.",
        {"editable_blocks": [{"anchor": "p_0001", "text": "x"}], "context_blocks": []},
    )

    assert result["error"] is True
    assert "too large to safely revise" in result["message"]


def test_build_full_content_file_entries_caps_chunks_per_file(monkeypatch):
    rows = [
        _make_row(1, "file-a", chunk_index=0),
        _make_row(2, "file-a", chunk_index=1),
        _make_row(3, "file-a", chunk_index=2),
        _make_row(4, "file-b", chunk_index=0),
        _make_row(5, "file-b", chunk_index=1),
        _make_row(6, "file-b", chunk_index=2),
    ]

    def _fake_query_rows(*, draft_id, project_id, file_scope, file_unique_ids):
        return rows

    monkeypatch.setattr(everything, "_query_full_content_rows", _fake_query_rows)

    entries = everything._build_full_content_file_entries(
        draft_id=42,
        project_id=7,
        file_scope="all_matter_and_chat",
        file_unique_ids=["file-a", "file-b"],
        file_scope_by_unique_id={"file-a": "recent_chat_files", "file-b": "matter_files"},
        max_chunks_per_file=2,
    )

    assert len(entries) == 2
    assert entries[0]["file_id"] == "file-a"
    assert entries[1]["file_id"] == "file-b"
    assert len(entries[0]["chunks"]) == 2
    assert len(entries[1]["chunks"]) == 2
    assert entries[0]["file_scope"] == "recent_chat_files"
    assert entries[1]["file_scope"] == "matter_files"


def test_summarize_one_full_content_file_prompt_includes_required_instructions(monkeypatch):
    captured = {}

    def _fake_chat_completion_j(prompt, model="gpt-5.4", token_counter=None):
        captured["prompt"] = prompt
        return """{
            "file_name":"A.pdf",
            "file_id":"file-a",
            "file_scope":"recent_chat_files",
            "query_relevant_summary":"summary text",
            "source_anchors":["Section I", "chunk 2"]
        }"""

    monkeypatch.setattr(everything, "chat_completion_j", _fake_chat_completion_j)

    result = everything._summarize_one_full_content_file(
        user_message="draft opposition arguments",
        file_entry={
            "file_name": "A.pdf",
            "file_id": "file-a",
            "file_scope": "recent_chat_files",
            "chunks": [{"chunk_index": 0, "content": "hello world"}],
        },
        model="gpt-5.4",
    )

    prompt = captured["prompt"]
    assert "Extract only information helpful for the user query." in prompt
    assert "Prioritize facts, claims/defenses, dates, amounts, procedural posture, authorities, and contradictions." in prompt
    assert "Do not invent content not in source." in prompt
    assert "Include paragraph/section references whenever possible." in prompt
    assert result["file_name"] == "A.pdf"
    assert result["file_id"] == "file-a"
    assert result["file_scope"] == "recent_chat_files"


def test_summarize_full_content_entries_threaded_calls_each_file_and_bounds_workers(monkeypatch):
    captured = {"max_workers": None}
    calls = []

    def _fake_summarize_one(user_message, file_entry, model="gpt-5.4"):
        calls.append(file_entry["file_id"])
        return {
            "file_name": file_entry["file_name"],
            "file_id": file_entry["file_id"],
            "file_scope": file_entry["file_scope"],
            "query_relevant_summary": f"summary-{file_entry['file_id']}",
            "source_anchors": ["chunk 0"],
        }

    class _FakeFuture:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

    class _FakeThreadPool:
        def __init__(self, max_workers=None):
            captured["max_workers"] = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn, *args, **kwargs):
            return _FakeFuture(fn(*args, **kwargs))

        def shutdown(self, wait=True, cancel_futures=False):
            pass

    def _fake_as_completed(future_map):
        return list(future_map.keys())

    monkeypatch.setattr(everything, "_summarize_one_full_content_file", _fake_summarize_one)
    monkeypatch.setattr(everything.concurrent.futures, "ThreadPoolExecutor", _FakeThreadPool)
    monkeypatch.setattr(everything.concurrent.futures, "as_completed", _fake_as_completed)

    file_entries = [
        {
            "file_name": f"File-{idx}.pdf",
            "file_id": f"file-{idx}",
            "file_scope": "matter_files",
            "chunks": [{"chunk_index": 0, "content": "abc"}],
        }
        for idx in range(8)
    ]

    summaries = everything._summarize_full_content_entries_threaded(
        user_message="question",
        file_entries=file_entries,
        model="gpt-5.4",
    )

    assert captured["max_workers"] == 6  # bounded thread count
    assert calls == [f"file-{idx}" for idx in range(8)]  # one call per file
    assert [row["file_id"] for row in summaries] == [f"file-{idx}" for idx in range(8)]  # preserves input order


def test_render_full_content_summary_blocks_includes_metadata_and_retrieve_section():
    text = everything._render_full_content_summary_blocks(
        file_summaries=[{
            "file_name": "A.pdf",
            "file_id": "file-a",
            "file_scope": "matter_files",
            "query_relevant_summary": "summary",
            "source_anchors": ["Section 1", "chunk 3"],
        }],
        retrieve_supplement="retrieve output",
    )

    assert "[file_name]: A.pdf" in text
    assert "[file_id]: file-a" in text
    assert "[file_scope]: matter_files" in text
    assert "[query_relevant_summary]:" in text
    assert "[source_anchors]:" in text
    assert "[supplemental_retrieve_results]:" in text


def test_build_threaded_full_content_extraction_appends_retrieve_when_available(monkeypatch):
    def _fake_build_entries(*args, **kwargs):
        return [{
            "file_name": "A.pdf",
            "file_id": "file-a",
            "file_scope": "recent_chat_files",
            "chunks": [{"chunk_index": 0, "content": "abc"}],
        }]

    def _fake_summarize_entries(user_message, file_entries, model="gpt-5.4"):
        return [{
            "file_name": "A.pdf",
            "file_id": "file-a",
            "file_scope": "recent_chat_files",
            "query_relevant_summary": "summary",
            "source_anchors": ["chunk 0"],
        }]

    def _fake_retrieve(*args, **kwargs):
        return "retrieve output"

    monkeypatch.setattr(everything, "_build_full_content_file_entries", _fake_build_entries)
    monkeypatch.setattr(everything, "_summarize_full_content_entries_threaded", _fake_summarize_entries)
    monkeypatch.setattr(everything, "get_relevant_content_bm25", _fake_retrieve)

    state = {}
    combined, cache_key = everything.build_threaded_full_content_extraction(
        user_message="question",
        draft_id=1,
        project_id=1,
        state=state,
        file_scope="all_chat_files",
        file_unique_ids=["file-a"],
        file_scope_by_unique_id={"file-a": "recent_chat_files"},
        include_retrieve_supplement=True,
        retrieve_file_unique_ids=["file-a"],
        model="gpt-5.4",
    )

    assert "[supplemental_retrieve_results]:" in combined
    assert "retrieve output" in combined
    assert state["last_extraction"]["cache_key"] == cache_key


def test_build_threaded_full_content_extraction_skips_retrieve_when_disabled(monkeypatch):
    def _fake_build_entries(*args, **kwargs):
        return [{
            "file_name": "A.pdf",
            "file_id": "file-a",
            "file_scope": "recent_chat_files",
            "chunks": [{"chunk_index": 0, "content": "abc"}],
        }]

    def _fake_summarize_entries(user_message, file_entries, model="gpt-5.4"):
        return [{
            "file_name": "A.pdf",
            "file_id": "file-a",
            "file_scope": "recent_chat_files",
            "query_relevant_summary": "summary",
            "source_anchors": ["chunk 0"],
        }]

    def _raise_if_called(*args, **kwargs):
        raise AssertionError("retrieve should not be called when include_retrieve_supplement=False")

    monkeypatch.setattr(everything, "_build_full_content_file_entries", _fake_build_entries)
    monkeypatch.setattr(everything, "_summarize_full_content_entries_threaded", _fake_summarize_entries)
    monkeypatch.setattr(everything, "get_relevant_content_bm25", _raise_if_called)

    state = {}
    combined, _ = everything.build_threaded_full_content_extraction(
        user_message="question",
        draft_id=1,
        project_id=1,
        state=state,
        file_scope="all_chat_files",
        file_unique_ids=["file-a"],
        include_retrieve_supplement=False,
        model="gpt-5.4",
    )
    assert "[supplemental_retrieve_results]:" not in combined


def _run_phase2_with_prompt_turn_meta(monkeypatch, payload_updates=None, llm_response="stubbed llm response", llm_call=None):
    state = {
        "draft": {"title": "", "sections": []},
        "selected_section": "Not Specified",
        "chat_history": [],
        "chat_history_meta": [],
        "uploaded_files": {},
        "conversation_brief": "",
        "constraints": {},
        "validated_cases": {},
    }

    class _DummyTask:
        def update_state(self, state=None):
            return None

    monkeypatch.setattr(everything, "DBMemory", lambda db, doc_id: {"doc_id": doc_id})
    monkeypatch.setattr(everything, "load_state_from_memory", lambda memory: state)
    monkeypatch.setattr(everything, "save_state_to_memory", lambda s, memory, **kw: s)
    monkeypatch.setattr(everything, "_is_cancelled", lambda memory, expected_task_id: False)
    monkeypatch.setattr(everything, "retrieve_relevant_chat_turns", lambda user_message, chat_history, **kwargs: "")
    monkeypatch.setattr(
        everything,
        "multichat_completion_parse",
        lambda **kwargs: _typed_parse_result(kwargs["response_model"], llm_response),
    )
    monkeypatch.setattr(everything, "multichat_completion_text", llm_call or (lambda **kwargs: llm_response))
    monkeypatch.setattr(everything, "verify_citations_with_lookup_tags", lambda text: ([], []))
    monkeypatch.setattr(
        everything,
        "update_conversation_brief",
        lambda brief, constraints, user_message, assistant_response, **kwargs: (brief, constraints),
    )
    monkeypatch.setattr(everything, "ChatThread", SimpleNamespace(query=SimpleNamespace(get=lambda _doc_id: None)))

    payload = {
        "phase1_task_id": "phase1-task-id",
        "token_total": 0,
        "user_message": "hello",
        "assistant_mode": "deep",
        "selected_section": "Not Specified",
        "use_uploaded_files": False,
        "refresh_context": False,
        "file_scope": "all_matter_and_chat",
        "extraction_method": "retrieve",
        "create_new_draft": False,
        "change_draft_requested": False,
        "file_extractions_str": "",
    }
    if payload_updates:
        payload.update(payload_updates)

    result = everything._everything_phase2_body(_DummyTask(), payload, doc_id=1)
    return state, result


def test_phase2_prompt_turn_meta_missing_key_defaults_to_empty_attachments(monkeypatch):
    state, result = _run_phase2_with_prompt_turn_meta(monkeypatch)

    assert result["response"] == "stubbed llm response"
    assert len(state["chat_history"]) == 1
    assert len(state["chat_history_meta"]) == 1
    assert state["chat_history_meta"][-1]["prompt_attachments"] == []
    assert state["chat_history_meta"][-1]["conversation_turn_id"]


def test_phase2_answer_only_cleans_case_start_markers_before_save(monkeypatch):
    state, result = _run_phase2_with_prompt_turn_meta(
        monkeypatch,
        llm_response="See [[CASE_START]]Smith v. Jones, 12 F.3d 1.",
    )

    assert result["response"] == "See Smith v. Jones, 12 F.3d 1."
    assert state["chat_history"][-1][1] == "See Smith v. Jones, 12 F.3d 1."


@pytest.mark.parametrize(("assistant_mode", "expected_model"), [
    ("deep", "gpt-6.1-sol"),
    ("fast", "gpt-6-luna"),
])
def test_phase2_answer_only_uses_mode_model(monkeypatch, assistant_mode, expected_model):
    captured = {}

    def _fake_text_completion(**kwargs):
        captured.update(kwargs)
        return "Completed answer."

    _, result = _run_phase2_with_prompt_turn_meta(
        monkeypatch,
        payload_updates={"assistant_mode": assistant_mode},
        llm_call=_fake_text_completion,
    )

    assert result["response"] == "Completed answer."
    assert captured["model"] == expected_model
    assert captured["reasoning_effort"] == ("high" if assistant_mode == "deep" else "medium")
    assert "require_json" not in captured


def test_phase2_answer_only_missing_output_fails_without_saving_turn(monkeypatch):
    state, result = _run_phase2_with_prompt_turn_meta(
        monkeypatch,
        payload_updates={"assistant_mode": "fast"},
        llm_call=lambda **kwargs: None,
    )

    assert result["error"] is True
    assert "could not be generated" in result["response"]
    assert state["chat_history"] == []


def test_phase2_invalid_mode_fails_before_loading_state(monkeypatch):
    monkeypatch.setattr(
        everything,
        "DBMemory",
        lambda **kwargs: pytest.fail("invalid mode must fail before state or LLM work"),
    )

    result = everything._everything_phase2_body(
        _DummyTask(), {"assistant_mode": "turbo"}, doc_id=1
    )

    assert result["error"] is True
    assert "Invalid Case Assistant mode" in result["response"]


def test_phase2_prompt_turn_meta_passthrough_persists_attachment_meta(monkeypatch):
    state, result = _run_phase2_with_prompt_turn_meta(
        monkeypatch,
        payload_updates={
            "prompt_turn_meta": {
                "prompt_attachments": [
                    {"file_id": "x", "file_name": "A.pdf"},
                ]
            }
        },
    )

    assert result["response"] == "stubbed llm response"
    assert state["chat_history_meta"][-1]["prompt_attachments"] == [
        {"file_id": "x", "file_name": "A.pdf"}
    ]


def test_phase2_prompt_turn_meta_malformed_type_normalizes_to_empty(monkeypatch):
    state, result = _run_phase2_with_prompt_turn_meta(
        monkeypatch,
        payload_updates={"prompt_turn_meta": "not-a-dict"},
    )

    assert result["response"] == "stubbed llm response"
    assert state["chat_history_meta"][-1]["prompt_attachments"] == []
    assert state["chat_history_meta"][-1]["conversation_turn_id"]


def test_phase2_confirmed_turn_saves_display_prompt_and_clarification_meta(monkeypatch):
    clarification_meta = {
        "workflow_run_id": "wf-test",
        "reason": "Which document should I revise?",
        "answer": "Use the uploaded brief and report only.",
    }
    state, result = _run_phase2_with_prompt_turn_meta(
        monkeypatch,
        payload_updates={
            "user_message": "revise the document\n\nUser clarification: Use the uploaded brief and report only.",
            "display_user_message": "revise the document",
            "internal_user_message": (
                "revise the document\n\n"
                ">>> USER'S CONFIRMED CLARIFICATION - AUTHORITATIVE"
            ),
            "prompt_turn_meta": {
                "prompt_attachments": [],
                "clarification": clarification_meta,
            },
        },
    )

    assert result["response"] == "stubbed llm response"
    assert state["chat_history"][-1][0] == "revise the document"
    assert "USER'S CONFIRMED CLARIFICATION" not in state["chat_history"][-1][0]
    assert state["chat_history_meta"][-1]["clarification"] == clarification_meta


def test_add_chat_message_strips_case_start_markers_defensively():
    state = {"chat_history": [], "chat_history_meta": []}

    everything.add_chat_message(
        state,
        ("q", "See [[CASE_START]]Smith v. Jones, 12 F.3d 1."),
    )

    assert state["chat_history"][-1] == ("q", "See Smith v. Jones, 12 F.3d 1.")


def test_pending_turn_snapshot_preserves_confirmation_for_hydration():
    confirmation = {
        "workflow_run_id": "wf-test",
        "message": "Please confirm how to handle this draft request.",
        "options": [],
    }

    pending_turn = everything._build_pending_turn_snapshot(
        display_user_message="check the uploaded brief",
        prompt_turn_meta={"prompt_attachments": [{"file_id": "file-1", "file_name": "Brief.pdf"}]},
        workflow_run_id="wf-test",
        draft_id="draft-public",
        confirmation=confirmation,
        awaiting_confirmation=True,
    )

    assert pending_turn["input"] == "check the uploaded brief"
    assert pending_turn["response"] == "Please confirm how to handle this draft request."
    assert pending_turn["awaitingConfirmation"] is True
    assert pending_turn["workflowRunId"] == "wf-test"
    assert pending_turn["draftId"] == "draft-public"
    assert pending_turn["confirmation"] == confirmation
    assert pending_turn["promptAttachments"] == [{"file_id": "file-1", "file_name": "Brief.pdf"}]


def _install_phase2_draft_action_mocks(monkeypatch, state):
    monkeypatch.setattr(everything, "DBMemory", lambda db, doc_id: {"doc_id": doc_id})
    monkeypatch.setattr(everything, "load_state_from_memory", lambda memory: state)
    monkeypatch.setattr(everything, "save_state_to_memory", lambda s, memory, **kw: s)
    monkeypatch.setattr(everything, "_is_cancelled", lambda memory, expected_task_id: False)
    monkeypatch.setattr(everything, "retrieve_relevant_chat_turns", lambda user_message, chat_history, **kwargs: "")
    monkeypatch.setattr(everything, "verify_citations_with_lookup_tags", lambda text: ([], []))
    monkeypatch.setattr(
        everything,
        "update_conversation_brief",
        lambda brief, constraints, user_message, assistant_response, **kwargs: (brief, constraints),
    )
    monkeypatch.setattr(
        everything,
        "ChatThread",
        SimpleNamespace(query=SimpleNamespace(get=lambda _doc_id: SimpleNamespace(public_id="draft-public"))),
    )


def _base_phase2_state(draft=None):
    return {
        "draft": draft if draft is not None else {"title": "", "sections": []},
        "selected_section": "Not Specified",
        "chat_history": [],
        "chat_history_meta": [],
        "uploaded_files": {},
        "conversation_brief": "",
        "constraints": {},
        "validated_cases": {},
    }


def _base_phase2_payload(**updates):
    payload = {
        "phase1_task_id": "phase1-task-id",
        "token_total": 0,
        "user_message": "please revise the draft",
        "assistant_mode": "deep",
        "selected_section": "Not Specified",
        "use_uploaded_files": False,
        "refresh_context": False,
        "file_scope": "all_matter_and_chat",
        "extraction_method": "retrieve",
        "create_new_draft": False,
        "change_draft_requested": True,
        "draft_action": "edit_current_draft",
        "revision_mode": "general_revision",
        "file_selection": {},
        "verbatim_file_unique_ids": [],
        "file_extractions_str": "",
    }
    payload.update(updates)
    return payload


def _fake_normalizer_factory(calls):
    def _fake_normalize(draft, **kwargs):
        calls.append(copy.deepcopy(draft))
        normalized = everything.sanitize_draft_structure(draft)
        for section in normalized.get("sections", []):
            if section.get("blocks"):
                continue
            content = section.get("section_content", "")
            if content:
                section["blocks"] = [{
                    "block_id": "b_0001",
                    "type": "paragraph",
                    "text": content,
                    "paragraph_index": 1,
                    "paragraph_number": None,
                }]
        return normalized

    return _fake_normalize


def _saved_evidence_phase2(monkeypatch, branch):
    """Exercise real phase-2 dispatch while isolating persistence/provider I/O."""
    state = _base_phase2_state(_block_operation_sample_draft())
    _install_phase2_draft_action_mocks(monkeypatch, state)
    monkeypatch.setattr(everything, "normalize_draft_structure", _fake_normalizer_factory([]))
    monkeypatch.setattr(everything, "db", SimpleNamespace(session=SimpleNamespace(commit=lambda: None)))
    from backend.util import document_preparation
    monkeypatch.setattr(document_preparation, "enabled", lambda: False)
    monkeypatch.setattr(everything, "structure_uploaded_source_draft_for_phase2",
                        lambda *a, **kw: copy.deepcopy(state["draft"]))
    payload = _base_phase2_payload(
        saved_evidence_only=True, document_snapshot={"files": []},
        file_extractions_str="UNIQUE_SAVED_SUPPORTING_EVIDENCE", use_uploaded_files=True,
        draft_action=branch, create_new_draft=branch == "create_new_draft",
        change_draft_requested=branch not in ("answer_only", "create_new_draft"),
    )
    if branch in ("revise_uploaded_draft", "preserve_original"):
        payload.update(draft_action="revise_uploaded_draft",
                       revision_mode="preserve_original" if branch == "preserve_original" else "general_revision",
                       file_selection={"primary_draft_file_id": "primary"},
                       verbatim_file_unique_ids=["primary"])
    return state, payload


@pytest.mark.parametrize("branch", ["answer_only", "create_new_draft", "edit_current_draft",
                                   "revise_uploaded_draft", "preserve_original"])
def test_phase2_saved_evidence_overflow_raises_budget_exception(monkeypatch, branch):
    state, payload = _saved_evidence_phase2(monkeypatch, branch)
    original = copy.deepcopy(state["draft"])
    monkeypatch.setattr(everything, "cap_file_extractions_for_prompt",
                        lambda **kw: (kw["file_extractions_str"], {"truncated": True, "verbatim_budget": 100}))
    def unexpected(*a, **kw):
        pytest.fail("Oversized saved evidence must not reach a provider or edit operation")
    for name in ("multichat_completion_parse", "multichat_completion_text", "run_preserve_original_uploaded_revision"):
        monkeypatch.setattr(everything, name, unexpected)
    with pytest.raises(everything.ContextBudgetExceeded, match="Saved evidence"):
        everything._everything_phase2_body(_DummyTask(), payload, doc_id=1)
    assert state["draft"] == original
    assert not state["chat_history"]


@pytest.mark.parametrize("branch", ["answer_only", "create_new_draft", "edit_current_draft",
                                   "revise_uploaded_draft", "preserve_original"])
@pytest.mark.parametrize("exception", [everything.ContextBudgetExceeded, everything.WorkflowStopped,
                                      everything.SoftTimeLimitExceeded])
def test_phase2_control_exceptions_escape_final_branch_handlers(monkeypatch, branch, exception):
    state, payload = _saved_evidence_phase2(monkeypatch, branch)
    original = copy.deepcopy(state["draft"])
    monkeypatch.setattr(everything, "cap_file_extractions_for_prompt",
                        lambda **kw: (kw["file_extractions_str"], {"truncated": False}))
    def fail(*a, **kw):
        raise exception("Stop before publication")
    for name in ("multichat_completion_parse", "multichat_completion_text", "run_preserve_original_uploaded_revision"):
        monkeypatch.setattr(everything, name, fail)
    with pytest.raises(exception, match="Stop before publication"):
        everything._everything_phase2_body(_DummyTask(), payload, doc_id=1)
    assert state["draft"] == original
    assert not state["chat_history"]


@pytest.mark.parametrize("branch", ["create_new_draft", "edit_current_draft", "revise_uploaded_draft"])
def test_phase2_final_preflight_requests_evidence_reduction(monkeypatch, branch):
    state, payload = _saved_evidence_phase2(monkeypatch, branch)
    monkeypatch.setattr(everything, "cap_file_extractions_for_prompt",
                        lambda **kw: (kw["file_extractions_str"], {"truncated": False}))
    # The complete prompt fails; keeping the same draft/instructions but removing
    # supporting evidence fits. This is recoverable without changing the draft.
    evidence = payload["file_extractions_str"]
    monkeypatch.setattr(everything, "draft_prompt_context_limit_error", lambda **kw:
                        {"message": "Evidence exceeds the available input"} if evidence in kw["new_prompt"] else None)
    monkeypatch.setattr(everything, "multichat_completion_parse",
                        lambda **kw: pytest.fail("Preflight must reject before provider dispatch"))
    with pytest.raises(everything.ContextBudgetExceeded):
        everything._everything_phase2_body(_DummyTask(), payload, doc_id=1)
    assert not state["chat_history"]


@pytest.mark.parametrize("branch", ["create_new_draft", "edit_current_draft", "revise_uploaded_draft"])
def test_phase2_source_only_overflow_is_not_retried_as_evidence(monkeypatch, branch):
    state, payload = _saved_evidence_phase2(monkeypatch, branch)
    original = copy.deepcopy(state["draft"])
    monkeypatch.setattr(everything, "cap_file_extractions_for_prompt",
                        lambda **kw: (kw["file_extractions_str"], {"truncated": False}))
    checked = []
    def too_large(**kw):
        checked.append(kw["new_prompt"])
        return {"message": everything.DRAFT_CONTEXT_TOO_LONG_MESSAGE}
    monkeypatch.setattr(everything, "draft_prompt_context_limit_error", too_large)
    monkeypatch.setattr(everything, "multichat_completion_parse",
                        lambda **kw: pytest.fail("An oversized exact draft must not reach the provider"))
    with pytest.raises(everything.ContextBudgetExceeded) as error:
        everything._everything_phase2_body(_DummyTask(), payload, doc_id=1)
    assert not error.value.can_reduce_evidence
    assert len(checked) == 2
    assert payload["file_extractions_str"] not in checked[-1]
    if branch != "create_new_draft":
        assert "Intro" in checked[-1]  # Exact source is retained in the second preflight.
    assert state["draft"] == original and not state["chat_history"]


@pytest.mark.parametrize("branch", ["answer_only", "edit_current_draft", "revise_uploaded_draft"])
def test_phase2_no_context_space_stops_without_evidence_reduction(monkeypatch, branch):
    state, payload = _saved_evidence_phase2(monkeypatch, branch)
    original = copy.deepcopy(state["draft"])
    monkeypatch.setattr(everything, "cap_file_extractions_for_prompt",
                        lambda **kw: (kw["file_extractions_str"], {"truncated": True, "verbatim_budget": 0}))
    with pytest.raises(everything.ContextBudgetExceeded, match="select a smaller section") as error:
        everything._everything_phase2_body(_DummyTask(), payload, doc_id=1)
    assert not error.value.can_reduce_evidence
    assert state["draft"] == original and not state["chat_history"]


def test_source_only_preflight_keeps_primary_verbatim_text(monkeypatch):
    state, payload = _saved_evidence_phase2(monkeypatch, "revise_uploaded_draft")
    exact = everything.VERBATIM_BLOCK_HEADER + "[file_id]: primary\n[verbatim_content]:\nEXACT_PRIMARY_DEADLINE_CLAUSE"
    evidence = payload["file_extractions_str"]
    payload["file_extractions_str"] = everything.merge_verbatim_and_extracted_context(exact, evidence)
    monkeypatch.setattr(everything, "cap_file_extractions_for_prompt",
                        lambda **kw: (kw["file_extractions_str"], {"truncated": False}))
    checked = []
    def reject(**kw):
        checked.append(kw["new_prompt"])
        return {"message": everything.DRAFT_CONTEXT_TOO_LONG_MESSAGE}
    monkeypatch.setattr(everything, "draft_prompt_context_limit_error", reject)
    with pytest.raises(everything.ContextBudgetExceeded) as error:
        everything._everything_phase2_body(_DummyTask(), payload, doc_id=1)
    assert not error.value.can_reduce_evidence
    assert len(checked) == 2 and "EXACT_PRIMARY_DEADLINE_CLAUSE" in checked[-1]
    assert evidence not in checked[-1] and not state["chat_history"]


@pytest.mark.parametrize("selection", [False, True])
def test_preserve_original_framed_source_overflow_cannot_be_fixed_by_evidence(monkeypatch, selection):
    source = "Exact original deadline paragraph."
    material = ([{"anchor": "p_0001", "text": source}] if selection else
                {"editable_blocks": [{"anchor": "p_0001", "text": source}], "context_blocks": []})
    # Raw source fits, but the full instructions/schema/framing do not, even
    # without evidence. The full preflight must not ask for futile reductions.
    source_budget = everything.num_tokens_from_string(json.dumps(material) + "Update.") + 100
    monkeypatch.setattr(everything, "PRESERVE_ORIGINAL_MAX_EDIT_TOKENS",
                        everything.PRESERVE_ORIGINAL_COMPLETION_RESERVE + source_budget)
    monkeypatch.setattr(everything, "chat_completion_parse", lambda *a, **kw:
                        pytest.fail("An oversized exact-source request reached the provider"))
    function = everything.select_preserve_original_targets if selection else everything.edit_preserve_original_blocks
    with pytest.raises(everything.ContextBudgetExceeded, match="select a smaller section") as error:
        function("Update.", material, supporting_evidence="Source fact.")
    assert not error.value.can_reduce_evidence
    assert source in json.dumps(material)


@pytest.mark.parametrize(("assistant_mode", "expected_model"), [
    ("deep", "gpt-6.1-sol"),
    ("fast", "gpt-6-luna"),
])
@pytest.mark.parametrize("draft_action", [
    "create_new_draft", "edit_current_draft", "revise_uploaded_draft"
])
def test_phase2_draft_outputs_use_mode_model_and_output_effort(
    monkeypatch, assistant_mode, expected_model, draft_action
):
    state = _base_phase2_state(
        _block_operation_sample_draft() if draft_action == "edit_current_draft" else None
    )
    _install_phase2_draft_action_mocks(monkeypatch, state)
    monkeypatch.setattr(everything, "normalize_draft_structure", _fake_normalizer_factory([]))
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        if draft_action == "create_new_draft":
            assert kwargs["response_model"] is everything.DraftCreateResult
            return kwargs["response_model"].model_validate({
                "response": "To the right, I drafted the document.",
                "draft": {"title": "New Draft", "sections": [
                    {"section_id": "1", "section_name": "Intro", "section_content": "Draft body."}
                ]},
            })
        assert kwargs["response_model"] is everything.DraftEditResult
        section_id = "s_0001" if draft_action == "revise_uploaded_draft" else "1"
        return kwargs["response_model"].model_validate({
            "response": "I revised the draft on the right.",
            "operations": [{"op": "edit_section", "section_id": section_id,
                            "section_name": "Intro", "section_content": "Revised body."}],
        })

    monkeypatch.setattr(everything, "multichat_completion_parse", parse)
    payload_updates = {"assistant_mode": assistant_mode, "draft_action": draft_action}
    if draft_action == "create_new_draft":
        payload_updates.update(create_new_draft=True, change_draft_requested=False)
    elif draft_action == "revise_uploaded_draft":
        payload_updates.update(
            use_uploaded_files=True,
            file_selection={"primary_draft_file_id": "draft-file"},
            verbatim_file_unique_ids=["draft-file"],
            file_extractions_str="source context",
        )
        monkeypatch.setattr(
            everything,
            "structure_uploaded_source_draft_for_phase2",
            lambda primary_draft_file_id, *, doc_id, project_id, **kwargs: {
                "title": "Uploaded Source",
                "sections": [{"section_id": "s_0001", "section_name": "Intro",
                              "section_content": "Source body."}],
            },
        )

    result = everything._everything_phase2_body(
        _DummyTask(), _base_phase2_payload(**payload_updates), doc_id=1
    )

    assert result["error"] is False
    assert len(calls) == 1
    assert calls[0]["model"] == expected_model
    assert calls[0]["reasoning_effort"] == ("high" if assistant_mode == "deep" else "medium")


def test_phase2_missing_typed_draft_output_fails_without_applying_edits(monkeypatch):
    state = _base_phase2_state(_block_operation_sample_draft())
    original_draft = copy.deepcopy(state["draft"])
    _install_phase2_draft_action_mocks(monkeypatch, state)
    monkeypatch.setattr(everything, "normalize_draft_structure", _fake_normalizer_factory([]))
    monkeypatch.setattr(everything, "multichat_completion_parse", lambda **kwargs: None)

    result = everything._everything_phase2_body(
        _DummyTask(), _base_phase2_payload(assistant_mode="deep"), doc_id=1
    )

    assert result["error"] is True
    assert state["draft"] == original_draft
    assert "retry" in result["response"].lower()


def test_phase2_edit_current_draft_normalizes_before_prompt_and_after_save(monkeypatch):
    state = _base_phase2_state({
        "title": "Draft",
        "sections": [{"section_id": "1", "section_name": "Intro", "section_content": "Original body."}],
    })
    _install_phase2_draft_action_mocks(monkeypatch, state)
    normalize_calls = []
    captured = {}
    monkeypatch.setattr(everything, "normalize_draft_structure", _fake_normalizer_factory(normalize_calls))

    def _fake_multichat_completion_j(**kwargs):
        captured["prompt"] = kwargs["new_prompt"]
        return """{
            "response": "I have made the following changes to the current draft to the right: revised the intro.",
            "operations": [
                {"op": "edit_section", "section_id": "1", "section_name": "Intro", "section_content": "Edited body."}
            ]
        }"""

    monkeypatch.setattr(everything, "multichat_completion_parse", _mock_multichat_parse(_fake_multichat_completion_j))

    result = everything._everything_phase2_body(_DummyTask(), _base_phase2_payload(), doc_id=1)

    assert len(normalize_calls) == 2
    assert '"blocks"' in captured["prompt"]
    assert "'op':'edit_block'" in captured["prompt"]
    _assert_cases_cited_response_requirement(captured["prompt"])
    assert state["draft"]["sections"][0]["blocks"][0]["text"] == "Edited body."
    assert result["text"]["sections"][0]["blocks"][0]["text"] == "Edited body."


def test_phase2_edit_current_draft_applies_block_operations_and_returns_metadata(monkeypatch):
    state = _base_phase2_state(_block_operation_sample_draft())
    _install_phase2_draft_action_mocks(monkeypatch, state)
    normalize_calls = []
    monkeypatch.setattr(everything, "normalize_draft_structure", _fake_normalizer_factory(normalize_calls))

    def _fake_multichat_completion_j(**kwargs):
        return """{
            "response": "I have made the following changes to the current draft to the right: revised one paragraph.",
            "operations": [
                {"op": "edit_block", "section_id": "1", "block_id": "b_0002", "text": "Revised second paragraph."}
            ]
        }"""

    monkeypatch.setattr(everything, "multichat_completion_parse", _mock_multichat_parse(_fake_multichat_completion_j))

    result = everything._everything_phase2_body(_DummyTask(), _base_phase2_payload(), doc_id=1)

    section = result["text"]["sections"][0]
    assert result["applied_operations"] == [{"op": "edit_block", "section_id": "1", "block_id": "b_0002"}]
    assert result["rejected_operations"] == []
    assert section["blocks"][0]["text"] == "First paragraph."
    assert section["blocks"][1]["text"] == "Revised second paragraph."
    assert section["section_content"] == "First paragraph.\n\nRevised second paragraph."


def test_phase2_create_new_draft_normalizes_output_before_save(monkeypatch):
    state = _base_phase2_state()
    _install_phase2_draft_action_mocks(monkeypatch, state)
    normalize_calls = []
    monkeypatch.setattr(everything, "normalize_draft_structure", _fake_normalizer_factory(normalize_calls))
    monkeypatch.setattr(
        everything,
        "multichat_completion_parse",
        _mock_multichat_parse(lambda **kwargs: """{
            "response": "To the right, I have drafted a short document.",
            "draft": {
                "title": "New Draft",
                "sections": [{"section_id": "1", "section_name": "Intro", "section_content": "Draft body."}]
            }
        }"""),
    )

    result = everything._everything_phase2_body(
        _DummyTask(),
        _base_phase2_payload(create_new_draft=True, change_draft_requested=False, draft_action="create_new_draft"),
        doc_id=1,
    )

    assert len(normalize_calls) == 1
    assert state["draft"]["sections"][0]["blocks"][0]["text"] == "Draft body."
    assert result["text"]["sections"][0]["blocks"][0]["text"] == "Draft body."


def test_phase2_create_new_draft_cleans_case_start_markers_before_save(monkeypatch):
    state = _base_phase2_state()
    _install_phase2_draft_action_mocks(monkeypatch, state)
    monkeypatch.setattr(everything, "normalize_draft_structure", _fake_normalizer_factory([]))
    captured = {}

    def _fake_multichat_completion_j(**kwargs):
        captured["prompt"] = kwargs["new_prompt"]
        return """{
            "response": "To the right, I drafted it using [[CASE_START]]Smith v. Jones, 12 F.3d 1.",
            "draft": {
                "title": "New Draft",
                "sections": [{"section_id": "1", "section_name": "Intro", "section_content": "Draft body."}]
            }
        }"""

    monkeypatch.setattr(everything, "multichat_completion_parse", _mock_multichat_parse(_fake_multichat_completion_j))

    result = everything._everything_phase2_body(
        _DummyTask(),
        _base_phase2_payload(create_new_draft=True, change_draft_requested=False, draft_action="create_new_draft"),
        doc_id=1,
    )

    expected = "To the right, I drafted it using Smith v. Jones, 12 F.3d 1."
    _assert_cases_cited_response_requirement(captured["prompt"])
    assert result["response"] == expected
    assert state["chat_history"][-1][1] == expected


def test_phase2_revise_uploaded_draft_structures_source_and_normalizes_output(monkeypatch):
    state = _base_phase2_state()
    _install_phase2_draft_action_mocks(monkeypatch, state)
    normalize_calls = []
    captured = {}
    monkeypatch.setattr(everything, "normalize_draft_structure", _fake_normalizer_factory(normalize_calls))
    monkeypatch.setattr(
        everything,
        "structure_uploaded_source_draft_for_phase2",
        lambda primary_draft_file_id, *, doc_id, project_id, **kwargs: {
            "title": "Uploaded Source",
            "sections": [{"section_id": "s_0001", "section_name": "Intro", "section_content": "Source body."}],
        },
    )

    def _fake_multichat_completion_j(**kwargs):
        captured["prompt"] = kwargs["new_prompt"]
        # Revise now uses the bounded operations contract (deltas), not whole-doc echo.
        return """{
            "response": "I have made the following changes to the current draft to the right: revised the intro.",
            "operations": [
                {"op": "edit_section", "section_id": "s_0001", "section_name": "Intro", "section_content": "Revised body."}
            ]
        }"""

    monkeypatch.setattr(everything, "multichat_completion_parse", _mock_multichat_parse(_fake_multichat_completion_j))

    result = everything._everything_phase2_body(
        _DummyTask(),
        _base_phase2_payload(
            user_message="revise the uploaded draft",
            use_uploaded_files=True,
            draft_action="revise_uploaded_draft",
            revision_mode="general_revision",
            file_selection={"primary_draft_file_id": "draft-file"},
            verbatim_file_unique_ids=["draft-file"],
            file_extractions_str="raw source context",
        ),
        doc_id=1,
    )

    # The structured uploaded source is embedded as the draft, and the operations menu is used
    # instead of asking the model to echo the whole revised document.
    assert "Source body." in captured["prompt"]
    assert "'op':'edit_block'" in captured["prompt"]
    _assert_cases_cited_response_requirement(captured["prompt"])
    assert len(normalize_calls) >= 1
    assert result["error"] is False
    assert result["text"]["sections"][0]["blocks"][0]["text"] == "Revised body."


def test_case_assistant_uses_documented_context_windows_and_maximum_output_reserve():
    assert everything.MODEL_CONTEXT_LIMITS == {
        "gpt-6.1-sol": 1_050_000,
        "gpt-6-luna": 1_050_000,
        "gpt-6-sol": 1_050_000,
        "gpt-5.5": 1_050_000,
        "gpt-5.4": 1_050_000,
        "gpt-5.4-mini": 400_000,
    }
    assert everything.MODEL_COMPLETION_RESERVE == 128_000
    assert everything.PROMPT_TOKEN_SAFETY_MARGIN == 2_048


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-luna"])
def test_case_assistant_allows_prompt_above_old_272k_budget(monkeypatch, model):
    monkeypatch.setattr(everything, "estimate_multichat_input_tokens", lambda **kwargs: 300_000)

    assert everything.draft_prompt_context_limit_error(
        new_prompt="long prompt",
        selected_model=model,
        sys_prompt="system",
        session_context_msg=None,
        chat_history_reduced=[],
    ) is None


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-luna"])
def test_case_assistant_draft_preflight_respects_model_window(monkeypatch, model):
    input_budget = 1_050_000 - 128_000 - 2_048
    monkeypatch.setattr(everything, "estimate_multichat_input_tokens", lambda **kwargs: input_budget + 1)

    error = everything.draft_prompt_context_limit_error(
        new_prompt="too long",
        selected_model=model,
        sys_prompt="system",
        session_context_msg=None,
        chat_history_reduced=[],
    )

    assert error["max_input_tokens"] == input_budget
    assert error["context_limit"] == 1_050_000
    assert error["estimated_input_tokens"] == input_budget + 1


def test_case_assistant_skips_local_budget_for_unknown_model(monkeypatch):
    def _unexpected_estimate(**kwargs):
        raise AssertionError("unknown models must not use an invented local context limit")

    monkeypatch.setattr(everything, "estimate_multichat_input_tokens", _unexpected_estimate)
    assert everything.draft_prompt_context_limit_error(
        new_prompt="any prompt",
        selected_model="unregistered-model",
        sys_prompt="system",
        session_context_msg=None,
        chat_history_reduced=[],
    ) is None


def test_phase2_edit_current_draft_surfaces_too_long_prompt(monkeypatch):
    state = _base_phase2_state({
        "title": "Draft",
        "sections": [{"section_id": "1", "section_name": "Intro", "section_content": "Original body."}],
    })
    _install_phase2_draft_action_mocks(monkeypatch, state)
    monkeypatch.setattr(everything, "draft_needs_normalization", lambda draft: False)
    monkeypatch.setattr(
        everything,
        "estimate_multichat_input_tokens",
        lambda **kwargs: 999,
    )
    monkeypatch.setitem(everything.MODEL_CONTEXT_LIMITS, "gpt-6.1-sol", 100)
    monkeypatch.setattr(everything, "MODEL_COMPLETION_RESERVE", 10)
    monkeypatch.setattr(everything, "PROMPT_TOKEN_SAFETY_MARGIN", 5)

    def _unexpected_multichat_completion_j(**kwargs):
        raise AssertionError("LLM should not be called when draft prompt is too long")

    monkeypatch.setattr(everything, "multichat_completion_parse", _unexpected_multichat_completion_j)

    result = everything._everything_phase2_body(_DummyTask(), _base_phase2_payload(), doc_id=1)

    assert result["error"] is True
    assert result["response"].startswith(everything.DRAFT_CONTEXT_TOO_LONG_MESSAGE)
    assert "Estimated input: 999 tokens" in result["response"]


def test_phase2_revise_uploaded_draft_does_not_cap_normalized_source_before_preflight(monkeypatch):
    state = _base_phase2_state()
    _install_phase2_draft_action_mocks(monkeypatch, state)
    captured = {}
    full_source = "SOURCE " * 500
    monkeypatch.setattr(everything, "normalize_draft_structure", _fake_normalizer_factory([]))
    monkeypatch.setattr(
        everything,
        "structure_uploaded_source_draft_for_phase2",
        lambda primary_draft_file_id, *, doc_id, project_id, **kwargs: {
            "title": "Uploaded Source",
            "sections": [{"section_id": "s_0001", "section_name": "Intro", "section_content": full_source}],
        },
    )

    def _fake_multichat_completion_j(**kwargs):
        captured["prompt"] = kwargs["new_prompt"]
        return """{
            "response": "I revised the uploaded document into the draft on the right.",
            "operations": [
                {"op": "edit_section", "section_id": "s_0001", "section_name": "Intro", "section_content": "Revised body."}
            ]
        }"""

    monkeypatch.setattr(everything, "multichat_completion_parse", _mock_multichat_parse(_fake_multichat_completion_j))

    everything._everything_phase2_body(
        _DummyTask(),
        _base_phase2_payload(
            user_message="revise the uploaded draft",
            use_uploaded_files=True,
            draft_action="revise_uploaded_draft",
            revision_mode="general_revision",
            file_selection={"primary_draft_file_id": "draft-file"},
            verbatim_file_unique_ids=["draft-file"],
            file_extractions_str="raw source context",
        ),
        doc_id=1,
    )

    assert full_source.strip() in captured["prompt"]


def test_phase2_preserve_original_response_annotates_citations(monkeypatch):
    state = {
        "draft": {"title": "", "sections": []},
        "selected_section": "Not Specified",
        "chat_history": [],
        "chat_history_meta": [],
        "uploaded_files": {},
        "conversation_brief": "",
        "constraints": {},
        "validated_cases": {},
    }
    captured = {}

    class _DummyTask:
        def update_state(self, state=None):
            return None

    preserve_message = (
        "I have made the following changes to the current draft to the right: "
        "corrected [[CASE_START]]Smith v. Jones, 12 F.3d 1."
    )
    normalize_calls = []

    monkeypatch.setattr(everything, "DBMemory", lambda db, doc_id: {"doc_id": doc_id})
    monkeypatch.setattr(everything, "load_state_from_memory", lambda memory: state)
    monkeypatch.setattr(everything, "save_state_to_memory", lambda s, memory, **kw: s)
    monkeypatch.setattr(everything, "_is_cancelled", lambda memory, expected_task_id: False)
    monkeypatch.setattr(everything, "retrieve_relevant_chat_turns", lambda user_message, chat_history, **kwargs: "")
    monkeypatch.setattr(
        everything,
        "update_conversation_brief",
        lambda brief, constraints, user_message, assistant_response, **kwargs: (brief, constraints),
    )
    monkeypatch.setattr(
        everything,
        "ChatThread",
        SimpleNamespace(query=SimpleNamespace(get=lambda _doc_id: SimpleNamespace(public_id="draft-public"))),
    )
    monkeypatch.setattr(
        everything,
        "run_preserve_original_uploaded_revision",
        lambda **kwargs: {
            "error": False,
            "message": preserve_message,
            "draft": {
                "title": "Uploaded Draft",
                "sections": [
                    {"section_id": "1", "section_name": "INTRODUCTION", "section_content": "Corrected body."}
                ],
            },
            "operations": [{"op": "preserve_original_edit", "edits_applied": 1, "edits_rejected": []}],
        },
    )
    monkeypatch.setattr(everything, "normalize_draft_structure", _fake_normalizer_factory(normalize_calls))

    def _fake_lookup(text):
        captured["lookup_text"] = text
        return ([{"case_name": "Smith v. Jones", "citation": "12 F.3d 1"}], [])

    def _fake_annotate(text, cite_info, case_viewer_prefix=None):
        captured["annotate_text"] = text
        captured["case_viewer_prefix"] = case_viewer_prefix
        return "Annotated Smith v. Jones response."

    monkeypatch.setattr(everything, "verify_citations_with_lookup_tags", _fake_lookup)
    monkeypatch.setattr(everything, "annotate_text_with_citations_multiple", _fake_annotate)
    monkeypatch.setattr(everything, "extract_validated_cases_from_citations", lambda cite_info: {})

    result = everything._everything_phase2_body(
        _DummyTask(),
        {
            "phase1_task_id": "phase1-task-id",
            "token_total": 0,
            "user_message": "correct bad citations",
            "assistant_mode": "deep",
            "selected_section": "Not Specified",
            "use_uploaded_files": True,
            "refresh_context": False,
            "file_scope": "all_matter_and_chat",
            "extraction_method": "retrieve",
            "create_new_draft": False,
            "change_draft_requested": True,
            "draft_action": "revise_uploaded_draft",
            "revision_mode": "preserve_original",
            "file_selection": {"primary_draft_file_id": "draft-file"},
            "verbatim_file_unique_ids": ["draft-file"],
            "file_extractions_str": "",
        },
        doc_id=1,
    )

    assert captured["lookup_text"] == preserve_message
    assert captured["annotate_text"] == preserve_message
    assert captured["case_viewer_prefix"] == "/CaseViewer/draft-public"
    assert result["response"] == "Annotated Smith v. Jones response."
    assert state["chat_history"][-1][1] == "Annotated Smith v. Jones response."
    assert "[[CASE_START]]" not in result["response"]
    assert "[[CASE_START]]" not in state["chat_history"][-1][1]
    assert state["draft"]["title"] == "Uploaded Draft"
    assert normalize_calls
    assert state["draft"]["sections"][0]["blocks"][0]["text"] == "Corrected body."


def test_exclude_file_ids_removes_verbatim_set():
    assert everything._exclude_file_ids(
        ["file-a", "file-b", "file-c"],
        {"file-b"},
    ) == ["file-a", "file-c"]


def test_merge_verbatim_and_extracted_context_ordering():
    verbatim = "====\n[Verbatim Source Files — Full Text]\n[chunk 0]:\npara one"
    extracted = "[query_relevant_summary]:\nsummary text"
    merged = everything.merge_verbatim_and_extracted_context(verbatim, extracted)
    assert merged.startswith(verbatim)
    assert "[Extracted Context]" in merged
    assert merged.endswith(extracted)

    assert everything.merge_verbatim_and_extracted_context(verbatim, "") == verbatim
    assert everything.merge_verbatim_and_extracted_context("", extracted) == extracted


def test_build_verbatim_file_extractions_wraps_formatted_chunks(monkeypatch):
    # Per-file verbatim: each file's raw chunks are loaded (no shared cross-file cap) and emitted
    # as its own block with file_name/file_id headers, joined by the file separator. No LLM call,
    # no summarization — the raw chunk text appears verbatim.
    rows = [
        _make_row(1, "file-complaint", chunk_index=0),
        _make_row(2, "file-complaint", chunk_index=1),
        _make_row(3, "file-answer", chunk_index=0),
    ]
    monkeypatch.setattr(everything, "_query_full_content_rows", lambda **kwargs: rows)

    result = everything.build_verbatim_file_extractions(
        draft_id=1,
        project_id=7,
        file_scope="all_chat_files",
        file_unique_ids=["file-complaint", "file-answer"],
    )
    assert "[Verbatim Source Files — Full Text]" in result
    assert "[file_name]: file-complaint.txt" in result
    assert "[file_id]: file-complaint" in result
    assert "[file_name]: file-answer.txt" in result
    assert "\n\n====\n\n" in result  # per-file blocks separated
    assert "content-1" in result
    assert "content-2" in result
    assert "content-3" in result


def test_summarize_full_content_removed():
    # The lossy second-pass digest was removed: per-file `relevant` extraction (citations
    # preserved) plus the unified final-prompt cap replace it, so no double compression remains.
    assert not hasattr(everything, "summarize_full_content")
    assert not hasattr(everything, "SUMMARIZE_DIGEST_PROMPT")


def test_phase2_drafting_routes_context_through_cap(monkeypatch):
    # Every drafting branch budgets its assembled file context through the unified per-file cap
    # immediately before the LLM call (replacing the old summarize step).
    cap_calls = []

    def _fake_cap(**kwargs):
        cap_calls.append(kwargs["prompt_kind"])
        return kwargs["file_extractions_str"], {"truncated": False}

    monkeypatch.setattr(everything, "cap_file_extractions_for_prompt", _fake_cap)
    monkeypatch.setattr(
        everything,
        "multichat_completion_parse",
        _mock_multichat_parse(lambda **kwargs: '{"response":"ok","operations":[]}'),
    )

    _run_phase2_with_prompt_turn_meta(
        monkeypatch,
        payload_updates={
            "change_draft_requested": True,
            "file_extractions_str": "x" * 600,
            "verbatim_file_unique_ids": ["file-complaint"],
        },
    )
    assert "edit" in cap_calls


def test_split_file_extractions_str_with_and_without_verbatim():
    merged = everything.merge_verbatim_and_extracted_context(
        everything.VERBATIM_BLOCK_HEADER + "[file name]: a.pdf\n[chunk 0]:\nalpha",
        "summary-only",
    )
    verbatim_body, extracted_body = everything.split_file_extractions_str(merged)
    assert verbatim_body is not None
    assert "alpha" in verbatim_body
    assert extracted_body == "summary-only"

    verbatim_only, extracted_only = everything.split_file_extractions_str("summary-only")
    assert verbatim_only is None
    assert extracted_only == "summary-only"


def test_truncate_verbatim_formatted_text_keeps_early_chunks():
    body = (
        "[file name]: complaint.pdf\n[chunk 0]:\nFIRST\n[chunk 1]:\nSECOND\n\n====\n\n"
        "[file name]: other.pdf\n[chunk 0]:\nTHIRD"
    )
    truncated, was_truncated = everything.truncate_verbatim_formatted_text(body, 25)
    assert was_truncated
    assert "FIRST" in truncated
    assert "THIRD" not in truncated
    assert everything.VERBATIM_TRUNCATION_NOTICE in truncated


def test_sample_text_begin_middle_end_under_cap_unchanged():
    text = "alpha beta gamma"
    assert everything._sample_text_begin_middle_end(text, 1000) == text


def test_sample_text_begin_middle_end_over_cap_keeps_head_and_tail():
    text = "HEAD " + ("mid " * 6000) + " TAIL"
    sampled = everything._sample_text_begin_middle_end(text, 200)
    assert everything.num_tokens_from_string(sampled) <= 200 * 1.5  # within the per-third budget
    assert sampled.startswith("HEAD")
    assert sampled.rstrip().endswith("TAIL")
    assert everything.RELEVANT_EXTRACT_DOTS in sampled
    assert len(sampled) < len(text)


def test_truncate_text_to_tokens_respects_budget():
    from backend.ai.base import truncate_text_to_tokens

    text = "alpha beta gamma delta"
    assert truncate_text_to_tokens(text, 100) == text
    shortened = truncate_text_to_tokens(text, 2)
    assert shortened.endswith("...")
    assert len(shortened) < len(text)


def test_estimate_multichat_input_tokens_increases_with_history():
    base = everything.estimate_multichat_input_tokens(
        sys_prompt="system",
        session_context_msg=None,
        chat_history_reduced=[],
        new_prompt="prompt",
    )
    with_history = everything.estimate_multichat_input_tokens(
        sys_prompt="system",
        session_context_msg=None,
        chat_history_reduced=[("question one", "answer one")],
        new_prompt="prompt",
    )
    assert with_history > base


def test_cap_file_extractions_keeps_context_above_old_272k_budget(monkeypatch):
    monkeypatch.setattr(everything, "estimate_multichat_input_tokens", lambda **kwargs: 300_000)
    file_context = "SOURCE " * 100

    capped, meta = everything.cap_file_extractions_for_prompt(
        file_extractions_str=file_context,
        selected_model="gpt-6.1-sol",
        sys_prompt="system prompt",
        session_context_msg=None,
        chat_history_reduced=[],
        prompt_kind="answer",
        user_message="summarize",
        selected_section="Not Specified",
        current_draft={},
        use_uploaded_files=True,
        verbatim_draft_hint="",
    )

    assert capped == file_context
    assert meta["truncated"] is False
    assert meta["context_limit"] == 1_050_000
    assert meta["verbatim_budget"] == 1_050_000 - 128_000 - 2_048 - 300_000


def test_cap_file_extractions_skips_unknown_model_budget(monkeypatch):
    def _unexpected_estimate(**kwargs):
        raise AssertionError("unknown models must not use an invented local context limit")

    monkeypatch.setattr(everything, "estimate_multichat_input_tokens", _unexpected_estimate)
    file_context = "original source text"

    capped, meta = everything.cap_file_extractions_for_prompt(
        file_extractions_str=file_context,
        selected_model="unregistered-model",
        sys_prompt="system prompt",
        session_context_msg=None,
        chat_history_reduced=[],
        prompt_kind="answer",
        user_message="summarize",
        selected_section="Not Specified",
        current_draft={},
        use_uploaded_files=True,
        verbatim_draft_hint="",
    )

    assert capped == file_context
    assert meta["truncated"] is False
    assert meta["context_limit"] is None
    assert meta["verbatim_budget"] is None


def test_cap_file_extractions_trims_every_file_fairly(monkeypatch):
    # Unified per-file budgeting: when the combined verbatim + relevant context exceeds the window,
    # the budget is split evenly across every file block and each is trimmed via begin+middle+end,
    # so every selected file stays represented (no whole-file drop) and the total shrinks.
    verbatim_body = (
        "FILE_A_MARKER " + ("alpha " * 4000)
        + "\n\n====\n\n"
        + "FILE_B_MARKER " + ("beta " * 4000)
    )
    merged = everything.merge_verbatim_and_extracted_context(
        everything.VERBATIM_BLOCK_HEADER + verbatim_body,
        "KEEP_THIS_SUMMARY",
    )
    # Window leaves a per-file share large enough to keep each marker but far below the ~8k
    # combined context, so trimming engages while every file stays represented.
    monkeypatch.setattr(everything, "MODEL_CONTEXT_LIMITS", {"gpt-5.4": 5000})
    monkeypatch.setattr(everything, "MODEL_COMPLETION_RESERVE", 100)
    monkeypatch.setattr(everything, "PROMPT_TOKEN_SAFETY_MARGIN", 100)

    capped, meta = everything.cap_file_extractions_for_prompt(
        file_extractions_str=merged,
        selected_model="gpt-5.4",
        sys_prompt="system prompt",
        session_context_msg=None,
        chat_history_reduced=[("prior", "response")],
        prompt_kind="create",
        user_message="draft paragraph responses",
        selected_section="Not Specified",
        current_draft={},
        use_uploaded_files=True,
        verbatim_draft_hint="",
    )

    assert meta["truncated"] is True
    # both section headers preserved
    assert everything.VERBATIM_BLOCK_HEADER.strip() in capped
    assert everything.EXTRACTED_CONTEXT_HEADER.strip() in capped
    # every file block still represented (none dropped wholesale)
    assert "FILE_A_MARKER" in capped
    assert "FILE_B_MARKER" in capped
    assert "KEEP_THIS_SUMMARY" in capped.split(everything.EXTRACTED_CONTEXT_HEADER)[-1]
    # combined context shrank, and each oversized file was sampled (dotted marker present)
    assert len(capped) < len(merged)
    assert everything.RELEVANT_EXTRACT_DOTS in capped


# ---- Hallucination guard: shared standalone engine adapter ----

def test_verify_draft_cases_delegates_to_shared_lookup(monkeypatch):
    expected_cites = [{"displayName": "Smith v. Jones\t123 F.2d 1", "form": "123 F.2d 1"}]
    expected_diagnostics = [{"citation": "123 F.2d 1", "lookup_tag": "VERIFIED_FL"}]
    calls = []

    def fake_lookup(text):
        calls.append(text)
        return expected_cites, expected_diagnostics

    monkeypatch.setattr(everything._hg, "verify_citations_with_lookup_tags_lookup_safe", fake_lookup)

    cites, diagnostics, complete = everything._verify_draft_cases_via_courtlistener("See 123 F.2d 1.")

    assert calls == ["See 123 F.2d 1."]
    assert cites == expected_cites
    assert diagnostics == expected_diagnostics
    assert complete is True


def test_guard_results_to_html_partial_coverage_note(monkeypatch):
    import backend.constants as constants

    monkeypatch.setattr(constants, "ENVIRONMENT", "test")

    assert "Partial review" in everything._guard_results_to_html([], partial_coverage=True)
    html = everything._guard_results_to_html([], partial_coverage=False)
    assert "Partial review" not in html
    assert "Possible Issues" not in html

    monkeypatch.setattr(constants, "ENVIRONMENT", "dev-monica")
    assert "Possible Issues" in everything._guard_results_to_html([], partial_coverage=False)


def test_guard_results_to_html_renders_lookup_possible_issue(monkeypatch):
    import backend.constants as constants

    monkeypatch.setattr(constants, "ENVIRONMENT", "dev-monica")

    html = everything._guard_results_to_html([{
        "case_name": "Three Arrows",
        "citation": "647 B.R. 440",
        "citation_as_drafted": "Three Arrows, 647 B.R. 440",
        "is_valid": False,
        "is_applicable": None,
        "lookup_tag": "LACKS_OPINION_DATA",
        "possible_issue": "We could not confirm this citation from CourtListener. It may be a database gap or an incorrect citation; check Westlaw or Lexis.",
    }], partial_coverage=False)

    assert "Possible Issues" in html
    assert "LACKS_OPINION_DATA" in html
    assert "could not confirm this citation from CourtListener" in html
    assert 'class="hg-tooltip-cell"' in html
    assert "<th" in html and 'class="hg-tooltip-cell"' in html
    assert not re.search(r"<td\b[^>]*\bhg-tooltip-cell\b", html)
    assert html.count('data-tooltip="Developer diagnostic details about lookup or citation matching."') == 1


def test_guard_results_to_html_shows_inapplicability_reason_outside_monica(monkeypatch):
    import backend.constants as constants

    monkeypatch.setattr(constants, "ENVIRONMENT", "production")

    reason = "The draft cites the case for an evidentiary rule it does not address."
    html = everything._guard_results_to_html([{
        "case_name": "Smith v. Jones",
        "citation": "123 F.2d 456",
        "citation_as_drafted": "Smith v. Jones, 123 F.2d 456",
        "is_valid": True,
        "is_applicable": False,
        "reason": reason,
    }], partial_coverage=False)

    assert "Possible Issues" not in html
    assert "Does not support:" in html
    assert reason in html
    assert not re.search(r"<td\b[^>]*\bhg-tooltip-cell\b", html)
    assert html.count('data-tooltip="Whether the authority supports the proposition in your document."') == 1


def test_guard_statutes_to_html_empty_and_nonempty():
    empty_html = everything._guard_statutes_to_html([])
    assert "Statute review" in empty_html
    assert "No statute citations found" in empty_html

    html = everything._guard_statutes_to_html(
        [{"citation": "17 U.S.C. § 411", "citation_as_drafted": "§ 411", "location": "Section 1",
          "is_valid": True, "is_applicable": True, "reason": ""}]
    )
    assert "Statute review" in html
    assert "17 U.S.C. § 411" in html
    assert "§ 411" in html
    assert "Citation as drafted" in html
    assert "Provision found" in html
    assert 'class="hg-tooltip-cell"' in html
    assert "<th" in html and 'class="hg-tooltip-cell"' in html
    assert not re.search(r"<td\b[^>]*\bhg-tooltip-cell\b", html)
    assert html.count('data-tooltip="The authority Hallucination Guard matched for review."') == 1
    assert html.count('data-tooltip="Whether the cited statute appears to be a real provision."') == 1


def test_guard_statutes_to_html_hides_possible_issues_outside_monica(monkeypatch):
    import backend.constants as constants

    monkeypatch.setattr(constants, "ENVIRONMENT", "production")

    html = everything._guard_statutes_to_html([{
        "citation": "99 U.S.C. § 9999",
        "citation_as_drafted": "§ 9999",
        "location": "Section 5",
        "is_valid": False,
        "is_applicable": False,
        "reason": "Title 99 does not exist in the United States Code.",
    }])

    assert "Possible Issues" not in html
    assert "Title 99 does not exist in the United States Code." not in html


def test_guard_result_is_valid_tolerates_both_field_names():
    assert everything._guard_result_is_valid({"is_valid": True}) is True
    assert everything._guard_result_is_valid({"is_validated": True}) is True
    assert everything._guard_result_is_valid({"is_valid": False}) is False
    assert everything._guard_result_is_valid({}) is False


def test_guard_build_link_map_and_attach_links():
    draft_cites = [
        {"form": "123 F.2d 456", "citationName": "123-F_dot_2d-456", "prefix": "/CaseViewer", "id": 99,
         "cluster_id": 99, "lookup_tag": "VERIFIED_FL", "displayName": "Smith v. Jones\t123 F.2d 456",
         "type": "case"},
        {"form": "2010 WI 78", "citationName": "2010-WI-78", "prefix": "/CaseViewer", "id": 8238267,
         "cluster_id": 8238267, "lookup_tag": "CASE_NAME_MISMATCH",
         "displayName": "Blum ex rel. Studinski v. 1st Auto & Casualty Insurance\t2010 WI 78",
         "type": "case"},
        {"form": "647 B.R. 440", "citationName": "647-B_dot_R_dot_-440", "prefix": "/CaseViewer", "id": None,
         "cluster_id": None, "lookup_tag": "LACKS_OPINION_DATA",
         "displayName": "647 B.R. 440", "type": "case"},
        {"form": "11 U.S.C. § 101", "citationName": "11/101",
         "prefix": "https://www.law.cornell.edu/uscode/text", "type": "us"},
    ]
    link_map = everything._guard_build_citation_link_map(draft_cites, "/CaseViewer/pub-1")
    case_results = [{"citation": "123 F.2d 456",
                     "citation_as_drafted": "Smith, 123 F.2d 456, 460 (9th Cir. 1990)"}]
    mismatch_results = [{"citation": "2010 WI 78", "citation_as_drafted": "Wrong Case, 2010 WI 78"}]
    no_cluster_results = [{"citation": "647 B.R. 440", "citation_as_drafted": "Three Arrows, 647 B.R. 440"}]
    statute_results = [{"citation": "11 U.S.C. § 101", "citation_as_drafted": "§ 101"}]

    everything._guard_attach_links(case_results, link_map)
    everything._guard_attach_links(mismatch_results, link_map)
    everything._guard_attach_links(no_cluster_results, link_map)
    everything._guard_attach_links(statute_results, link_map)

    assert case_results[0]["href"] == "/CaseViewer/pub-1/123-F_dot_2d-456&99"
    assert mismatch_results[0]["href"] == "/CaseViewer/pub-1/2010-WI-78&8238267"
    assert "href" not in no_cluster_results[0]
    assert statute_results[0]["href"] == "https://www.law.cornell.edu/uscode/text/11/101"


def test_guard_results_to_html_merges_case_name_and_shows_as_drafted():
    html = everything._guard_results_to_html([{
        "citation": "123 F.2d 456",
        "citation_as_drafted": "Smith, 123 F.2d 456, 460 (9th Cir. 1990)",
        "case_name": "Smith v. Jones",
        "location": "Section 2",
        "is_valid": True,
        "is_applicable": True,
        "reason": "",
    }])
    assert "Citation review" in html
    assert "Citation as drafted" in html
    assert "Smith v. Jones 123 F.2d 456" in html
    assert "460 (9th Cir. 1990)" in html


def test_hallucination_guard_task_uses_shared_engine(monkeypatch):
    state = {
        "draft": {
            "title": "Brief",
            "sections": [{"section_id": "1", "section_name": "Argument", "section_content": "See 123 F.2d 1."}],
        },
        "validated_cases": {},
        "chat_history": [],
        "conversation_brief": "",
        "constraints": {},
    }
    captured = {}
    saves = []

    monkeypatch.setattr(everything, "DBMemory", lambda db, doc_id: {"doc_id": doc_id})
    monkeypatch.setattr(everything, "load_state_from_memory", lambda mem: state)
    monkeypatch.setattr(everything, "save_state_to_memory", lambda s, mem, **kw: saves.append(copy.deepcopy(s)))
    monkeypatch.setattr(
        everything, "ChatThread",
        SimpleNamespace(query=SimpleNamespace(get=lambda _doc_id: SimpleNamespace(public_id="pub-1"))),
    )

    def fake_run_hg(draft_text, **kwargs):
        captured["draft_text"] = draft_text
        captured["case_viewer_prefix"] = kwargs.get("case_viewer_prefix")
        captured["source_label"] = kwargs.get("source_label")
        kwargs["state"].setdefault("validated_cases", {})["123 F.2d 1"] = {"case_name": "Smith"}
        return {"response": "<p>shared hg result</p>"}

    monkeypatch.setattr(everything._hg, "run_hallucination_guard_on_text", fake_run_hg)

    result = everything.hallucination_guard_task.run({"doc_id": 1})

    assert "[Section 1: Argument]" in captured["draft_text"]
    assert captured["case_viewer_prefix"] == "/CaseViewer/pub-1"
    assert captured["source_label"] == "draft"
    assert result == {"response": "<p>shared hg result</p>", "text": state["draft"]}
    assert state["chat_history"][-1] == ("check cases for hallucination", "<p>shared hg result</p>")
    assert saves[-1]["validated_cases"]["123 F.2d 1"]["case_name"] == "Smith"


def test_phase2_uses_saved_planning_conversation_context_once(monkeypatch):
    captured = {}
    def answer(**kwargs):
        captured.update(kwargs)
        return 'A narrower request.'
    _run_phase2_with_prompt_turn_meta(
        monkeypatch,
        payload_updates={'conversation_context': {'version': 1, 'text': 'RFP 7 concerns source-control backups.'}},
        llm_call=answer)
    assert captured['context_message'] == 'RFP 7 concerns source-control backups.'
    assert captured['prev_prompt_output'] == []
