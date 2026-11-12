"""Regression tests for Doc Search source-chunk metadata propagation."""

from datetime import datetime, timezone
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend import smart_search
from backend import everything


SOURCE = {"filename": "contract.pdf", "unique_id": "file-1"}
CHUNK = {**SOURCE, "chunk_index": 7, "content": "The notice was delivered on Tuesday."}
CONTEXT = "[contract.pdf]\nThe notice was delivered on Tuesday."


@pytest.fixture(autouse=True)
def search_owner_boundary(monkeypatch):
    # Source-format tests use fake persistence; revocation has database tests.
    monkeypatch.setattr(smart_search, 'project_account_id', lambda _id: 1)
    monkeypatch.setattr(smart_search, 'account_execution', lambda _id: nullcontext())


def _extraction(*, context=CONTEXT, sources=None, source_chunks=None):
    return {
        "file_extractions_str": context,
        "sources": [SOURCE] if sources is None else sources,
        "source_chunks": [CHUNK] if source_chunks is None else source_chunks,
    }


def _source_metadata(state, context=CONTEXT, use_uploaded_files=True):
    return smart_search._get_persistable_sources_and_source_chunks(
        state,
        use_uploaded_files=use_uploaded_files,
        file_extractions_str=context,
    )


def test_matching_bm25_extraction_returns_best_chunk_per_source():
    state = {
        "last_extraction": _extraction(
            sources=[SOURCE],
            source_chunks=[
                CHUNK,
                {
                    "filename": "memo.docx",
                    "unique_id": "file-2",
                    "chunk_index": 3,
                    "content": "Second matching passage.",
                },
            ],
        )
    }

    sources, chunks = _source_metadata(state)

    assert sources == [SOURCE, {"filename": "memo.docx", "unique_id": "file-2"}]
    assert chunks == state["last_extraction"]["source_chunks"]


def test_exact_bm25_cache_hit_preserves_chunk_metadata(monkeypatch):
    query = "notice of termination"
    file_ids = ["file-1"]
    cache_key = f"{query}|scope=all_matter_and_chat|files={','.join(file_ids)}"
    state = {
        "extraction_cache": {cache_key: _extraction()},
        "last_extraction": None,
    }
    keyword_call = MagicMock(side_effect=AssertionError("cache hit should not expand keywords"))
    monkeypatch.setattr(everything, "expand_search_keywords", keyword_call)

    context = everything.get_relevant_content_bm25(
        query,
        draft_id=None,
        state=state,
        project_id=5,
        file_unique_ids=file_ids,
    )

    assert context == CONTEXT
    assert _source_metadata(state, context) == ([SOURCE], [CHUNK])
    keyword_call.assert_not_called()


def test_fresh_bm25_retrieval_produces_highest_ranked_chunk_per_file(monkeypatch):
    state = smart_search.create_empty_state()
    rows = [
        {
            "source_unique_id": "file-1", "source_filename": "contract.pdf",
            "source_relative_path": None, "chunk_index": 7,
            "content": "The notice was delivered on Tuesday.", "bm25_score": 8.0,
        },
        {
            "source_unique_id": "file-2", "source_filename": "memo.docx",
            "source_relative_path": None, "chunk_index": 3,
            "content": "The memo confirms receipt.", "bm25_score": 6.0,
        },
        {
            "source_unique_id": "file-1", "source_filename": "contract.pdf",
            "source_relative_path": None, "chunk_index": 8,
            "content": "A weaker match from the same file.", "bm25_score": 2.0,
        },
    ]
    expand = MagicMock(return_value=["notice", "delivered"])
    search = MagicMock(return_value=rows)
    monkeypatch.setattr(everything, "expand_search_keywords", expand)
    monkeypatch.setattr(everything, "bm25_search_chunks", search)

    context = everything.get_relevant_content_bm25(
        "Where is the notice?", draft_id=None, state=state, top_k=60,
        model="gpt-5.5", project_id=5, file_unique_ids=["file-1", "file-2"],
    )

    assert context
    assert "The notice was delivered on Tuesday." in context
    assert _source_metadata(state, context) == (
        [SOURCE, {"filename": "memo.docx", "unique_id": "file-2"}],
        [CHUNK, {
            "filename": "memo.docx", "unique_id": "file-2",
            "chunk_index": 3, "content": "The memo confirms receipt.",
        }],
    )
    expand.assert_called_once_with("Where is the notice?", model="gpt-5.5", token_counter=None)
    assert search.call_args.kwargs["file_unique_ids"] == ["file-1", "file-2"]
    assert search.call_args.kwargs["top_k"] == 60


def test_legacy_top_chunk_is_returned_when_source_chunks_missing():
    state = {
        "last_extraction": {
            "file_extractions_str": CONTEXT,
            "sources": [],
            "top_chunk": CHUNK,
        }
    }

    assert _source_metadata(state) == ([SOURCE], [CHUNK])


@pytest.mark.parametrize(
    ("context", "use_uploaded_files"),
    [("", True), ("different answer context", True), (CONTEXT, False)],
)
def test_missing_or_mismatched_answer_context_never_returns_stale_chunks(
    context, use_uploaded_files
):
    state = {"last_extraction": _extraction()}

    assert _source_metadata(state, context, use_uploaded_files) == ([], [])


def test_full_content_without_chunk_metadata_keeps_sources_only():
    state = {"last_extraction": _extraction(source_chunks=[])}

    assert _source_metadata(state) == ([SOURCE], [])


def _install_task_mocks(monkeypatch, *, state, retrieval_context=CONTEXT, use_files=True):
    query_result = SimpleNamespace(
        all=lambda: [SimpleNamespace(original_filename="contract.pdf", file_size=200, unique_id="file-1")]
    )
    monkeypatch.setattr(
        smart_search,
        "UploadedFiles",
        SimpleNamespace(query=SimpleNamespace(filter_by=lambda **_kwargs: query_result)),
    )
    monkeypatch.setattr(smart_search, "load_state", lambda _session_id: state)
    monkeypatch.setattr(smart_search, "save_state", lambda _session_id, _state: None)
    monkeypatch.setattr(smart_search, "_is_cancelled", lambda *_args: False)
    monkeypatch.setattr(smart_search, "_clear_active_task", lambda *_args: None)
    monkeypatch.setattr(smart_search, "retrieve_relevant_chat_turns", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(smart_search, "multichat_completion_text", lambda **_kwargs: "Answer")
    monkeypatch.setattr(smart_search, "update_conversation_brief", lambda *_args, **_kwargs: ("", {}))
    monkeypatch.setattr(smart_search, "num_tokens_from_string", lambda _prompt: 1)
    monkeypatch.setattr(
        smart_search,
        "classify_intent",
        lambda *_args, **_kwargs: (
            use_files, True, "all_matter_and_chat", "relevant", ["file-1"], ["file-1"],
            False, False, [],
        ),
    )

    def fake_retrieval(*_args, **_kwargs):
        if retrieval_context:
            state["last_extraction"] = _extraction(context=retrieval_context)
        return retrieval_context

    monkeypatch.setattr(smart_search, "get_relevant_content_bm25", fake_retrieval)
    monkeypatch.setattr(smart_search.smart_search_task, "update_state", lambda **_kwargs: None)
    monkeypatch.setattr(smart_search.smart_search_phase1, "update_state", lambda **_kwargs: None)
    monkeypatch.setattr(smart_search.smart_search_phase2, "update_state", lambda **_kwargs: None)
    persisted = MagicMock()
    monkeypatch.setattr(smart_search, "persist_turn", persisted)
    return persisted


def test_live_task_returns_and_persists_bm25_chunks_for_relevant_method(monkeypatch):
    state = smart_search.create_empty_state()
    persisted = _install_task_mocks(monkeypatch, state=state)

    result = smart_search.smart_search_task.run(
        {"project_id": 5, "query": "Where is the notice?", "session_id": "session-1"}
    )

    assert result["sources"] == [SOURCE]
    assert result["source_chunks"] == [CHUNK]
    assert persisted.call_args.kwargs["sources"] == [SOURCE]
    assert persisted.call_args.kwargs["source_chunks"] == [CHUNK]


@pytest.mark.parametrize("use_files, retrieval_context", [(False, CONTEXT), (True, "")])
def test_live_task_does_not_attach_stale_chunks_without_file_context(
    monkeypatch, use_files, retrieval_context
):
    state = smart_search.create_empty_state()
    state["last_extraction"] = _extraction(context="Earlier query's passages")
    persisted = _install_task_mocks(
        monkeypatch, state=state, retrieval_context=retrieval_context, use_files=use_files
    )

    result = smart_search.smart_search_task.run(
        {"project_id": 5, "query": "New question", "session_id": "session-1"}
    )

    assert result["sources"] == []
    assert result["source_chunks"] == []
    assert persisted.call_args.kwargs["source_chunks"] in (None, [])


def test_split_phase_returns_and_persists_bm25_chunks(monkeypatch):
    state = smart_search.create_empty_state()
    persisted = _install_task_mocks(monkeypatch, state=state)
    input_data = {"project_id": 5, "query": "Where is the notice?", "session_id": "session-1"}

    phase1 = smart_search.smart_search_phase1.run(input_data)
    result = smart_search.smart_search_phase2.run(phase1)

    assert phase1["extraction_method"] == "relevant"
    assert result["sources"] == [SOURCE]
    assert result["source_chunks"] == [CHUNK]
    assert persisted.call_args.kwargs["source_chunks"] == [CHUNK]


def test_persisted_turn_reloads_same_source_chunks(monkeypatch):
    from backend import models, views

    rows = []
    session_row = SimpleNamespace(
        id="session-1",
        user_id=8,
        title="Notice search",
        created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        updated_at=None,
        conversation_brief="",
        constraints_json="{}",
    )

    class FakeMessage:
        created_at = None
        id = None

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.metadata_json = kwargs.get("metadata_json")

    class FakeQuery:
        def filter_by(self, **_kwargs):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return rows

    FakeMessage.query = FakeQuery()
    fake_db_session = MagicMock()
    fake_db_session.add.side_effect = rows.append
    monkeypatch.setattr(smart_search, "db", SimpleNamespace(session=fake_db_session))
    monkeypatch.setattr(smart_search, "SmartSearchMessage", FakeMessage)
    monkeypatch.setattr(
        smart_search,
        "SmartSearchSession",
        SimpleNamespace(query=SimpleNamespace(get=lambda _session_id: session_row)),
    )

    smart_search.persist_turn(
        "session-1", "Where is the notice?", "Answer", "", {},
        sources=[SOURCE], source_chunks=[CHUNK],
    )

    fake_db_session.commit.assert_called_once()
    monkeypatch.setattr(views, "current_user", SimpleNamespace(id=8))
    monkeypatch.setattr(
        views,
        "_SmartSearchSession",
        SimpleNamespace(query=SimpleNamespace(get=lambda _session_id: session_row)),
    )
    monkeypatch.setattr(models, "SmartSearchMessage", FakeMessage)
    body, status = views.smart_search_get_session.__wrapped__("session-1")

    assert status == 200
    assert body["messages"][1]["role"] == "assistant"
    assert body["messages"][1]["sources"] == [SOURCE]
    assert body["messages"][1]["source_chunks"] == [CHUNK]
