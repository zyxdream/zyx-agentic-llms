"""Tests for async upload helpers, per-file workers, and batch task OCR counts."""

import json
from types import SimpleNamespace
import pytest
from unittest.mock import MagicMock, patch, call

from backend.util.matter_upload import (
    MAX_FILES_PER_MATTER,
    MAX_MATTER_FILE_SIZE,
    MAX_MATTER_TOTAL_PAGES,
    matter_background_counts,
    classify_matter_upload_indices,
    enqueue_matter_upload_file_tasks,
    stage_matter_upload_files,
    cleanup_staged_files,
)
from backend.util.cloud_pdf_parser import MAX_CLOUD_PDF_OCR_TOTAL_TIMEOUT_SECONDS


@pytest.fixture(autouse=True)
def upload_owner_boundary(monkeypatch):
    # These I/O unit tests mock persistence; real owner/status checks are covered
    # with database-backed worker tests in test_disabled_upload_workers.py.
    from backend import upload_tasks
    monkeypatch.setattr(upload_tasks, 'require_enabled_account', lambda _id: None)
    monkeypatch.setattr(upload_tasks, 'project_account_id', lambda _id: 1)
    monkeypatch.setattr(upload_tasks, 'chat_account_id', lambda _id: 1)


class TestMatterBackgroundCounts:
    def test_constants(self):
        assert MAX_FILES_PER_MATTER == 30
        assert MAX_MATTER_TOTAL_PAGES is None
        assert MAX_MATTER_FILE_SIZE == 100 * 1024 * 1024
        assert MAX_CLOUD_PDF_OCR_TOTAL_TIMEOUT_SECONDS == 600

    @patch("backend.util.matter_upload.matter_background_files_filter", return_value=())
    @patch("backend.util.matter_upload.UploadedFiles")
    @patch("backend.util.matter_upload.db")
    def test_matter_background_counts_queries_with_source_filter(self, mock_db, mock_uploaded_files, mock_filter):
        mock_uploaded_files.query.filter.return_value.count.return_value = 5
        mock_db.session.query.return_value.filter.return_value.scalar.return_value = 120

        file_count, page_count = matter_background_counts(42)

        assert file_count == 5
        assert page_count == 120
        mock_filter.assert_called_once_with(42)
        mock_uploaded_files.query.filter.assert_called_once()


class TestClassifyMatterUploadIndices:
    def test_skips_unsupported_and_lock_files(self):
        good = MagicMock()
        good.filename = "doc.pdf"
        bad_ext = MagicMock()
        bad_ext.filename = "data.xlsx"
        lock = MagicMock()
        lock.filename = "~$doc.pdf"

        allowed, skipped = classify_matter_upload_indices(
            [good, bad_ext, lock],
            relative_paths=[None, None, None],
        )
        assert allowed == [0]
        assert len(skipped) == 2


class TestStageMatterUploadFiles:
    def test_rejects_oversized_before_save(self, tmp_path):
        file_mock = MagicMock()
        file_mock.filename = "big.pdf"
        file_mock.tell.return_value = MAX_MATTER_FILE_SIZE + 1
        file_mock.seek = MagicMock()

        staged, failed = stage_matter_upload_files(
            [file_mock],
            [0],
        )
        assert staged == []
        assert len(failed) == 1
        assert failed[0]["reason"] == "file_too_large"
        file_mock.save.assert_not_called()

    @patch("backend.util.matter_upload.os.path.join")
    def test_stages_allowed_file(self, mock_join, tmp_path):
        mock_join.return_value = str(tmp_path / "temp_test.pdf")
        file_mock = MagicMock()
        file_mock.filename = "doc.pdf"
        file_mock.tell.return_value = 1000
        file_mock.seek = MagicMock()

        staged, failed = stage_matter_upload_files([file_mock], [0])
        assert len(staged) == 1
        assert staged[0]["filename"] == "doc.pdf"
        assert staged[0]["temp_path"] == str(tmp_path / "temp_test.pdf")
        file_mock.save.assert_called_once()
        cleanup_staged_files(staged)


class TestMatterBackgroundPerFileEnqueue:
    def test_enqueues_one_task_per_staged_file(self, monkeypatch):
        from backend.upload_tasks import case_background_upload_file_task

        queued_payloads = []

        def fake_delay(payload):
            queued_payloads.append(payload)
            return MagicMock(id=f"task-{len(queued_payloads)}")

        monkeypatch.setattr(case_background_upload_file_task, "delay", fake_delay)

        staged_files = [
            {"filename": "a.pdf", "temp_path": "/tmp/a.pdf", "relative_path": "Folder/a.pdf"},
            {"filename": "b.pdf", "temp_path": "/tmp/b.pdf", "relative_path": "Folder/b.pdf"},
        ]

        enqueued = enqueue_matter_upload_file_tasks(
            project_id=7,
            staged_files=staged_files,
            folder_id=99,
        )

        assert [result.id for _staged, result in enqueued] == ["task-1", "task-2"]
        assert [payload["file"]["filename"] for payload in queued_payloads] == ["a.pdf", "b.pdf"]
        assert all(payload["project_id"] == 7 for payload in queued_payloads)
        assert all(payload["folder_id"] == 99 for payload in queued_payloads)


class TestUploadBatchTaskOcrCount:
    @patch("backend.upload_tasks.document_metadata_task.delay")
    @patch("backend.upload_tasks.process_project_uploaded_file")
    @patch("backend.upload_tasks.get_last_ocr_warning")
    @patch("backend.upload_tasks.db")
    def test_ocr_warning_count_in_loose_batch_result(self, mock_db, mock_ocr, mock_process, mock_metadata_delay):
        mock_db.session.rollback = MagicMock()
        mock_process.return_value = {
            "ok": True,
            "uploaded_file_id": 123,
            "unique_id": "u1",
            "filename": "a.pdf",
            "page_count": 1,
            "chunk_count": 2,
            "running_page_count": 1,
        }
        mock_ocr.side_effect = [True, False]

        from backend.upload_tasks import upload_batch_task

        payload = {
            "project_id": 1,
            "source": "everything",
            "folder_id": None,
            "existing_page_count": 0,
            "max_total_pages": 1000,
            "files": [
                {"filename": "a.pdf", "temp_path": "/tmp/a.pdf", "relative_path": None},
                {"filename": "b.pdf", "temp_path": "/tmp/b.pdf", "relative_path": None},
            ],
            "skipped_files": [],
        }

        with patch("backend.upload_tasks.os.path.exists", return_value=True):
            with patch("backend.upload_tasks.os.remove"):
                result = upload_batch_task.run(payload)

        assert result["ocr_warning_count"] == 1
        assert result["file_count"] == 2
        assert mock_metadata_delay.call_count == 2


class TestCaseBackgroundUploadFileTask:
    class FakeLock:
        def acquire(self):
            return True

        def release(self):
            return None

    class FakeRedis:
        def lock(self, *_args, **_kwargs):
            return TestCaseBackgroundUploadFileTask.FakeLock()

    @patch("backend.upload_tasks.document_metadata_task.delay")
    @patch("backend.upload_tasks.persist_project_uploaded_file")
    @patch("backend.upload_tasks.parse_project_uploaded_file")
    @patch("backend.upload_tasks.get_last_ocr_warning")
    @patch("backend.upload_tasks.db")
    def test_single_file_task_success_without_page_cap(
        self,
        mock_db,
        mock_ocr,
        mock_parse,
        mock_persist,
        mock_metadata_delay,
        monkeypatch,
    ):
        from backend.upload_tasks import case_background_upload_file_task

        monkeypatch.setattr("backend.upload_tasks._upload_redis", self.FakeRedis())
        mock_parse.return_value = ("parsed text", 5000)
        mock_persist.return_value = {
            "ok": True,
            "uploaded_file_id": 123,
            "unique_id": "u1",
            "filename": "large.pdf",
            "relative_path": None,
            "page_count": 5000,
            "chunk_count": 3,
            "running_page_count": 5000,
        }
        mock_ocr.return_value = True

        payload = {
            "project_id": 1,
            "folder_id": None,
            "file": {"filename": "large.pdf", "temp_path": "/tmp/large.pdf", "relative_path": None},
        }

        with patch("backend.upload_tasks.os.path.exists", return_value=True):
            with patch("backend.upload_tasks.os.remove"):
                result = case_background_upload_file_task.run(payload)

        assert result["file_count"] == 1
        assert result["total_chunks"] == 3
        assert result["ocr_warning_count"] == 1
        assert result["files"][0]["page_count"] == 5000
        persist_kwargs = mock_persist.call_args.kwargs
        assert persist_kwargs["max_total_pages"] is None
        assert persist_kwargs["max_project_file_count"] == MAX_FILES_PER_MATTER
        mock_metadata_delay.assert_called_once_with(123)

    @patch("backend.upload_tasks.persist_project_uploaded_file")
    @patch("backend.upload_tasks.parse_project_uploaded_file")
    @patch("backend.upload_tasks.db")
    def test_single_file_task_reports_file_count_limit(
        self,
        mock_db,
        mock_parse,
        mock_persist,
        monkeypatch,
    ):
        from backend.upload_tasks import case_background_upload_file_task

        monkeypatch.setattr("backend.upload_tasks._upload_redis", self.FakeRedis())
        mock_parse.return_value = ("parsed text", 1)
        mock_persist.return_value = {
            "ok": False,
            "filename": "extra.pdf",
            "reason": "file_count_limit",
            "error": "Maximum 30 files allowed per matter.",
        }

        payload = {
            "project_id": 1,
            "folder_id": None,
            "file": {"filename": "extra.pdf", "temp_path": "/tmp/extra.pdf", "relative_path": None},
        }

        with patch("backend.upload_tasks.os.path.exists", return_value=True):
            with patch("backend.upload_tasks.os.remove"):
                result = case_background_upload_file_task.run(payload)

        assert result["file_count"] == 0
        assert result["failed_files"] == [{
            "filename": "extra.pdf",
            "error": "Maximum 30 files allowed per matter.",
            "reason": "file_count_limit",
        }]


class TestSmartSearchUploadFileTask:
    @patch("backend.upload_tasks.document_metadata_task.delay")
    @patch("backend.upload_tasks.process_project_uploaded_file")
    @patch("backend.upload_tasks.get_last_ocr_warning")
    @patch("backend.upload_tasks.db")
    def test_single_file_task_success_without_page_cap(
        self,
        mock_db,
        mock_ocr,
        mock_process,
        mock_metadata_delay,
    ):
        from backend.upload_tasks import smart_search_upload_file_task

        mock_process.return_value = {
            "ok": True,
            "uploaded_file_id": 321,
            "unique_id": "u-search",
            "filename": "large-search.pdf",
            "relative_path": None,
            "page_count": 5000,
            "chunk_count": 4,
            "running_page_count": 5000,
        }
        mock_ocr.return_value = True

        payload = {
            "project_id": 7,
            "folder_id": None,
            "file": {"filename": "large-search.pdf", "temp_path": "/tmp/large-search.pdf", "relative_path": None},
        }

        with patch("backend.upload_tasks.os.path.exists", return_value=True):
            with patch("backend.upload_tasks.os.remove"):
                result = smart_search_upload_file_task.run(payload)

        assert result["file_count"] == 1
        assert result["total_chunks"] == 4
        assert result["ocr_warning_count"] == 1
        assert result["files"][0]["page_count"] == 5000
        process_kwargs = mock_process.call_args.kwargs
        assert process_kwargs["source"] == "search"
        assert process_kwargs["max_total_pages"] is None
        assert process_kwargs["running_page_count"] == 0
        assert "max_project_file_count" not in process_kwargs
        mock_metadata_delay.assert_called_once_with(321)

    @patch("backend.upload_tasks.process_project_uploaded_file")
    @patch("backend.upload_tasks.db")
    def test_single_file_task_failure_affects_only_that_file(
        self,
        mock_db,
        mock_process,
    ):
        from backend.upload_tasks import smart_search_upload_file_task

        mock_process.side_effect = RuntimeError("parse failed")

        payload = {
            "project_id": 7,
            "folder_id": None,
            "file": {"filename": "bad.pdf", "temp_path": "/tmp/bad.pdf", "relative_path": None},
        }

        with patch("backend.upload_tasks.os.path.exists", return_value=True):
            with patch("backend.upload_tasks.os.remove"):
                result = smart_search_upload_file_task.run(payload)

        assert result["file_count"] == 0
        assert result["total_files"] == 1
        assert result["failed_files"] == [{
            "filename": "bad.pdf",
            "error": "parse failed",
            "reason": "processing_error",
        }]
        mock_db.session.rollback.assert_called_once()


class TestCaseAssistantUploadFileTask:
    def test_constants(self):
        from backend import upload_tasks
        from backend import views

        assert upload_tasks.CASE_ASSISTANT_MAX_FILE_SIZE == 50 * 1024 * 1024
        assert upload_tasks.CASE_ASSISTANT_MAX_FILE_SIZE_MB == 50
        assert views.CASE_ASSISTANT_MAX_FILE_SIZE == 50 * 1024 * 1024
        assert views.CASE_ASSISTANT_MAX_FILE_SIZE_MB == 50
        assert views.CASE_ASSISTANT_MAX_FILES == upload_tasks.CASE_ASSISTANT_MAX_FILES == 100
        assert views.CASE_ASSISTANT_MAX_TOTAL_BYTES == upload_tasks.CASE_ASSISTANT_MAX_TOTAL_BYTES == 1024**3

    @patch("backend.upload_tasks.os.remove")
    @patch("backend.upload_tasks.os.path.getsize")
    @patch("backend.upload_tasks.os.path.exists")
    @patch("backend.upload_tasks.UploadedFiles")
    @patch("backend.upload_tasks.ChatThread.resolve_id")
    def test_worker_rejects_file_over_50mb(
        self,
        mock_resolve_id,
        mock_uploaded_files,
        mock_exists,
        mock_getsize,
        mock_remove,
    ):
        from backend import upload_tasks

        mock_exists.return_value = True
        mock_getsize.return_value = upload_tasks.CASE_ASSISTANT_MAX_FILE_SIZE + 1
        mock_resolve_id.return_value = 123
        mock_uploaded_files.query.filter_by.return_value.count.return_value = 0

        result = upload_tasks.case_assistant_upload_file_task.run({
            "draft_id": "draft-public-id",
            "client_upload_id": "client-1",
            "temp_path": "/tmp/too-big.pdf",
            "filename": "too-big.pdf",
        })

        assert result["ok"] is False
        assert result["reason"] == "file_too_large"
        assert "> 50MB" in result["error"]

    @patch("backend.upload_tasks.os.remove")
    @patch("backend.upload_tasks.os.path.exists")
    @patch("backend.upload_tasks.UploadedFiles")
    @patch("backend.upload_tasks.ChatThread.resolve_id")
    def test_worker_still_enforces_file_count_limit(
        self,
        mock_resolve_id,
        mock_uploaded_files,
        mock_exists,
        mock_remove,
    ):
        from backend import upload_tasks

        mock_exists.return_value = True
        mock_resolve_id.return_value = 123
        mock_uploaded_files.query.filter_by.return_value.count.return_value = 100

        result = upload_tasks.case_assistant_upload_file_task.run({
            "draft_id": "draft-public-id",
            "client_upload_id": "client-1",
            "temp_path": "/tmp/extra.pdf",
            "filename": "extra.pdf",
        })

        assert result["ok"] is False
        assert result["reason"] == "file_count_limit"
        assert "Maximum 100 files allowed per chat." == result["error"]

    @pytest.mark.parametrize('used_count,used_bytes,expected_failure', [
        (99, 0, None), (100, 0, 'file_count_limit'), (99, 1024**3, 'total_size_limit'),
    ])
    @patch("backend.upload_tasks.document_metadata_task.delay")
    @patch("backend.util.workspace_store.patch_auxiliary")
    @patch("backend.upload_tasks.load_state_from_memory")
    @patch("backend.upload_tasks.DBMemory")
    @patch("backend.upload_tasks.DocumentChunk")
    @patch("backend.upload_tasks.chunk_text")
    @patch("backend.upload_tasks.default_document_metadata")
    @patch("backend.upload_tasks.get_pagenum_from_pdf_fitz")
    @patch("backend.upload_tasks.get_last_ocr_warning")
    @patch("backend.upload_tasks.get_complaint_text_from_file")
    @patch("backend.upload_tasks.os.remove")
    @patch("backend.upload_tasks.os.path.getsize")
    @patch("backend.upload_tasks.os.path.exists")
    @patch("backend.upload_tasks.UploadedFiles")
    @patch("backend.upload_tasks.ChatThread.resolve_id")
    @patch("backend.upload_tasks.db")
    def test_worker_allows_large_page_count_without_total_page_cap(
        self,
        mock_db,
        mock_resolve_id,
        mock_uploaded_files,
        mock_exists,
        mock_getsize,
        mock_remove,
        mock_parse,
        mock_ocr,
        mock_page_count,
        mock_default_metadata,
        mock_chunk_text,
        mock_document_chunk,
        mock_db_memory,
        mock_load_state,
        mock_patch_auxiliary,
        mock_metadata_delay,
        used_count,
        used_bytes,
        expected_failure,
    ):
        from backend import upload_tasks

        mock_exists.return_value = True
        mock_getsize.return_value = 1000
        mock_resolve_id.return_value = 123
        mock_uploaded_files.query.filter_by.return_value.count.return_value = 99
        mock_db.session.query.return_value.filter_by.return_value.one.return_value = (used_count, used_bytes)
        fake_uploaded_file = MagicMock(id=456)
        mock_uploaded_files.return_value = fake_uploaded_file
        mock_parse.return_value = ("parsed text", "case number", True)
        mock_ocr.return_value = False
        mock_page_count.return_value = 5000
        mock_default_metadata.return_value = {"status": "pending"}
        mock_chunk_text.return_value = [{
            "chunk_index": 0,
            "content": "parsed text",
            "token_count": 2,
            "char_count": 11,
        }]
        state = {"uploaded_files": {"existing": {"page_count": 700}}}
        mock_load_state.return_value = state
        mock_patch_auxiliary.return_value = state

        result = upload_tasks.case_assistant_upload_file_task.run({
            "draft_id": "draft-public-id",
            "client_upload_id": "client-1",
            "unique_id": "accepted-document-id",
            "temp_path": "/tmp/large.pdf",
            "filename": "large.pdf",
        })

        if expected_failure:
            assert result['ok'] is False
            assert result['reason'] == expected_failure
            mock_db.session.add.assert_not_called()
            mock_patch_auxiliary.assert_not_called()
            mock_db.session.rollback.assert_called_once()
            return
        assert result["ok"] is True
        assert result["page_count"] == 5000
        assert result["total_page_count"] == 5700
        assert result["chunk_count"] == 1
        assert result["unique_id"] == 'accepted-document-id'
        assert state["total_page_count"] == 5700
        assert state["uploaded_files"][result["unique_id"]]["page_count"] == 5000
        assert mock_patch_auxiliary.call_args_list == [
            call(123, {'uploaded_files': {'accepted-document-id': state['uploaded_files']['accepted-document-id']}},
                 merge=('uploaded_files',)),
            call(123, {'total_page_count': 5700}),
        ]
        mock_metadata_delay.assert_called_once_with(456)


class TestCaseAssistantUploadRoute:
    def test_compatibility_endpoint_always_uses_durable_service(self, monkeypatch):
        from backend import views, document_preparation_routes
        durable = MagicMock(return_value=({'documents': [], 'uploads': []}, 202))
        monkeypatch.setattr(document_preparation_routes, 'legacy_upload', durable)
        monkeypatch.setattr(views.case_assistant_upload_file_task, 'delay', MagicMock())
        body, status = views.upload_files_everything_async.__wrapped__()
        assert status == 202
        durable.assert_called_once_with('chat')
        views.case_assistant_upload_file_task.delay.assert_not_called()

    @pytest.mark.parametrize('handler', ['upload_file_matter', 'upload_folder_matter'])
    def test_matter_compatibility_endpoints_use_same_durable_service(self, monkeypatch, handler):
        from backend import views, document_preparation_routes
        durable = MagicMock(return_value=({'documents': []}, 202))
        monkeypatch.setattr(document_preparation_routes, 'legacy_upload', durable)
        body, status = getattr(views, handler).__wrapped__()
        assert status == 202
        durable.assert_called_once_with('matter')


class TestSmartSearchUploadRoute:
    def _install_route_context(self, monkeypatch, *, files, form_values=None):
        from backend import views

        request_mock = MagicMock()
        values = {"projectId": "7"}
        if form_values:
            values.update(form_values)
        request_mock.form.get.side_effect = lambda key, default=None: values.get(key, default)
        request_mock.files.getlist.return_value = files

        monkeypatch.setattr(views, "request", request_mock)
        monkeypatch.setattr(views, "current_user", SimpleNamespace(id=9))
        monkeypatch.setattr(views, "project_ownership_check", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(views, "_cancel_redis", MagicMock())
        return views

    def test_loose_route_returns_one_task_per_staged_file_and_immediate_skips(self, monkeypatch):
        supported_a = MagicMock()
        supported_a.filename = "a.pdf"
        supported_a.tell.side_effect = AssertionError("Smart Search backend should not check file size")
        supported_b = MagicMock()
        supported_b.filename = "b.msg"
        unsupported = MagicMock()
        unsupported.filename = "notes.xlsx"
        lock_file = MagicMock()
        lock_file.filename = "~$draft.docx"

        views = self._install_route_context(
            monkeypatch,
            files=[supported_a, unsupported, supported_b, lock_file],
        )

        queued_payloads = []

        def fake_delay(payload):
            queued_payloads.append(payload)
            return SimpleNamespace(id=f"task-{len(queued_payloads)}")

        monkeypatch.setattr(views.smart_search_upload_file_task, "delay", fake_delay)

        body, status = views.smart_search_upload_file.__wrapped__()

        assert status == 202
        assert body["uploads"] == [
            {"filename": "a.pdf", "relative_path": None, "task_id": "task-1"},
            {"filename": "b.msg", "relative_path": None, "task_id": "task-2"},
        ]
        assert body["skipped_files"] == ["notes.xlsx", "~$draft.docx"]
        assert body["failed_files"] == []
        assert [payload["file"]["filename"] for payload in queued_payloads] == ["a.pdf", "b.msg"]
        assert all(payload["project_id"] == 7 for payload in queued_payloads)
        assert all(payload["folder_id"] is None for payload in queued_payloads)
        supported_a.save.assert_called_once()
        supported_b.save.assert_called_once()
        unsupported.save.assert_not_called()
        lock_file.save.assert_not_called()
        assert views._cancel_redis.set.call_count == 2

    def test_folder_route_reuses_folder_and_returns_one_task_per_staged_file(self, monkeypatch):
        file_a = MagicMock()
        file_a.filename = "a.pdf"
        file_b = MagicMock()
        file_b.filename = "b.txt"
        relative_paths = ["Root/a.pdf", "Root/Sub/b.txt"]
        folder = SimpleNamespace(id=77, folder_name="Root", file_count=0, total_chunks=0)

        views = self._install_route_context(
            monkeypatch,
            files=[file_a, file_b],
            form_values={
                "folderId": "77",
                "relativePaths": json.dumps(relative_paths),
            },
        )
        folder_upload_mock = MagicMock()
        folder_upload_mock.query.filter_by.return_value.first.return_value = folder
        monkeypatch.setattr(views, "FolderUpload", folder_upload_mock)

        queued_payloads = []

        def fake_delay(payload):
            queued_payloads.append(payload)
            return SimpleNamespace(id=f"task-{len(queued_payloads)}")

        monkeypatch.setattr(views.smart_search_upload_file_task, "delay", fake_delay)

        body, status = views.smart_search_upload_folder.__wrapped__()

        assert status == 202
        assert body["folder_id"] == 77
        assert body["uploads"] == [
            {"filename": "a.pdf", "relative_path": "Root/a.pdf", "task_id": "task-1"},
            {"filename": "b.txt", "relative_path": "Root/Sub/b.txt", "task_id": "task-2"},
        ]
        assert [payload["folder_id"] for payload in queued_payloads] == [77, 77]
        assert [payload["file"]["relative_path"] for payload in queued_payloads] == relative_paths
        assert views._cancel_redis.set.call_count == 2

    def test_folder_route_returns_empty_uploads_when_only_unsupported_files(self, monkeypatch):
        unsupported = MagicMock()
        unsupported.filename = "sheet.xlsx"
        folder = SimpleNamespace(id=77, folder_name="Root", file_count=3, total_chunks=9)

        views = self._install_route_context(
            monkeypatch,
            files=[unsupported],
            form_values={
                "folderId": "77",
                "relativePaths": json.dumps(["Root/sheet.xlsx"]),
            },
        )
        folder_upload_mock = MagicMock()
        folder_upload_mock.query.filter_by.return_value.first.return_value = folder
        monkeypatch.setattr(views, "FolderUpload", folder_upload_mock)
        monkeypatch.setattr(views.smart_search_upload_file_task, "delay", MagicMock())

        body, status = views.smart_search_upload_folder.__wrapped__()

        assert status == 200
        assert body["uploads"] == []
        assert body["files"] == []
        assert body["skipped_files"] == ["sheet.xlsx"]
        assert body["file_count"] == 3
        assert body["total_chunks"] == 9
        unsupported.save.assert_not_called()
        views.smart_search_upload_file_task.delay.assert_not_called()
