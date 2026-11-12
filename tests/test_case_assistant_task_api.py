"""API compatibility for single-task Case Assistant runs and legacy chains."""

import json
from types import SimpleNamespace

import pytest
from billiard.exceptions import TimeLimitExceeded
from celery.exceptions import SoftTimeLimitExceeded
from flask import Flask

from backend import everything, views


def _metadata(task_id="turn-1"):
    return json.dumps({
        "task_type": "everything",
        "draft_id": "public-draft",
        "user_id": 17,
    }).encode()


def _install_state(monkeypatch, *, active_id="turn-1"):
    state = {
        "active_task_id": active_id,
        "active_workflow": {"status": "running", "phase": "draft"},
    }
    saved = []
    released = []
    monkeypatch.setattr(views.ChatThread, "resolve_id", lambda _draft_id: 42)
    monkeypatch.setattr(views, "DBMemory", lambda **_kwargs: object())
    monkeypatch.setattr(views, "load_state_from_memory", lambda _memory: state)
    monkeypatch.setattr(views, "save_state_to_memory", lambda _state, _memory, **kwargs: saved.append(dict(_state)))
    monkeypatch.setattr(views, "release_draft_lock", lambda doc_id, user_id: released.append((doc_id, user_id)))
    return state, saved, released


@pytest.mark.parametrize("active_id,expect_cleanup", [
    ("turn-1", True),
    ("newer-turn", False),
])
def test_result_reconciles_only_current_hard_timed_out_run(monkeypatch, active_id, expect_cleanup):
    failure = TimeLimitExceeded(everything.CASE_ASSISTANT_HARD_TIME_LIMIT)
    task_result = SimpleNamespace(
        result=failure,
        state="FAILURE",
        info=failure,
        failed=lambda: True,
        ready=lambda: True,
        successful=lambda: False,
    )
    monkeypatch.setattr(views, "AsyncResult", lambda _task_id: task_result)
    monkeypatch.setattr(
        views,
        "_cancel_redis",
        SimpleNamespace(get=lambda key: _metadata() if key == "task_meta:turn-1" else None),
    )
    state, saved, released = _install_state(monkeypatch, active_id=active_id)

    app = Flask(__name__)
    with app.test_request_context():
        response = views.result.__wrapped__("turn-1")

    assert response["ready"] is True
    assert response["successful"] is False
    assert response["value"]["error_code"] == "task_timeout"
    assert response["value"]["retryable"] is True
    if expect_cleanup:
        assert "active_task_id" not in state
        assert state["active_workflow"]["status"] == "failed"
        assert saved and released == [(42, 17)]
    else:
        assert state["active_task_id"] == "newer-turn"
        assert saved == [] and released == []


@pytest.mark.parametrize("legacy_parent", [None, "phase-1"])
def test_cancel_clears_single_task_or_legacy_parent(monkeypatch, legacy_parent):
    task_id = "phase-2" if legacy_parent else "turn-1"
    active_id = legacy_parent or task_id
    state, saved, released = _install_state(monkeypatch, active_id=active_id)
    revoked = []
    deleted = []

    def get(key):
        if key == f"task_meta:{task_id}":
            return _metadata(task_id)
        if key == f"task_chain:{task_id}" and legacy_parent:
            return legacy_parent.encode()
        return None

    monkeypatch.setattr(views, "current_user", SimpleNamespace(id=17))
    monkeypatch.setattr(
        views,
        "_cancel_redis",
        SimpleNamespace(get=get, delete=lambda *keys: deleted.extend(keys)),
    )
    app = Flask(__name__)
    app.extensions["celery"] = SimpleNamespace(
        control=SimpleNamespace(revoke=lambda task, **_kwargs: revoked.append(task))
    )

    with app.test_request_context():
        response, status = views.cancel_task.__wrapped__(task_id)

    assert (response, status) == ({"status": "cancelled"}, 200)
    assert revoked == ([task_id, legacy_parent] if legacy_parent else [task_id])
    assert "active_task_id" not in state
    assert state["active_workflow"]["status"] == "cancelled"
    assert saved and released == [(42, 17)]
    assert f"task_meta:{task_id}" in deleted


def test_cancel_old_task_preserves_newer_run_state_and_lock(monkeypatch):
    state, saved, released = _install_state(monkeypatch, active_id="newer-turn")
    revoked = []
    monkeypatch.setattr(views, "current_user", SimpleNamespace(id=17))
    monkeypatch.setattr(
        views,
        "_cancel_redis",
        SimpleNamespace(
            get=lambda key: _metadata() if key == "task_meta:turn-1" else None,
            delete=lambda *_keys: None,
        ),
    )
    app = Flask(__name__)
    app.extensions["celery"] = SimpleNamespace(
        control=SimpleNamespace(revoke=lambda task, **_kwargs: revoked.append(task))
    )

    with app.test_request_context():
        response, status = views.cancel_task.__wrapped__("turn-1")

    assert (response, status) == ({"status": "cancelled"}, 200)
    assert revoked == ["turn-1"]
    assert state["active_task_id"] == "newer-turn"
    assert saved == [] and released == []


@pytest.mark.parametrize("failure", [TimeLimitExceeded(300), SoftTimeLimitExceeded()])
def test_result_preserves_legacy_phase_one_timeout_message(monkeypatch, failure):
    phase_one = SimpleNamespace(result=failure, state="FAILURE", failed=lambda: True)
    phase_two = SimpleNamespace(result=None, state="PENDING")
    monkeypatch.setattr(
        views,
        "AsyncResult",
        lambda task_id: phase_one if task_id == "phase-1" else phase_two,
    )
    monkeypatch.setattr(
        views,
        "_cancel_redis",
        SimpleNamespace(
            get=lambda key: (
                b"phase-1" if key == "task_chain:phase-2"
                else json.dumps({"draft_id": "public-draft", "user_id": 17}).encode()
                if key == "task_meta:phase-2" else None
            )
        ),
    )
    state, saved, released = _install_state(monkeypatch, active_id="phase-1")

    app = Flask(__name__)
    with app.test_request_context():
        response = views.result.__wrapped__("phase-2")

    assert response["value"]["error_code"] == "task_timeout"
    assert response["value"]["error_message"] == views.TASK_TIMEOUT_RETRY_MESSAGE_PHASE1
    assert "active_task_id" not in state
    assert state["active_workflow"]["status"] == "failed"
    assert saved and released == [(42, 17)]


def test_result_cleans_up_legacy_phase_two_hard_timeout(monkeypatch):
    failure = TimeLimitExceeded(300)
    phase_one = SimpleNamespace(failed=lambda: False, ready=lambda: True)
    phase_two = SimpleNamespace(
        result=failure,
        state="FAILURE",
        info=failure,
        failed=lambda: True,
        ready=lambda: True,
        successful=lambda: False,
    )
    monkeypatch.setattr(
        views,
        "AsyncResult",
        lambda task_id: phase_one if task_id == "phase-1" else phase_two,
    )
    monkeypatch.setattr(
        views,
        "_cancel_redis",
        SimpleNamespace(
            get=lambda key: (
                b"phase-1" if key == "task_chain:phase-2"
                else json.dumps({"draft_id": "public-draft", "user_id": 17}).encode()
                if key == "task_meta:phase-2" else None
            )
        ),
    )
    state, saved, released = _install_state(monkeypatch, active_id="phase-1")

    app = Flask(__name__)
    with app.test_request_context():
        response = views.result.__wrapped__("phase-2")

    assert response["value"]["error_code"] == "task_timeout"
    assert response["value"]["error_message"] == views.TASK_TIMEOUT_RETRY_MESSAGE_PHASE2
    assert "active_task_id" not in state
    assert saved and released == [(42, 17)]
