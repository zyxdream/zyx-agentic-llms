import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from backend.ai import base as ai_base
from backend import document_metadata_tasks
from backend.util import document_metadata


@pytest.fixture(autouse=True)
def metadata_owner_boundary(monkeypatch):
    # Metadata/provider unit tests supply fake rows; worker authorization uses
    # real database rows in test_disabled_upload_workers.py.
    monkeypatch.setattr(document_metadata_tasks, 'require_enabled_account', lambda _id: None)
    monkeypatch.setattr(document_metadata_tasks, 'project_account_id', lambda _id: 1)
    monkeypatch.setattr(document_metadata_tasks, 'chat_account_id', lambda _id: 1)


def test_default_document_metadata_allows_missing_values():
    meta = document_metadata.default_document_metadata()

    assert meta == {
        "schema_version": 1,
        "status": "missing",
        "summary": "",
        "document_kind": "unknown",
    }


def test_parse_metadata_invalid_json_returns_missing_default():
    meta = document_metadata.parse_metadata("not-json")

    assert meta["status"] == "missing"
    assert meta["summary"] == ""
    assert meta["document_kind"] == "unknown"


def test_normalize_document_metadata_limits_summary_to_three_sentences():
    meta = document_metadata.normalize_document_metadata({
        "status": "ready",
        "summary": "One. Two. Three. Four.",
        "document_kind": "report",
    })

    assert meta["status"] == "ready"
    assert meta["summary"] == "One. Two. Three."
    assert meta["document_kind"] == "report"


def test_build_metadata_excerpt_uses_front_and_back_for_long_text():
    text = "A" * 9000 + "B" * 5000

    excerpt = document_metadata.build_metadata_excerpt(text, front_chars=10, back_chars=10)

    assert excerpt.startswith("A" * 10)
    assert excerpt.endswith("B" * 10)
    assert "middle omitted" in excerpt


def test_extract_document_metadata_raises_on_empty_llm_output(monkeypatch):
    monkeypatch.setattr(document_metadata, "chat_completion_parse", lambda *args, **kwargs: None)

    with pytest.raises(ValueError):
        document_metadata.extract_document_metadata("Some document text", "doc.txt")


def test_document_metadata_task_writes_ready_metadata(monkeypatch):
    fake_file = SimpleNamespace(
        id=7,
        unique_id="file-7",
        original_filename="doc.txt",
        content="Some document text",
        source="search",
        metadata_json=None,
        draft_id=None, chat_thread_id=None, project_id=None,
    )
    fake_uploaded_files = SimpleNamespace(
        query=SimpleNamespace(get=lambda uploaded_file_id: fake_file)
    )
    fake_db = SimpleNamespace(session=SimpleNamespace(commit=MagicMock(), rollback=MagicMock()))

    monkeypatch.setattr(document_metadata_tasks, "UploadedFiles", fake_uploaded_files)
    monkeypatch.setattr(document_metadata_tasks, "db", fake_db)
    monkeypatch.setattr(
        document_metadata_tasks,
        "extract_document_metadata",
        lambda parsed_text, filename, **kwargs: {
            "schema_version": 1,
            "status": "ready",
            "summary": "A short summary.",
            "document_kind": "memo",
        },
    )

    result = document_metadata_tasks.document_metadata_task.run(7)

    assert result["ok"] is True
    assert result["metadata"]["status"] == "ready"
    assert '"summary": "A short summary."' in fake_file.metadata_json
    fake_db.session.commit.assert_called_once()


@pytest.mark.parametrize(
    ("source", "expected_model", "expected_reasoning"),
    [
        ("search", "gpt-6.1-sol", {"effort": "medium"}),
        ("everything", "gpt-6.1-sol", {"effort": "medium"}),
        (None, "gpt-6.1-sol", {"effort": "medium"}),
    ],
)
def test_document_metadata_task_routes_only_search_uploads_to_base_default(
    monkeypatch, source, expected_model, expected_reasoning
):
    fake_file = SimpleNamespace(
        id=7,
        unique_id="file-7",
        original_filename="doc.txt",
        content="Some document text",
        source=source,
        metadata_json=None,
        draft_id=None, chat_thread_id=None, project_id=None,
    )
    fake_uploaded_files = SimpleNamespace(
        query=SimpleNamespace(get=lambda uploaded_file_id: fake_file)
    )
    fake_db = SimpleNamespace(session=SimpleNamespace(commit=MagicMock(), rollback=MagicMock()))
    requests = []

    def responses_parse(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            id="resp_metadata",
            status="completed",
            model=kwargs["model"],
            output_parsed=document_metadata.DocumentMetadataResult(
                summary="A short summary.", document_kind="memo"
            ),
            output=[],
            usage=SimpleNamespace(total_tokens=17),
        )

    client = SimpleNamespace(responses=SimpleNamespace(parse=responses_parse))
    monkeypatch.setattr(document_metadata_tasks, "UploadedFiles", fake_uploaded_files)
    monkeypatch.setattr(document_metadata_tasks, "db", fake_db)
    monkeypatch.setattr(ai_base, "get_openai_client", lambda: client)
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", lambda *args, **kwargs: None)

    result = document_metadata_tasks.document_metadata_task.run(7)

    assert result["ok"] is True
    assert result["metadata"]["summary"] == "A short summary."
    assert len(requests) == 1
    assert requests[0]["model"] == expected_model
    assert requests[0].get("reasoning") == expected_reasoning
    assert requests[0]["text_format"] is document_metadata.DocumentMetadataResult


def test_search_metadata_reads_base_default_at_task_runtime(monkeypatch):
    fake_file = SimpleNamespace(
        id=7,
        unique_id="file-7",
        original_filename="doc.txt",
        content="Some document text",
        source="search",
        metadata_json=None,
        draft_id=None, chat_thread_id=None, project_id=None,
    )
    fake_uploaded_files = SimpleNamespace(
        query=SimpleNamespace(get=lambda uploaded_file_id: fake_file)
    )
    fake_db = SimpleNamespace(session=SimpleNamespace(commit=MagicMock(), rollback=MagicMock()))
    models = []

    def fake_extract_document_metadata(*, parsed_text, filename, model, reasoning_effort):
        models.append((model, reasoning_effort))
        return document_metadata.default_document_metadata("ready")

    monkeypatch.setattr(document_metadata_tasks, "UploadedFiles", fake_uploaded_files)
    monkeypatch.setattr(document_metadata_tasks, "db", fake_db)
    monkeypatch.setattr(document_metadata_tasks, "extract_document_metadata", fake_extract_document_metadata)
    monkeypatch.setattr(ai_base, "DEFAULT_MODEL", "gpt-test-future-default")

    assert document_metadata_tasks.document_metadata_task.run(7)["ok"] is True
    assert models == [("gpt-test-future-default", "medium")]
