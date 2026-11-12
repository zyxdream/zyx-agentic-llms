from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask_sqlalchemy")


@pytest.fixture(autouse=True)
def enabled_account(monkeypatch):
    # Transport tests have no database; real account status is covered separately.
    from backend import caseviewer_tasks
    guard = MagicMock()
    monkeypatch.setattr(caseviewer_tasks, "require_enabled_account", guard)
    return guard


def _valid_cluster(*, sub_opinions=True):
    return {
        "case_name": "Valid Case",
        "absolute_url": "/opinion/123/valid-case/",
        "docket": "https://www.courtlistener.com/api/rest/v4/dockets/44/",
        "sub_opinions": (
            ["https://www.courtlistener.com/api/rest/v4/opinions/456/"]
            if sub_opinions else []
        ),
    }


def _valid_caseviewer_response():
    opinion = {"html_with_citations": "<p>Opinion text</p>"}
    return {
        "cluster": _valid_cluster(),
        "llm_holding": "The holding controls.",
        "opinion": opinion,
        "opinions": [opinion],
        "llm_hist": "Generated overview.",
        "caseviewer_synopsis_version": 1,
        "casetext": None,
        "freelaw_link": "https://www.courtlistener.com/opinion/123/valid-case/",
    }


class FakeTask:
    request = SimpleNamespace(id="case-task-1")

    def __init__(self):
        self.progress_updates = []

    def update_state(self, *, state=None, meta=None):
        self.progress_updates.append((state, meta))


class FakeRedis:
    def __init__(self):
        self.delete = MagicMock()


def _payload(**overrides):
    payload = {
        "cluster_id": "123",
        "citation_name_decoded": "123 F.2d 456",
        "draft_id": "draft-public-id",
        "user_id": 9,
        "active_task_key": "caseviewer:active:9:draft-public-id:123",
    }
    payload.update(overrides)
    return payload


def test_caseviewer_task_cache_hit_returns_cached_payload_and_skips_live(monkeypatch):
    from backend import caseviewer_tasks

    cached_response = _valid_caseviewer_response()
    fake_task = FakeTask()
    fake_redis = FakeRedis()
    monkeypatch.setattr(caseviewer_tasks, "_caseviewer_redis", fake_redis)
    monkeypatch.setattr(
        caseviewer_tasks,
        "load_cached_caseviewer_response",
        MagicMock(return_value=(cached_response, {"reason": "valid"}, SimpleNamespace(cluster_id=123))),
    )
    monkeypatch.setattr(caseviewer_tasks, "build_live_caseviewer_payload", MagicMock())
    monkeypatch.setattr(caseviewer_tasks, "save_caseviewer_global_cache", MagicMock())
    monkeypatch.setattr(caseviewer_tasks, "save_caseviewer_validated_case", MagicMock())

    result = caseviewer_tasks.run_caseviewer_enrichment(_payload(), task=fake_task)

    assert result == cached_response
    caseviewer_tasks.build_live_caseviewer_payload.assert_not_called()
    caseviewer_tasks.save_caseviewer_global_cache.assert_not_called()
    caseviewer_tasks.save_caseviewer_validated_case.assert_not_called()
    fake_redis.delete.assert_called_once_with("caseviewer:active:9:draft-public-id:123")
    assert ("PROGRESS", {"message": "Checking saved case details...", "status": "Checking saved case details...", "progress": 5}) in fake_task.progress_updates
    assert ("PROGRESS", {"message": "Case details ready.", "status": "Case details ready.", "progress": 100}) in fake_task.progress_updates


def test_caseviewer_task_live_success_saves_global_and_per_draft_cache(monkeypatch):
    from backend import caseviewer_service, caseviewer_tasks

    fake_task = FakeTask()
    fake_redis = FakeRedis()
    global_save = MagicMock(return_value=True)
    draft_save = MagicMock(return_value=True)
    monkeypatch.setattr(caseviewer_tasks, "_caseviewer_redis", fake_redis)
    monkeypatch.setattr(
        caseviewer_tasks,
        "load_cached_caseviewer_response",
        MagicMock(return_value=(None, {"reason": "cache_miss"}, None)),
    )
    monkeypatch.setattr(caseviewer_tasks, "save_caseviewer_global_cache", global_save)
    monkeypatch.setattr(caseviewer_tasks, "save_caseviewer_validated_case", draft_save)
    monkeypatch.setattr(caseviewer_service, "get_cluster", MagicMock(return_value=_valid_cluster()))
    monkeypatch.setattr(
        caseviewer_service,
        "get_docket",
        MagicMock(return_value={"court": "https://www.courtlistener.com/api/rest/v4/courts/ca9/"}),
    )
    monkeypatch.setattr(caseviewer_service, "get_court", MagicMock(return_value={"citation_string": "9th Cir."}))
    monkeypatch.setattr(
        caseviewer_service,
        "get_cluster_opinions",
        MagicMock(return_value=[{"html_with_citations": "<p>Opinion text</p>"}]),
    )
    synopsis_call = MagicMock(return_value=SimpleNamespace(
        holding="The holding controls.",
        case_overview="The case overview is grounded in the opinion.",
    ))
    monkeypatch.setattr(caseviewer_service, "extract_holding_and_case_overview_from_opinion", synopsis_call)
    history_call = MagicMock(side_effect=AssertionError("case history should not be generated"))
    monkeypatch.setattr(caseviewer_service, "get_llm_case_hist", history_call, raising=False)

    result = caseviewer_tasks.run_caseviewer_enrichment(_payload(), task=fake_task)

    assert result["cluster"]["case_name"] == "Valid Case"
    assert result["cluster"]["court_citation_string"] == "9th Cir."
    assert result["llm_holding"] == "The holding controls."
    assert result["llm_hist"] == "The case overview is grounded in the opinion."
    assert result["caseviewer_synopsis_version"] == 1
    caseviewer_service.get_cluster.assert_called_once_with("123")
    caseviewer_service.get_cluster_opinions.assert_called_once()
    synopsis_call.assert_called_once_with({"html_with_citations": "<p>Opinion text</p>"})
    history_call.assert_not_called()
    global_save.assert_called_once()
    draft_save.assert_called_once()
    assert global_save.call_args.kwargs["response"]["llm_hist"] == "The case overview is grounded in the opinion."
    assert draft_save.call_args.kwargs["response"]["llm_hist"] == "The case overview is grounded in the opinion."
    assert draft_save.call_args.kwargs["draft_id"] == "draft-public-id"
    fake_redis.delete.assert_called_once_with("caseviewer:active:9:draft-public-id:123")
    messages = [meta["message"] for state, meta in fake_task.progress_updates if state == "PROGRESS"]
    assert "Fetching case cluster..." in messages
    assert "Fetching opinions..." in messages
    assert "Extracting holding and case overview..." in messages
    assert "Generating case history..." not in messages
    assert "Saving case details..." in messages
    assert "Case details ready." in messages


@pytest.mark.parametrize("has_opinion", [False, True])
def test_caseviewer_live_missing_opinion_or_synopsis_keeps_empty_fields(monkeypatch, has_opinion):
    from backend import caseviewer_service

    opinion = {"html_with_citations": "<p>Opinion text</p>"}
    synopsis_call = MagicMock(return_value=None)
    monkeypatch.setattr(
        caseviewer_service,
        "get_cluster",
        MagicMock(return_value=_valid_cluster(sub_opinions=has_opinion)),
    )
    monkeypatch.setattr(caseviewer_service, "get_docket", MagicMock(return_value=None))
    monkeypatch.setattr(
        caseviewer_service,
        "get_cluster_opinions",
        MagicMock(return_value=[opinion]),
    )
    monkeypatch.setattr(
        caseviewer_service,
        "extract_holding_and_case_overview_from_opinion",
        synopsis_call,
    )

    response, context = caseviewer_service.build_live_caseviewer_payload(
        citation_name_decoded="123 F.2d 456",
        cluster_id="123",
    )

    assert response["llm_holding"] == ""
    assert response["llm_hist"] == ""
    assert response["caseviewer_synopsis_version"] == 1
    if has_opinion:
        assert context["synopsis_failed"] is True
        synopsis_call.assert_called_once_with(opinion)
    else:
        assert context["synopsis_failed"] is False
        synopsis_call.assert_not_called()
        caseviewer_service.get_cluster_opinions.assert_not_called()


def test_caseviewer_does_not_cache_transient_synopsis_failure(monkeypatch):
    from backend import caseviewer_service

    upsert = MagicMock()
    monkeypatch.setattr(caseviewer_service, "upsert_freelaw_cluster_cache", upsert)

    saved = caseviewer_service.save_caseviewer_global_cache(
        cluster_id="123",
        citation_name_decoded="123 F.2d 456",
        response={
            "cluster": _valid_cluster(),
            "llm_holding": "",
            "llm_hist": "",
            "caseviewer_synopsis_version": 1,
        },
        context={"synopsis_failed": True},
    )

    assert saved is False
    upsert.assert_not_called()


def test_caseviewer_task_no_cluster_returns_no_details_and_writes_no_cache(monkeypatch):
    from backend import caseviewer_service, caseviewer_tasks

    fake_task = FakeTask()
    fake_redis = FakeRedis()
    monkeypatch.setattr(caseviewer_tasks, "_caseviewer_redis", fake_redis)
    monkeypatch.setattr(
        caseviewer_tasks,
        "load_cached_caseviewer_response",
        MagicMock(return_value=(None, {"reason": "cache_miss"}, None)),
    )
    monkeypatch.setattr(caseviewer_tasks, "save_caseviewer_global_cache", MagicMock())
    monkeypatch.setattr(caseviewer_tasks, "save_caseviewer_validated_case", MagicMock())
    monkeypatch.setattr(caseviewer_service, "get_cluster", MagicMock(return_value=None))

    result = caseviewer_tasks.run_caseviewer_enrichment(_payload(), task=fake_task)

    assert result["error"] is True
    assert result["not_found"] is True
    assert result["cluster"] is None
    assert result["opinions"] == []
    caseviewer_tasks.save_caseviewer_global_cache.assert_not_called()
    caseviewer_tasks.save_caseviewer_validated_case.assert_not_called()
    fake_redis.delete.assert_called_once_with("caseviewer:active:9:draft-public-id:123")


def test_caseviewer_task_live_exception_returns_error_and_writes_no_cache(monkeypatch):
    from backend import caseviewer_service, caseviewer_tasks

    fake_task = FakeTask()
    fake_redis = FakeRedis()
    monkeypatch.setattr(caseviewer_tasks, "_caseviewer_redis", fake_redis)
    monkeypatch.setattr(
        caseviewer_tasks,
        "load_cached_caseviewer_response",
        MagicMock(return_value=(None, {"reason": "cache_miss"}, None)),
    )
    monkeypatch.setattr(caseviewer_tasks, "save_caseviewer_global_cache", MagicMock())
    monkeypatch.setattr(caseviewer_tasks, "save_caseviewer_validated_case", MagicMock())
    monkeypatch.setattr(caseviewer_service, "get_cluster", MagicMock(side_effect=RuntimeError("CourtListener failed")))

    result = caseviewer_tasks.run_caseviewer_enrichment(_payload(), task=fake_task)

    assert result["error"] is True
    assert result["not_found"] is True
    assert result["cluster"] is None
    assert result["error_message"] == "Unable to load case details. Please try again."
    caseviewer_tasks.save_caseviewer_global_cache.assert_not_called()
    caseviewer_tasks.save_caseviewer_validated_case.assert_not_called()
    fake_redis.delete.assert_called_once_with("caseviewer:active:9:draft-public-id:123")
