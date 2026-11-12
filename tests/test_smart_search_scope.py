"""Doc Search must never reuse file context from another effective file scope."""

from copy import deepcopy
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend import everything, smart_search
from backend.ai import base as ai_base


NO_FILES_MESSAGE = (
    "No files are available in the selected scope. "
    "Select a file or folder with documents and try again."
)
FILES = [
    SimpleNamespace(original_filename="first.pdf", file_size=100, unique_id="file-1"),
    SimpleNamespace(original_filename="second.pdf", file_size=200, unique_id="file-2"),
]


def _run(path, *, query="Find notice", scoped_file_ids=None, include_scope=False):
    data = {"project_id": 5, "query": query, "session_id": "session-1"}
    if include_scope:
        data["scoped_file_ids"] = scoped_file_ids
    if path == "live":
        return smart_search.smart_search_task.run(data)
    phase1 = smart_search.smart_search_phase1.run(data)
    if phase1.get("error"):
        return phase1
    return smart_search.smart_search_phase2.run(phase1)


@pytest.fixture
def task_env(monkeypatch):
    monkeypatch.setattr(smart_search, 'project_account_id', lambda _id: 1)
    monkeypatch.setattr(smart_search, 'account_execution', lambda _id: nullcontext())
    state = smart_search.create_empty_state()
    files = list(FILES)
    options = {"refresh": True, "file_scope": "all_matter_and_chat", "empty_result": False}
    classified = []
    retrieved = []
    answers = MagicMock(return_value="Answer")
    persisted = MagicMock()

    class FileQuery:
        def filter_by(self, **kwargs):
            assert kwargs == {"project_id": 5, "draft_id": None, "source": "search"}
            return self

        def all(self):
            return files

    monkeypatch.setattr(smart_search, "UploadedFiles", SimpleNamespace(query=FileQuery()))
    monkeypatch.setattr(smart_search, "load_state", lambda _session_id: state)
    monkeypatch.setattr(smart_search, "save_state", lambda _session_id, _state: None)
    monkeypatch.setattr(smart_search, "_is_cancelled", lambda *_args: False)
    monkeypatch.setattr(smart_search, "_clear_active_task", lambda *_args: None)
    monkeypatch.setattr(smart_search, "retrieve_relevant_chat_turns", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(smart_search, "multichat_completion_text", answers)
    monkeypatch.setattr(smart_search, "update_conversation_brief", lambda *_args, **_kwargs: ("", {}))
    monkeypatch.setattr(smart_search, "num_tokens_from_string", lambda _prompt: 1)
    monkeypatch.setattr(smart_search, "persist_turn", persisted)
    monkeypatch.setattr(smart_search.smart_search_task, "update_state", lambda **_kwargs: None)
    monkeypatch.setattr(smart_search.smart_search_phase1, "update_state", lambda **_kwargs: None)
    monkeypatch.setattr(smart_search.smart_search_phase2, "update_state", lambda **_kwargs: None)

    def classify(state_arg, _message, **kwargs):
        matter_files = kwargs["matter_files"]
        classified.append({
            "matter_ids": [f["unique_id"] for f in matter_files],
            "context": kwargs["file_extractions_str"],
            "last_extraction": deepcopy(state_arg.get("last_extraction")),
            "model": kwargs["model"],
        })
        ids = [f["unique_id"] for f in matter_files]
        planner_ids = options.get("planner_ids", ids)
        return (
            True, options["refresh"], options["file_scope"], "relevant",
            planner_ids, planner_ids, False, False, [],
        )

    def retrieve(_message, *, state, file_unique_ids, **kwargs):
        ids = list(file_unique_ids)
        retrieved.append({"ids": ids, "file_scope": kwargs["file_scope"], "model": kwargs["model"]})
        if options["empty_result"]:
            return ""
        uid = ids[0]
        filename = next(f.original_filename for f in files if f.unique_id == uid)
        context = f"Context from {filename}"
        state["last_extraction"] = {
            "file_extractions_str": context,
            "sources": [{"filename": filename, "unique_id": uid}],
            "source_chunks": [{
                "filename": filename, "unique_id": uid,
                "chunk_index": 0, "content": context,
            }],
        }
        return context

    monkeypatch.setattr(smart_search, "classify_intent", classify)
    monkeypatch.setattr(smart_search, "get_relevant_content_bm25", retrieve)
    return SimpleNamespace(
        state=state, files=files, options=options, classified=classified,
        retrieved=retrieved, answers=answers, persisted=persisted,
    )


def _scope(*, file_scope="all_matter_and_chat", file_ids=None):
    return {
        "version": 1,
        "project_id": 5,
        "file_scope": file_scope,
        "file_ids": ["file-1", "file-2"] if file_ids is None else file_ids,
    }


@pytest.mark.parametrize("path", ["live", "split"])
def test_fresh_search_stamps_effective_corpus_and_preserves_sources(path, task_env):
    result = _run(
        path, scoped_file_ids=["file-2", "file-2", "deleted", "file-1"], include_scope=True,
    )

    assert set(task_env.classified[0]["matter_ids"]) == {"file-1", "file-2"}
    assert set(task_env.retrieved[0]["ids"]) == {"file-1", "file-2"}
    assert len(task_env.retrieved[0]["ids"]) == 2
    assert task_env.state["last_extraction"]["smart_search_scope"] == _scope()
    assert result["sources"] == task_env.state["last_extraction"]["sources"]
    assert result["source_chunks"] == task_env.state["last_extraction"]["source_chunks"]
    assert task_env.persisted.call_args.kwargs["source_chunks"] == result["source_chunks"]


@pytest.mark.parametrize("path", ["live", "split"])
@pytest.mark.parametrize("selected", [[], ["deleted"]])
def test_empty_or_stale_explicit_selection_fails_before_llm_or_search(
    path, selected, task_env,
):
    result = _run(path, scoped_file_ids=selected, include_scope=True)

    assert result == {
        "error": True,
        "response": NO_FILES_MESSAGE,
        "error_message": NO_FILES_MESSAGE,
    }
    assert task_env.classified == []
    assert task_env.retrieved == []
    task_env.answers.assert_not_called()
    task_env.persisted.assert_not_called()
    assert task_env.state["chat_history"] == []


@pytest.mark.parametrize("path", ["live", "split"])
def test_empty_explicit_selection_clears_prior_extraction(path, task_env):
    task_env.state["last_extraction"] = {
        "file_extractions_str": "Old file context",
        "smart_search_scope": _scope(),
    }

    result = _run(path, scoped_file_ids=[], include_scope=True)

    assert result["error"] is True
    assert task_env.state["last_extraction"] is None
    assert task_env.classified == []
    assert task_env.retrieved == []


@pytest.mark.parametrize("path", ["live", "split"])
def test_unscoped_empty_project_never_calls_bm25_with_empty_ids(path, task_env):
    task_env.files.clear()

    result = _run(path)

    assert task_env.retrieved == []
    assert result["sources"] == []
    assert result["source_chunks"] == []


@pytest.mark.parametrize("path", ["live", "split"])
def test_partly_stale_explicit_selection_searches_only_surviving_file(path, task_env):
    result = _run(path, scoped_file_ids=["deleted", "file-2"], include_scope=True)

    assert task_env.classified[0]["matter_ids"] == ["file-2"]
    assert task_env.retrieved[0]["ids"] == ["file-2"]
    assert task_env.state["last_extraction"]["smart_search_scope"] == _scope(
        file_ids=["file-2"],
    )
    assert result["sources"] == [{"filename": "second.pdf", "unique_id": "file-2"}]


@pytest.mark.parametrize("path", ["live", "split"])
def test_null_selection_means_all_current_doc_search_files(path, task_env):
    _run(path, scoped_file_ids=None, include_scope=True)

    assert set(task_env.classified[0]["matter_ids"]) == {"file-1", "file-2"}
    assert set(task_env.retrieved[0]["ids"]) == {"file-1", "file-2"}
    assert task_env.state["last_extraction"]["smart_search_scope"] == _scope()


@pytest.mark.parametrize("path", ["live", "split"])
def test_classifier_cannot_broaden_explicit_file_selection(path, task_env):
    task_env.options["planner_ids"] = ["file-1", "file-2", "deleted"]

    _run(path, scoped_file_ids=["file-2"], include_scope=True)

    assert task_env.classified[0]["matter_ids"] == ["file-2"]
    assert task_env.retrieved[0]["ids"] == ["file-2"]
    assert task_env.state["last_extraction"]["smart_search_scope"] == _scope(
        file_ids=["file-2"],
    )


@pytest.mark.parametrize("path", ["live", "split"])
def test_same_scope_reuses_extraction_despite_selection_order_and_duplicates(path, task_env):
    first = _run(path, scoped_file_ids=["file-2", "file-1"], include_scope=True)
    task_env.options["refresh"] = False
    second = _run(path, query="Follow-up", scoped_file_ids=["file-1", "file-2", "file-1"], include_scope=True)

    assert len(task_env.retrieved) == 1
    assert task_env.classified[1]["context"] == "Context from first.pdf"
    assert task_env.state["last_extraction"]["smart_search_scope"] == _scope()
    assert second["sources"] == first["sources"]
    assert second["source_chunks"] == first["source_chunks"]


@pytest.mark.parametrize("path", ["live", "split"])
def test_changing_selected_files_discards_prior_context_before_classification(path, task_env):
    _run(path, scoped_file_ids=["file-1"], include_scope=True)
    task_env.options["refresh"] = False
    result = _run(path, query="Follow-up", scoped_file_ids=["file-2"], include_scope=True)

    assert len(task_env.retrieved) == 2
    assert task_env.retrieved[1]["ids"] == ["file-2"]
    assert task_env.classified[1]["context"] == ""
    assert task_env.classified[1]["last_extraction"] is None
    assert result["sources"] == [{"filename": "second.pdf", "unique_id": "file-2"}]
    assert result["source_chunks"][0]["unique_id"] == "file-2"


@pytest.mark.parametrize("path", ["live", "split"])
@pytest.mark.parametrize("change", ["add", "delete"])
def test_unscoped_corpus_change_forces_fresh_retrieval(path, change, task_env):
    _run(path)
    task_env.options["refresh"] = False
    if change == "add":
        task_env.files.append(SimpleNamespace(
            original_filename="third.pdf", file_size=300, unique_id="file-3",
        ))
    else:
        task_env.files.remove(FILES[1])
    _run(path, query="Follow-up")

    assert len(task_env.retrieved) == 2
    assert task_env.classified[1]["context"] == ""
    assert task_env.classified[1]["last_extraction"] is None
    assert task_env.state["last_extraction"]["smart_search_scope"]["file_ids"] == sorted(
        f.unique_id for f in task_env.files
    )


@pytest.mark.parametrize("path", ["live", "split"])
def test_file_scope_change_prevents_reuse_after_classification(path, task_env):
    _run(path)
    task_env.options.update(refresh=False, file_scope="chat_files_only")
    _run(path, query="Follow-up")

    assert len(task_env.retrieved) == 2
    assert task_env.retrieved[1]["file_scope"] == "chat_files_only"
    assert task_env.state["last_extraction"]["smart_search_scope"] == _scope(
        file_scope="chat_files_only",
    )


@pytest.mark.parametrize("path", ["live", "split"])
def test_legacy_unstamped_extraction_is_not_shown_to_classifier_or_reused(path, task_env):
    task_env.state["last_extraction"] = {
        "file_extractions_str": "Old unscoped context",
        "sources": [{"filename": "old.pdf", "unique_id": "old-file"}],
    }
    task_env.options["refresh"] = False

    result = _run(path)

    assert task_env.classified[0]["context"] == ""
    assert task_env.classified[0]["last_extraction"] is None
    assert len(task_env.retrieved) == 1
    assert result["sources"] != [{"filename": "old.pdf", "unique_id": "old-file"}]


@pytest.mark.parametrize("path", ["live", "split"])
def test_wrong_project_stamp_is_not_shown_to_classifier_or_reused(path, task_env):
    task_env.state["last_extraction"] = {
        "file_extractions_str": "Other matter's context",
        "smart_search_scope": {**_scope(), "project_id": 6},
    }
    task_env.options["refresh"] = False

    _run(path)

    assert task_env.classified[0]["context"] == ""
    assert task_env.classified[0]["last_extraction"] is None
    assert len(task_env.retrieved) == 1


@pytest.mark.parametrize("path", ["live", "split"])
def test_no_match_clears_previous_extraction_and_source_metadata(path, task_env):
    _run(path)
    task_env.options.update(refresh=True, empty_result=True)
    result = _run(path, query="Nothing matches")

    assert len(task_env.retrieved) == 2
    assert task_env.state["last_extraction"] is None
    assert result["sources"] == []
    assert result["source_chunks"] == []
    assert task_env.persisted.call_args.kwargs["sources"] == []
    assert task_env.persisted.call_args.kwargs["source_chunks"] == []
    assert "Context from first.pdf" not in task_env.answers.call_args.kwargs["new_prompt"]


@pytest.mark.parametrize("path", ["live", "split"])
def test_exact_bm25_cache_hit_gets_scope_stamp_without_keyword_call(
    path, task_env, monkeypatch,
):
    query = "Cached question"
    key = "cached question|scope=all_matter_and_chat|files=file-1"
    source = {"filename": "first.pdf", "unique_id": "file-1"}
    chunk = {**source, "chunk_index": 3, "content": "Cached matching passage"}
    task_env.state["extraction_cache"][key] = {
        "file_extractions_str": "Cached passage context",
        "sources": [source], "source_chunks": [chunk],
    }
    expand = MagicMock(side_effect=AssertionError("cache hit must not expand keywords"))
    monkeypatch.setattr(everything, "expand_search_keywords", expand)
    monkeypatch.setattr(smart_search, "get_relevant_content_bm25", everything.get_relevant_content_bm25)

    result = _run(path, query=query, scoped_file_ids=["file-1"], include_scope=True)

    assert task_env.state["last_extraction"]["smart_search_scope"] == _scope(
        file_ids=["file-1"],
    )
    assert result["sources"] == [source]
    assert result["source_chunks"] == [chunk]
    expand.assert_not_called()


@pytest.mark.parametrize("path", ["live", "split"])
def test_doc_search_provider_calls_use_base_default_despite_legacy_model(
    path, task_env, monkeypatch,
):
    """Keywords/answers use high; classifier/brief keep the base medium default."""
    assert ai_base.DEFAULT_MODEL == "gpt-6.1-sol"
    task_env.state["chat_history"] = [
        ("Was notice served?", "Notice was served."),
        ("Recent question", "Recent answer"),
        ("Latest question", "Latest answer"),
    ]
    parsed_outputs = iter([
        {
            "draft_action": "answer_only",
            "needs_confirmation": False,
            "confirmation_reason": None,
            "confirmation_options": [],
            "revision_mode": "general_revision",
            "selected_files": [{
                "file_id": "file-1",
                "role": "supporting_context",
                "method": "relevant",
            }],
            "context_policy": "refresh",
        },
        {"keywords": ["notice"]},
        {
            "brief": "Notice discussed.",
            "constraints": {
                "tone": None,
                "exclusions": [],
                "special_instructions": None,
                "cases": [],
            },
        },
    ])
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            id=f"response-{len(requests)}",
            status="completed",
            output_text="The notice is in first.pdf.",
            output=[],
            usage=SimpleNamespace(total_tokens=5),
        )

    def parse(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            id=f"response-{len(requests)}",
            status="completed",
            output_parsed=kwargs["text_format"].model_validate(next(parsed_outputs)),
            output=[],
            usage=SimpleNamespace(total_tokens=5),
        )

    provider = SimpleNamespace(responses=SimpleNamespace(create=create, parse=parse))
    monkeypatch.setattr(ai_base, "get_openai_client", lambda: provider)
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(smart_search, "classify_intent", everything.classify_intent)
    monkeypatch.setattr(smart_search, "get_relevant_content_bm25", everything.get_relevant_content_bm25)
    monkeypatch.setattr(smart_search, "retrieve_relevant_chat_turns", everything.retrieve_relevant_chat_turns)
    monkeypatch.setattr(smart_search, "multichat_completion_text", ai_base.multichat_completion_text)
    monkeypatch.setattr(smart_search, "update_conversation_brief", everything.update_conversation_brief)
    monkeypatch.setattr(everything, "bm25_search_chunks", lambda **_kwargs: [{
        "source_unique_id": "file-1",
        "source_filename": "first.pdf",
        "source_relative_path": None,
        "chunk_index": 0,
        "content": "The notice was delivered.",
        "bm25_score": 4.0,
    }])

    data = {
        "project_id": 5,
        "query": "Find notice",
        "session_id": "session-1",
        "model": "gpt-5.5",
    }
    if path == "live":
        result = smart_search.smart_search_task.run(data)
    else:
        phase1 = smart_search.smart_search_phase1.run(data)
        phase1["selected_model"] = "gpt-4.1"  # stale queued payload is ignored
        result = smart_search.smart_search_phase2.run(phase1)

    assert result["response"] == "The notice is in first.pdf."
    assert result["sources"] == [{"filename": "first.pdf", "unique_id": "file-1"}]
    assert len(requests) == 4
    assert all(request["model"] == ai_base.DEFAULT_MODEL for request in requests)
    # Classifier, file keywords, final answer, then brief. Older-chat ranking is local.
    assert [request["reasoning"] for request in requests] == [
        {"effort": effort} for effort in ["medium", "high", "high", "medium"]
    ]
    assert task_env.state["conversation_brief"] == "Notice discussed."


@pytest.mark.parametrize("path", ["live", "split"])
def test_doc_search_passes_base_default_to_older_chat_keyword_search(
    path, task_env, monkeypatch,
):
    task_env.state["chat_history"] = [
        ("Old question 1", "Old answer 1"),
        ("Old question 2", "Old answer 2"),
        ("Old question 3", "Old answer 3"),
    ]
    history_search = MagicMock(return_value="")
    monkeypatch.setattr(smart_search, "retrieve_relevant_chat_turns", history_search)

    _run(path)

    assert history_search.call_args.kwargs["model"] == ai_base.DEFAULT_MODEL
    assert history_search.call_args.kwargs["reasoning_effort"] == "high"


@pytest.mark.parametrize("path", ["live", "split"])
def test_doc_search_reads_base_default_at_task_runtime(path, task_env, monkeypatch):
    monkeypatch.setattr(ai_base, "DEFAULT_MODEL", "gpt-test-future-default")
    history_search = MagicMock(return_value="")
    brief_update = MagicMock(return_value=("", {}))
    monkeypatch.setattr(smart_search, "retrieve_relevant_chat_turns", history_search)
    monkeypatch.setattr(smart_search, "update_conversation_brief", brief_update)
    data = {"project_id": 5, "query": "Find notice", "session_id": "session-1", "model": "gpt-5.5"}

    if path == "live":
        smart_search.smart_search_task.run(data)
    else:
        phase1 = smart_search.smart_search_phase1.run(data)
        phase1["selected_model"] = "gpt-4.1"
        smart_search.smart_search_phase2.run(phase1)

    expected = "gpt-test-future-default"
    assert task_env.classified[0]["model"] == expected
    assert task_env.retrieved[0]["model"] == expected
    assert task_env.answers.call_args.kwargs["model"] == expected
    assert task_env.answers.call_args.kwargs["reasoning_effort"] == "high"
    assert history_search.call_args.kwargs["model"] == expected
    assert history_search.call_args.kwargs["reasoning_effort"] == "high"
    assert brief_update.call_args.kwargs["model"] == expected
