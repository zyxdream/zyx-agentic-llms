"""HTTP regression coverage for large Answer Drafter Step 1 submissions."""

from types import SimpleNamespace

from flask import Flask

from backend import answer_drafter, views


def test_large_step1_json_request_enqueues_without_multipart_limit(monkeypatch):
    app = Flask(__name__)
    # Exercise the real route body while bypassing auth and the database only.
    app.add_url_rule(
        "/api/answerdrafter",
        view_func=views.handle_answer_draft.__wrapped__,
        methods=["POST"],
    )
    monkeypatch.setattr(views, "current_user", SimpleNamespace(id=7))

    checked = []

    def check_ownership(d, jwt_id):
        checked.append((d, jwt_id))
        return None

    monkeypatch.setattr(views, "ownership_check", check_ownership)

    queued = []

    def enqueue(data):
        queued.append(data)
        return SimpleNamespace(id="answer-step1-task")

    # No Celery worker or LLM is called: only the request dispatch is exercised.
    monkeypatch.setattr(answer_drafter.state1, "delay", enqueue)

    paragraph_selections = [
        {"outline_index": number, "paragraph_number": number, "choice": "deny"}
        for number in range(1, 1202)
    ]
    payload = {
        "appState": 1,
        "draftId": "large-answer-draft",
        "defendants": ["Example Defendant"],
        "default_choice": "deny",
        "paragraph_selections": paragraph_selections,
    }

    response = app.test_client().post("/api/answerdrafter", json=payload)

    assert response.status_code == 200
    assert response.get_json() == {"result_id": "answer-step1-task"}
    assert checked == [(payload, 7)]
    assert len(queued) == 1
    assert queued[0] == payload
    assert len(queued[0]["paragraph_selections"]) == 1201
