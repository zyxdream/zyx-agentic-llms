"""Case Assistant phase-1 cache provenance and stale-context regression tests."""

from types import SimpleNamespace

import pytest

from backend import everything


def _decision(relevant_ids, *, verbatim_ids=(), context_policy="reuse"):
    selected_files = [
        {"file_id": uid, "role": "supporting_context", "method": "relevant"}
        for uid in relevant_ids
    ] + [
        {"file_id": uid, "role": "citation_source", "method": "verbatim"}
        for uid in verbatim_ids
    ]
    selected_ids = list(dict.fromkeys([*relevant_ids, *verbatim_ids]))
    return {
        "draft_action": "answer_only",
        "revision_mode": "general_revision",
        "needs_confirmation": False,
        "selected_files": selected_files,
        "file_selection": {"selected_files": selected_files, "primary_draft_file_id": None},
        "context_policy": context_policy,
        "use_uploaded_files": bool(selected_ids),
        "refresh_context": context_policy == "refresh",
        "file_scope": "all_matter_and_chat",
        "extraction_method": "relevant",
        "all_uploaded_files_unique_ids": selected_ids,
        "target_file_unique_ids": selected_ids,
        "verbatim_file_unique_ids": list(verbatim_ids),
        "create_new_draft": False,
        "change_draft_requested": False,
    }


def _run_phase1(monkeypatch, *, previous, decision, fresh_text="fresh text", verbatim_text="",
                mode="deep", route_calls=None):
    state = {"chat_history": [], "draft": {}, "last_extraction": previous}
    retrieval_calls = []
    verbatim_calls = []
    monkeypatch.setattr(
        everything,
        "ChatThread",
        SimpleNamespace(resolve_id=lambda _draft_id: 1, query=SimpleNamespace(get=lambda _doc_id: None)),
    )
    monkeypatch.setattr(everything, "DBMemory", lambda **_kwargs: object())
    monkeypatch.setattr(everything, "load_state_from_memory", lambda _memory: state)
    monkeypatch.setattr(everything, "save_state_to_memory", lambda _state, _memory, **_kwargs: None)
    monkeypatch.setattr(everything, "analytics_record", lambda *_args, **_kwargs: None)
    def classify(*_args, **kwargs):
        if route_calls is not None:
            route_calls.append(("intent", kwargs["model"], kwargs["reasoning_effort"]))
        return decision.copy()

    monkeypatch.setattr(everything, "classify_intent_decision", classify)

    def build_relevant(*_args, **kwargs):
        if route_calls is not None:
            route_calls.append(("extraction", kwargs["model"], kwargs["reasoning_effort"]))
        retrieval_calls.append(kwargs["file_unique_ids"])
        everything._persist_cached_extraction(
            kwargs["state"],
            kwargs["user_message"],
            "new-key",
            fresh_text,
            relevant_file_ids=kwargs["file_unique_ids"],
        )
        return fresh_text, "new-key"

    def build_verbatim(**kwargs):
        verbatim_calls.append(kwargs["file_unique_ids"])
        return verbatim_text

    monkeypatch.setattr(everything, "build_relevant_extraction", build_relevant)
    monkeypatch.setattr(everything, "build_verbatim_file_extractions", build_verbatim)
    task = SimpleNamespace(
        request=SimpleNamespace(id="task-1"),
        update_state=lambda **_kwargs: None,
    )
    payload = everything._everything_phase1_body(
        task,
        {"draftId": "draft-1", "query": "Review the selected files.", "assistant_mode": mode},
    )
    return payload, state, retrieval_calls, verbatim_calls


@pytest.mark.parametrize("mode,model", [("fast", "gpt-6-luna"), ("deep", "gpt-6.1-sol")])
def test_phase1_routes_planner_and_extraction_from_mode(monkeypatch, mode, model):
    calls = []
    payload, _, _, _ = _run_phase1(
        monkeypatch, previous={}, decision=_decision(["A"], context_policy="refresh"),
        mode=mode, route_calls=calls,
    )
    assert calls == [("intent", model, "medium"), ("extraction", model, "medium")]
    assert payload["selected_model"] == model
    assert payload["assistant_mode"] == mode


def test_relevant_extraction_stamps_sorted_unique_file_ids(monkeypatch):
    monkeypatch.setattr(everything, "_build_full_content_file_entries", lambda **_kwargs: [])
    monkeypatch.setattr(everything, "_extract_relevant_entries_threaded", lambda *_args, **_kwargs: [])
    state = {}

    text, _cache_key = everything.build_relevant_extraction(
        "question", 1, None, state, "all_matter_and_chat", ["B", "A", "B"]
    )

    assert text == ""
    assert state["last_extraction"]["relevant_file_ids"] == ["A", "B"]


@pytest.mark.parametrize("cached_ids", [["A", "B"], ["B", "A", "B"]])
def test_same_relevant_files_reuse_context_regardless_of_order(monkeypatch, cached_ids):
    previous = {"file_extractions_str": "cached A+B", "relevant_file_ids": cached_ids}
    payload, _state, retrieval_calls, _ = _run_phase1(
        monkeypatch, previous=previous, decision=_decision(["B", "A"])
    )

    assert retrieval_calls == []
    assert payload["refresh_context"] is False
    assert payload["file_extractions_str"] == "cached A+B"


@pytest.mark.parametrize("cached_ids,selected_ids", [
    (["A"], ["B"]),
    (["A"], ["A", "B"]),
    (["A", "B"], ["A"]),
    (None, ["A"]),
])
def test_changed_or_legacy_cache_forces_fresh_relevant_extraction(
    monkeypatch, cached_ids, selected_ids
):
    previous = {"file_extractions_str": "old A text"}
    if cached_ids is not None:
        previous["relevant_file_ids"] = cached_ids
    payload, state, retrieval_calls, _ = _run_phase1(
        monkeypatch,
        previous=previous,
        decision=_decision(selected_ids),
        fresh_text="fresh selected text",
    )

    assert retrieval_calls == [selected_ids]
    assert payload["refresh_context"] is True
    assert state["last_intent_meta"]["context_policy"] == "refresh"
    assert payload["file_extractions_str"] == "fresh selected text"


def test_empty_forced_refresh_does_not_reuse_prior_file_text(monkeypatch):
    previous = {"file_extractions_str": "old A text", "relevant_file_ids": ["A"]}
    payload, state, retrieval_calls, _ = _run_phase1(
        monkeypatch, previous=previous, decision=_decision(["B"]), fresh_text=""
    )

    assert retrieval_calls == [["B"]]
    assert payload["file_extractions_str"] == ""
    assert state["last_extraction"]["file_extractions_str"] == ""


def test_empty_planned_refresh_does_not_reuse_prior_file_text(monkeypatch):
    previous = {"file_extractions_str": "old A text", "relevant_file_ids": ["A"]}
    payload, _state, retrieval_calls, _ = _run_phase1(
        monkeypatch,
        previous=previous,
        decision=_decision(["A"], context_policy="refresh"),
        fresh_text="",
    )

    assert retrieval_calls == [["A"]]
    assert payload["file_extractions_str"] == ""


def test_mixed_selection_reuses_only_matching_relevant_context_and_loads_verbatim(monkeypatch):
    previous = {"file_extractions_str": "cached A excerpt", "relevant_file_ids": ["A"]}
    payload, _state, retrieval_calls, verbatim_calls = _run_phase1(
        monkeypatch,
        previous=previous,
        decision=_decision(["A"], verbatim_ids=["B"]),
        verbatim_text="new B full text",
    )

    assert retrieval_calls == []
    assert verbatim_calls == [["B"]]
    assert "cached A excerpt" in payload["file_extractions_str"]
    assert "new B full text" in payload["file_extractions_str"]


def test_verbatim_only_selection_does_not_reuse_old_relevant_context(monkeypatch):
    previous = {"file_extractions_str": "old A text", "relevant_file_ids": ["A"]}
    payload, _state, retrieval_calls, verbatim_calls = _run_phase1(
        monkeypatch,
        previous=previous,
        decision=_decision([], verbatim_ids=["B"]),
        verbatim_text="new B full text",
    )

    assert retrieval_calls == []
    assert verbatim_calls == [["B"]]
    assert "new B full text" in payload["file_extractions_str"]
    assert "old A text" not in payload["file_extractions_str"]


def test_empty_relevant_result_keeps_fresh_verbatim_without_old_context(monkeypatch):
    previous = {"file_extractions_str": "old A text", "relevant_file_ids": ["A"]}
    payload, _state, retrieval_calls, verbatim_calls = _run_phase1(
        monkeypatch,
        previous=previous,
        decision=_decision(["C"], verbatim_ids=["B"]),
        fresh_text="",
        verbatim_text="new B full text",
    )

    assert retrieval_calls == [["C"]]
    assert verbatim_calls == [["B"]]
    assert "new B full text" in payload["file_extractions_str"]
    assert "old A text" not in payload["file_extractions_str"]
