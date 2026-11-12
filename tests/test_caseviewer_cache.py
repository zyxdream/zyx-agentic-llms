from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask_sqlalchemy")


def _cached_caseviewer_response():
    opinion = {
        "html_with_citations": "<p>Cached opinion text</p>",
        "author": "Justice Cache",
        "date_filed": "2007-05-21",
    }
    return {
        "cluster": {
            "case_name": "Cached Case",
            "absolute_url": "/opinion/123/cached-case/",
            "date_filed": "2007-05-21",
            "citations": [{"volume": "123", "reporter": "F.2d", "page": "456"}],
            "court_citation_string": "9th Cir.",
        },
        "opinion": opinion,
        "opinions": [opinion],
        "llm_holding": "Cached holding.",
        "llm_hist": "Cached history.",
        "caseviewer_synopsis_version": 1,
        "casetext": None,
        "freelaw_link": "https://www.courtlistener.com/opinion/123/cached-case/",
    }


class FakeRedis:
    def __init__(self, get_value=None):
        self.get = MagicMock(return_value=get_value)
        self.set = MagicMock()
        self.delete = MagicMock()


def _install_caseviewer_route_mocks(
    monkeypatch,
    *,
    request_args=None,
    cached_response=None,
    cache_details=None,
    cache_row=None,
    redis_get_value=None,
):
    from backend import views

    cache_details = cache_details or {
        "reason": "cache_miss",
        "has_caseviewer_response": False,
        "has_holding": False,
        "has_hist": False,
    }
    task = SimpleNamespace(apply_async=MagicMock(return_value=SimpleNamespace(id="case-task-1")))
    fake_redis = FakeRedis(redis_get_value)
    calls = {
        "load_cached_caseviewer_response": MagicMock(return_value=(cached_response, cache_details, cache_row)),
        "task": task,
        "redis": fake_redis,
    }

    monkeypatch.setattr(views, "request", SimpleNamespace(args=request_args or {}))
    monkeypatch.setattr(views, "current_user", SimpleNamespace(id=9))
    monkeypatch.setattr(views, "decode_citation_url", lambda _value: "123 F.2d 456")
    monkeypatch.setattr(views, "load_cached_caseviewer_response", calls["load_cached_caseviewer_response"])
    monkeypatch.setattr(views, "caseviewer_enrichment_task", task)
    monkeypatch.setattr(views, "_cancel_redis", fake_redis)
    return views, calls


def _call_caseviewer(views, cluster_id="123"):
    return views.get_clusteropinion_data.__wrapped__("123-F_dot_2d-456", cluster_id)


def test_caseviewer_full_cache_hit_returns_cached_response_and_skips_enqueue(monkeypatch):
    cached_response = _cached_caseviewer_response()
    views, calls = _install_caseviewer_route_mocks(
        monkeypatch,
        cached_response=cached_response,
        cache_details={
            "reason": "valid",
            "has_caseviewer_response": True,
            "has_holding": True,
            "has_hist": True,
        },
        cache_row=SimpleNamespace(cluster_id=123),
    )

    body, status = _call_caseviewer(views)

    assert status == 200
    assert body == cached_response
    calls["load_cached_caseviewer_response"].assert_called_once_with("123")
    calls["task"].apply_async.assert_not_called()
    calls["redis"].get.assert_not_called()
    calls["redis"].set.assert_not_called()


@pytest.mark.parametrize(
    ("cache_row", "reason", "cache_status"),
    [
        (None, "cache_miss", "miss"),
        (SimpleNamespace(cluster_id=123), "missing_caseviewer_response", "fallback"),
        (SimpleNamespace(cluster_id=123), "invalid_payload", "fallback"),
        (SimpleNamespace(cluster_id=123), "missing_case_name", "fallback"),
        (SimpleNamespace(cluster_id=123), "missing_display_fields", "fallback"),
        (SimpleNamespace(cluster_id=123), "stale_synopsis", "fallback"),
    ],
)
def test_caseviewer_cache_miss_or_incomplete_cache_enqueues_task(
    monkeypatch,
    cache_row,
    reason,
    cache_status,
):
    views, calls = _install_caseviewer_route_mocks(
        monkeypatch,
        cache_details={
            "reason": reason,
            "has_caseviewer_response": cache_row is not None,
            "has_holding": False,
            "has_hist": False,
        },
        cache_row=cache_row,
    )

    body, status = _call_caseviewer(views)

    assert status == 202
    assert body == {
        "async": True,
        "result_id": "case-task-1",
        "message": "Case details are being prepared...",
        "progress": 5,
        "cache_status": cache_status,
        "cache_reason": reason,
    }
    calls["load_cached_caseviewer_response"].assert_called_once_with("123")
    calls["task"].apply_async.assert_called_once()
    task_payload = calls["task"].apply_async.call_args.kwargs["args"][0]
    assert task_payload["cluster_id"] == "123"
    assert task_payload["citation_name_decoded"] == "123 F.2d 456"
    assert task_payload["draft_id"] is None
    assert task_payload["user_id"] == 9
    assert task_payload["active_task_key"] == "caseviewer:active:9:no-draft:123"
    assert calls["redis"].set.call_count == 2


def test_caseviewer_active_task_is_reused(monkeypatch):
    views, calls = _install_caseviewer_route_mocks(
        monkeypatch,
        redis_get_value=b"existing-task",
    )
    monkeypatch.setattr(views, "AsyncResult", MagicMock(return_value=SimpleNamespace(ready=lambda: False)))

    body, status = _call_caseviewer(views)

    assert status == 202
    assert body["result_id"] == "existing-task"
    assert body["async"] is True
    calls["task"].apply_async.assert_not_called()
    calls["redis"].set.assert_not_called()


def test_caseviewer_completed_active_task_is_replaced(monkeypatch):
    views, calls = _install_caseviewer_route_mocks(
        monkeypatch,
        redis_get_value=b"finished-task",
    )
    monkeypatch.setattr(views, "AsyncResult", MagicMock(return_value=SimpleNamespace(ready=lambda: True)))

    body, status = _call_caseviewer(views)

    assert status == 202
    assert body["result_id"] == "case-task-1"
    calls["redis"].delete.assert_called_once_with("caseviewer:active:9:no-draft:123")
    calls["task"].apply_async.assert_called_once()


def test_caseviewer_no_cluster_id_does_not_enqueue(monkeypatch):
    views, calls = _install_caseviewer_route_mocks(monkeypatch)

    body, status = views.get_clusteropinion_data.__wrapped__("123-F_dot_2d-456")

    assert status == 200
    assert body["not_found"] is True
    assert body["cluster"] is None
    assert body["opinions"] == []
    calls["load_cached_caseviewer_response"].assert_not_called()
    calls["task"].apply_async.assert_not_called()
    calls["redis"].get.assert_not_called()


def test_caseviewer_owned_non_69_draft_can_enqueue(monkeypatch):
    views, calls = _install_caseviewer_route_mocks(
        monkeypatch,
        request_args={"draft_id": "draft-public-id"},
    )
    draft = SimpleNamespace(id=77, docCategory=5, parent=SimpleNamespace(userId=9))
    monkeypatch.setattr(views, "ChatThread", SimpleNamespace(
        resolve_id=lambda _value: 77,
        query=SimpleNamespace(get=lambda _doc_id: draft),
    ))

    body, status = _call_caseviewer(views)

    assert status == 202
    assert body["result_id"] == "case-task-1"
    task_payload = calls["task"].apply_async.call_args.kwargs["args"][0]
    assert task_payload["draft_id"] == "draft-public-id"
    assert task_payload["active_task_key"] == "caseviewer:active:9:draft-public-id:123"


def test_caseviewer_draft_not_found_fails_before_cache_or_enqueue(monkeypatch):
    views, calls = _install_caseviewer_route_mocks(
        monkeypatch,
        request_args={"draft_id": "missing-draft"},
    )
    monkeypatch.setattr(views, "ChatThread", SimpleNamespace(
        resolve_id=lambda _value: 77,
        query=SimpleNamespace(get=lambda _doc_id: None),
    ))

    body, status = _call_caseviewer(views)

    assert status == 404
    assert body == {"msg": "Draft not found"}
    calls["load_cached_caseviewer_response"].assert_not_called()
    calls["task"].apply_async.assert_not_called()


def test_caseviewer_wrong_owner_fails_before_cache_or_enqueue(monkeypatch):
    views, calls = _install_caseviewer_route_mocks(
        monkeypatch,
        request_args={"draft_id": "draft-public-id"},
    )
    draft = SimpleNamespace(id=77, docCategory=5, parent=SimpleNamespace(userId=10))
    monkeypatch.setattr(views, "ChatThread", SimpleNamespace(
        resolve_id=lambda _value: 77,
        query=SimpleNamespace(get=lambda _doc_id: draft),
    ))

    body, status = _call_caseviewer(views)

    assert status == 403
    assert body == {"msg": "Current user does not own this draft"}
    calls["load_cached_caseviewer_response"].assert_not_called()
    calls["task"].apply_async.assert_not_called()
