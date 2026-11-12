from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask, Response, jsonify, request

from tests.e2e.helpers import (
    ArtifactCollector,
    E2EClient,
    E2ETaskFailure,
    E2ETaskTimeout,
)


def test_authenticated_flask_requests_and_project_draft_creation() -> None:
    app = Flask(__name__)
    projects: list[list[object]] = []

    @app.post("/create-project")
    def create_project():
        assert request.headers["Authorization"] == "Bearer test-token"
        name = request.json["proj-name"]
        projects.append([name, 17])
        return jsonify(msg="Command processed successfully!")

    @app.post("/list-projects")
    def list_projects():
        return jsonify(projects=projects)

    @app.post("/create-draft")
    def create_draft():
        assert request.headers["Authorization"] == "Bearer test-token"
        return jsonify(draftId="draft-public-id", draftName=request.json["draft-name"])

    client = E2EClient(flask_client=app.test_client(), access_token="test-token")

    project = client.create_project("E2E Complaint")
    draft = client.create_draft(project.public_id, "Complaint", 1)

    assert project.public_id == 17
    assert draft.public_id == "draft-public-id"
    assert draft.project_id == 17


def test_poll_task_preserves_progress_and_requires_success() -> None:
    app = Flask(__name__)
    responses = iter(
        [
            {"ready": False, "successful": None, "state": "PENDING", "info": "queued"},
            {"ready": False, "successful": None, "state": "PROGRESS", "info": {"step": 2}},
            {"ready": True, "successful": True, "state": "SUCCESS", "value": {"draftId": "1"}},
        ]
    )

    @app.get("/result/task-1")
    def result():
        return jsonify(next(responses))

    client = E2EClient(flask_client=app.test_client(), access_token="token")
    result = client.poll_task("task-1", interval=0, sleep_fn=lambda _: None)

    assert result.require_success() == {"draftId": "1"}
    assert [item["state"] for item in result.history] == ["PENDING", "PROGRESS", "SUCCESS"]


def test_failed_task_exposes_diagnostic_and_can_be_collected(tmp_path: Path) -> None:
    app = Flask(__name__)

    @app.get("/result/failed-task")
    def failed_result():
        return jsonify(
            ready=True,
            successful=False,
            state="FAILURE",
            info="worker raised an exception",
            value={"error_code": "EXPORT_FAILED", "error_message": "PDF export failed"},
        )

    client = E2EClient(flask_client=app.test_client(), access_token="token")
    result = client.poll_task("failed-task")

    with pytest.raises(E2ETaskFailure, match="PDF export failed") as exc_info:
        result.require_success()

    collector = ArtifactCollector(tmp_path)
    diagnostic_path = collector.collect_task_failure(exc_info.value.diagnostic)
    assert diagnostic_path.read_text(encoding="utf-8").find("EXPORT_FAILED") >= 0


def test_collect_draft_pdf_and_word_artifacts(tmp_path: Path) -> None:
    app = Flask(__name__)

    @app.get("/draft-pdf/draft-1")
    def draft_pdf():
        return Response(b"%PDF-test", mimetype="application/pdf")

    @app.get("/download-word-complaint/draft-1")
    def draft_word():
        return Response(
            b"PK-word-test",
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    client = E2EClient(flask_client=app.test_client(), access_token="token")
    artifacts = ArtifactCollector(tmp_path).collect_draft_artifacts(client, "draft-1")

    assert artifacts["pdf"].read_bytes() == b"%PDF-test"
    assert artifacts["word"].read_bytes() == b"PK-word-test"


def test_poll_timeout_contains_last_payload() -> None:
    app = Flask(__name__)

    @app.get("/result/slow-task")
    def slow_result():
        return jsonify(ready=False, successful=None, state="PENDING", info="still queued")

    client = E2EClient(flask_client=app.test_client(), access_token="token")
    with pytest.raises(E2ETaskTimeout) as exc_info:
        client.poll_task("slow-task", timeout=0)

    assert exc_info.value.last_payload["state"] == "PENDING"
