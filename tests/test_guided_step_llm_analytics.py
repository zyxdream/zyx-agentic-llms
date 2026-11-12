from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import importlib
import inspect
import json

from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import Flask
import httpx
import pytest
import sqlalchemy as sa

from backend.database import db
from backend.ai import base as ai_base
from backend.models import (
    Drafts,
    Project,
    StepAnalytics,
    StepLlmModelUsage,
    TaskAnalytics,
    UploadedFiles,
    User,
)
from backend.util import analytics, task_analytics
from backend import hallucination_guard, rfp_response_drafter, views


def test_guided_step_migration_adds_usage_schema_and_deletes_only_documented_aux_rows(
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.c4d5e6f7a8b9_add_guided_drafter_llm_analytics"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    step_table = sa.Table(
        "stepAnalytics",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("date", sa.DateTime),
        sa.Column("userId", sa.Integer),
        sa.Column("draftId", sa.Integer),
        sa.Column("docType", sa.String(100)),
        sa.Column("step", sa.Integer),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            step_table.insert(),
            [
                {"id": 1, "docType": "complaint", "step": 100},
                {"id": 2, "docType": "Complaint Analyzer", "step": 100},
                {"id": 3, "docType": "motion to dismiss", "step": 101},
                {"id": 4, "docType": "RFP", "step": 100},
                {"id": 5, "docType": "unrelated", "step": 100},
                {"id": 6, "docType": "complaint", "step": 0},
            ],
        )
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()

        rows = connection.execute(
            sa.text(
                'SELECT id, docType, step, status, llmCallCount, totalTokens '
                'FROM "stepAnalytics" ORDER BY id'
            )
        ).mappings().all()
        indexes = {
            index["name"]
            for index in sa.inspect(connection).get_indexes("stepLlmModelUsage")
        }
        unique_constraints = {
            constraint["name"]
            for constraint in sa.inspect(connection).get_unique_constraints(
                "stepLlmModelUsage"
            )
        }
        step_indexes = {
            index["name"]
            for index in sa.inspect(connection).get_indexes("stepAnalytics")
        }
        migration.downgrade()
        remaining_columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("stepAnalytics")
        }
        remaining_tables = set(sa.inspect(connection).get_table_names())

    assert [row["id"] for row in rows] == [5, 6]
    assert all(row["status"] == "unknown" for row in rows)
    assert all(row["llmCallCount"] == 0 and row["totalTokens"] == 0 for row in rows)
    assert "ix_step_llm_model_usage_step_id" in indexes
    assert "ix_step_llm_model_usage_model" in indexes
    assert "uq_step_llm_model_usage_step_model" in unique_constraints
    assert "ix_step_analytics_draft_doc_step" in step_indexes
    assert "status" not in remaining_columns
    assert "stepLlmModelUsage" not in remaining_tables


@pytest.fixture
def guided_analytics_app(tmp_path):
    app = Flask(__name__)
    database_path = tmp_path / "guided-analytics.sqlite"
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        user = User(email="guided@example.com")
        db.session.add(user)
        db.session.flush()
        project = Project(name="Matter", userId=user.id)
        db.session.add(project)
        db.session.flush()
        draft = Drafts(
            display_name="Complaint",
            docCategory=1,
            projectId=project.id,
            location=None,
        )
        db.session.add(draft)
        db.session.commit()
        from backend.util.workspace_store import initialize
        initialize(draft.id)
        db.session.commit()
        yield app, draft.id
        db.session.remove()
        db.drop_all()


def _response(model, tokens):
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(total_tokens=tokens),
    )


def _add_standalone_hg_upload(draft_id):
    uploaded_file = UploadedFiles(
        unique_id=f"standalone-{draft_id}",
        original_filename="brief.pdf",
        file_size=100,
        page_count=1,
        content="See Example v. Example, 123 F.3d 456.",
        draft_id=draft_id,
    )
    db.session.add(uploaded_file)
    db.session.commit()
    return uploaded_file


def _standalone_success_result():
    return {
        "case_results": [],
        "case_results_html": "",
        "statute_results": [],
        "statute_results_html": "",
        "response": "",
        "filtered_citations": [],
        "lookup_diagnostics": [],
        "prompt_token_counts": [10, 5],
    }


def test_standalone_hg_run_records_one_scoped_success_attempt(
    monkeypatch, guided_analytics_app
):
    app, draft_id = guided_analytics_app
    with app.app_context():
        _add_standalone_hg_upload(draft_id)
        monkeypatch.setattr(
            analytics, "_current_celery_task_id", lambda: "standalone-task-1"
        )

        def fake_guard(*_args, **_kwargs):
            analytics.record_guided_llm_response(_response("gpt-case", 40))
            # An individual provider failure does not fail the overall Guard when
            # the existing product logic still returns and saves a complete result.
            analytics.record_guided_llm_failure(model="gpt-statute")
            return _standalone_success_result()

        monkeypatch.setattr(
            hallucination_guard, "run_hallucination_guard_on_text", fake_guard
        )

        result = hallucination_guard._standalone_hg_impl(
            SimpleNamespace(), {"draftId": draft_id}
        )

        assert result["case_results"] == []
        rows = StepAnalytics.query.filter_by(
            draftId=draft_id,
            docType=hallucination_guard.DOC_TYPE,
            step=hallucination_guard.RUN_STEP,
        ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.status == analytics.STEP_STATUS_SUCCEEDED
        assert row.completedAt is not None
        assert row.celeryTaskId == "standalone-task-1"
        assert row.llmCallCount == 2
        assert row.llmSuccessCount == 1
        assert row.llmFailureCount == 1
        assert row.totalTokens == 40
        assert {
            (
                usage.model,
                usage.llmCallCount,
                usage.llmSuccessCount,
                usage.llmFailureCount,
                usage.totalTokens,
            )
            for usage in StepLlmModelUsage.query.filter_by(
                stepAnalyticsId=row.id
            ).all()
        } == {
            ("gpt-case", 1, 1, 0, 40),
            ("gpt-statute", 1, 0, 1, 0),
        }


def test_standalone_hg_returned_error_marks_attempt_failed(
    monkeypatch, guided_analytics_app
):
    app, draft_id = guided_analytics_app
    with app.app_context():
        _add_standalone_hg_upload(draft_id)
        monkeypatch.setattr(
            hallucination_guard,
            "run_hallucination_guard_on_text",
            lambda *_args, **_kwargs: {
                "error": True,
                "error_message": "Hallucination Guard could not analyze this document right now.",
            },
        )

        result = hallucination_guard._standalone_hg_impl(
            SimpleNamespace(), {"draftId": draft_id}
        )

        assert result["error"] is True
        row = StepAnalytics.query.filter_by(
            draftId=draft_id, docType=hallucination_guard.DOC_TYPE
        ).one()
        assert row.status == analytics.STEP_STATUS_FAILED
        assert row.completedAt is not None
        assert row.llmCallCount == 0


def test_standalone_hg_exception_marks_attempt_failed(
    monkeypatch, guided_analytics_app
):
    app, draft_id = guided_analytics_app
    with app.app_context():
        _add_standalone_hg_upload(draft_id)

        def fail_guard(*_args, **_kwargs):
            raise RuntimeError("guard failed")

        monkeypatch.setattr(
            hallucination_guard, "run_hallucination_guard_on_text", fail_guard
        )

        with pytest.raises(RuntimeError, match="guard failed"):
            hallucination_guard._standalone_hg_impl(
                SimpleNamespace(), {"draftId": draft_id}
            )

        row = StepAnalytics.query.filter_by(
            draftId=draft_id, docType=hallucination_guard.DOC_TYPE
        ).one()
        assert row.status == analytics.STEP_STATUS_FAILED
        assert row.completedAt is not None


def test_standalone_hg_analytics_failure_does_not_stop_run(
    monkeypatch, guided_analytics_app
):
    app, draft_id = guided_analytics_app
    with app.app_context():
        _add_standalone_hg_upload(draft_id)
        monkeypatch.setattr(
            analytics,
            "_independent_session",
            lambda: (_ for _ in ()).throw(RuntimeError("analytics unavailable")),
        )
        monkeypatch.setattr(
            hallucination_guard,
            "run_hallucination_guard_on_text",
            lambda *_args, **_kwargs: _standalone_success_result(),
        )

        result = hallucination_guard._standalone_hg_impl(
            SimpleNamespace(), {"draftId": draft_id}
        )

        assert result["case_results"] == []


def test_standalone_hg_primary_worker_pool_preserves_step_scope(
    monkeypatch, guided_analytics_app
):
    app, draft_id = guided_analytics_app
    from backend.ai import base as ai_base

    monkeypatch.setattr(
        hallucination_guard,
        "verify_citations_with_lookup_tags_lookup_safe",
        lambda _text: ([], []),
    )
    monkeypatch.setattr(
        hallucination_guard,
        "extract_validated_cases_from_citations",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        hallucination_guard,
        "_enrich_validated_cases_for_standalone",
        lambda _cases: None,
    )
    monkeypatch.setattr(
        hallucination_guard,
        "_build_citation_link_map",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        hallucination_guard,
        "_load_local_hallucination_prompt",
        lambda: "Cases: {validated_cases_text}\nDraft: {draft_text}",
    )
    monkeypatch.setattr(
        hallucination_guard,
        "LOCAL_STATUTES_PROMPT_PATH",
        SimpleNamespace(read_text=lambda: "Statutes: {draft_text}"),
    )
    monkeypatch.setattr(ai_base, "num_tokens_from_string", lambda _prompt: 5)

    def fake_completion(prompt, model, reasoning_effort=None):
        assert model == "gpt-6.1-sol"
        assert reasoning_effort == "high"
        tokens = 31 if prompt.startswith("Cases:") else 19
        analytics.record_guided_llm_response(_response(model, tokens))
        return '{"results": []}'

    monkeypatch.setattr(ai_base, "chat_completion_j", fake_completion)

    with app.app_context():
        step_id = analytics.start_step_analytics(
            draft_id, hallucination_guard.DOC_TYPE, hallucination_guard.RUN_STEP
        )
        with analytics.step_analytics_scope(step_id):
            result = hallucination_guard.run_hallucination_guard_on_text(
                "Document text"
            )

        assert result.get("error") is not True
        db.session.expire_all()
        row = db.session.get(StepAnalytics, step_id)
        assert row.llmCallCount == 2
        assert row.llmSuccessCount == 2
        assert row.llmFailureCount == 0
        assert row.totalTokens == 50


def test_standalone_hg_web_search_pool_preserves_success_and_failure_usage(
    monkeypatch, guided_analytics_app
):
    app, draft_id = guided_analytics_app
    rows = [
        {
            "citation": "123 F.3d 1",
            "citation_as_drafted": "First v. Case",
            "is_valid": False,
        },
        {
            "citation": "456 F.3d 2",
            "citation_as_drafted": "Second v. Case",
            "is_valid": False,
        },
    ]

    def fake_web_search(_candidates, citation_as_drafted="", **_kwargs):
        if citation_as_drafted.startswith("Second"):
            analytics.record_guided_llm_failure(model="gpt-web")
            raise RuntimeError("provider unavailable")
        analytics.record_guided_llm_response(_response("gpt-web", 17))
        return {
            "resolved": True,
            "resolved_casename": "First v. Case",
            "resolved_reporter_citation": "123 F.3d 1",
            "url": "https://example.test/case",
            "confidence_level": "high",
        }

    monkeypatch.setattr(
        hallucination_guard,
        "_resolve_citation_via_web_search",
        fake_web_search,
    )

    with app.app_context():
        step_id = analytics.start_step_analytics(
            draft_id, hallucination_guard.DOC_TYPE, hallucination_guard.RUN_STEP
        )
        with analytics.step_analytics_scope(step_id):
            result = hallucination_guard._attach_web_search_suggestions(rows)

        assert result[0]["did_you_mean"]["url"] == "https://example.test/case"
        assert "did_you_mean" not in result[1]
        db.session.expire_all()
        row = db.session.get(StepAnalytics, step_id)
        assert row.llmCallCount == 2
        assert row.llmSuccessCount == 1
        assert row.llmFailureCount == 1
        assert row.totalTokens == 17


def test_main_and_per_model_usage_are_aggregated_atomically(guided_analytics_app):
    app, draft_id = guided_analytics_app
    with app.app_context():
        step_id = analytics.start_step_analytics(draft_id, "RFP", 0)
        with analytics.step_analytics_scope(step_id):
            analytics.record_guided_llm_response(_response("gpt-5.5", 100))
            analytics.record_guided_llm_response(_response("gpt-5.5", 125))
            analytics.record_guided_llm_failure(model="gpt-5.5")
            analytics.record_guided_llm_response(_response("gpt-5.4", 75))
        analytics.finish_step_analytics(step_id, analytics.STEP_STATUS_SUCCEEDED)

        db.session.expire_all()
        step = db.session.get(StepAnalytics, step_id)
        assert step.status == "succeeded"
        assert step.completedAt is not None
        assert step.llmCallCount == 4
        assert step.llmSuccessCount == 3
        assert step.llmFailureCount == 1
        assert step.totalTokens == 300

        by_model = {
            row.model: row
            for row in StepLlmModelUsage.query.filter_by(
                stepAnalyticsId=step_id
            ).all()
        }
        assert by_model["gpt-5.5"].llmCallCount == 3
        assert by_model["gpt-5.5"].llmSuccessCount == 2
        assert by_model["gpt-5.5"].llmFailureCount == 1
        assert by_model["gpt-5.5"].totalTokens == 225
        assert by_model["gpt-5.4"].llmCallCount == 1
        assert by_model["gpt-5.4"].totalTokens == 75


def test_three_threaded_reduction_calls_attach_to_one_main_step(guided_analytics_app):
    app, draft_id = guided_analytics_app
    with app.app_context():
        step_id = analytics.start_step_analytics(draft_id, "RFP", 0)
        with analytics.step_analytics_scope(step_id):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [
                    analytics.submit_with_context(
                        pool,
                        analytics.record_guided_llm_response,
                        _response("gpt-5.5", tokens),
                    )
                    for tokens in (10, 20, 30)
                ]
                assert all(future.result() for future in futures)

        db.session.expire_all()
        step = db.session.get(StepAnalytics, step_id)
        assert step.llmCallCount == 3
        assert step.llmSuccessCount == 3
        assert step.llmFailureCount == 0
        assert step.totalTokens == 60
        assert StepAnalytics.query.filter_by(
            draftId=draft_id, docType="RFP", step=0
        ).count() == 1


def test_auxiliary_scope_uses_latest_attempt_and_preserves_status(
    guided_analytics_app,
):
    app, draft_id = guided_analytics_app
    with app.app_context():
        first = analytics.start_step_analytics(draft_id, "RFA", 0)
        analytics.finish_step_analytics(first, analytics.STEP_STATUS_SUCCEEDED)
        second = analytics.start_step_analytics(draft_id, "RFA", 0)
        analytics.finish_step_analytics(second, analytics.STEP_STATUS_FAILED)

        latest = analytics.latest_or_create_step_analytics(draft_id, "RFA", 0)
        assert latest == second
        with analytics.step_analytics_scope(latest):
            analytics.record_guided_llm_response(_response("gpt-5.5", 42))

        db.session.expire_all()
        assert db.session.get(StepAnalytics, first).llmCallCount == 0
        second_row = db.session.get(StepAnalytics, second)
        assert second_row.status == "failed"
        assert second_row.llmCallCount == 1
        assert second_row.totalTokens == 42


def test_missing_auxiliary_parent_creates_unknown_row(guided_analytics_app):
    app, draft_id = guided_analytics_app
    with app.app_context():
        step_id = analytics.latest_or_create_step_analytics(
            draft_id, "Complaint Analyzer", 1
        )
        row = db.session.get(StepAnalytics, step_id)
        assert row.status == "unknown"
        assert row.completedAt is None


def test_operation_decorator_records_separate_success_failure_and_exception_attempts(
    guided_analytics_app,
):
    app, draft_id = guided_analytics_app

    @analytics.guided_step_operation("answer", 2, lambda *_args: draft_id)
    def operation(outcome):
        if outcome == "exception":
            raise RuntimeError("draft failed")
        if outcome == "error":
            return {"message": "error", "error_message": "invalid output"}
        return {"message": "ok"}

    with app.app_context():
        assert operation("success") == {"message": "ok"}
        assert operation("error")["message"] == "error"
        with pytest.raises(RuntimeError):
            operation("exception")

        rows = StepAnalytics.query.order_by(StepAnalytics.id).all()
        assert [row.status for row in rows] == ["succeeded", "failed", "failed"]
        assert all(row.completedAt is not None for row in rows)


def test_celery_reconciliation_only_finishes_running_rows(guided_analytics_app):
    app, draft_id = guided_analytics_app
    with app.app_context():
        running_id = analytics.start_step_analytics(
            draft_id, "ROG", 1, celery_task_id="celery-1"
        )
        completed_id = analytics.start_step_analytics(
            draft_id, "ROG", 1, celery_task_id="celery-1"
        )
        analytics.finish_step_analytics(
            completed_id, analytics.STEP_STATUS_SUCCEEDED
        )

        assert analytics.finish_running_steps_for_celery_task(
            "celery-1", analytics.STEP_STATUS_CANCELLED
        )
        # A task body that unwinds after revocation must not overwrite the
        # terminal cancellation with a later success transition.
        analytics.finish_step_analytics(running_id, analytics.STEP_STATUS_SUCCEEDED)
        db.session.expire_all()
        assert db.session.get(StepAnalytics, running_id).status == "cancelled"
        assert db.session.get(StepAnalytics, completed_id).status == "succeeded"


def test_subprocess_capture_can_be_replayed_into_parent(guided_analytics_app):
    app, draft_id = guided_analytics_app
    with app.app_context():
        with analytics.capture_guided_llm_usage() as events:
            analytics.record_guided_llm_response(_response("gpt-child", 22))
            analytics.record_guided_llm_failure(model="gpt-child")

        step_id = analytics.start_step_analytics(draft_id, "complaint", 0)
        with analytics.step_analytics_scope(step_id):
            analytics.replay_guided_llm_usage(events)

        db.session.expire_all()
        row = db.session.get(StepAnalytics, step_id)
        assert row.llmCallCount == 2
        assert row.llmSuccessCount == 1
        assert row.llmFailureCount == 1
        assert row.totalTokens == 22


def test_analytics_usage_failure_is_fail_open(monkeypatch, guided_analytics_app):
    app, draft_id = guided_analytics_app
    with app.app_context():
        step_id = analytics.start_step_analytics(draft_id, "ROG", 0)
        monkeypatch.setattr(
            analytics,
            "_independent_session",
            lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")),
        )
        with analytics.step_analytics_scope(step_id):
            assert not analytics.record_guided_llm_response(
                _response("gpt-5.5", 100)
            )


def test_provider_retry_counts_failure_and_success(monkeypatch, guided_analytics_app):
    app, draft_id = guided_analytics_app
    successful_response = SimpleNamespace(
        model="gpt-5.5-2026-09-01",
        usage=SimpleNamespace(total_tokens=88),
        output_text="{not valid application json",
    )
    create = SimpleNamespace(
        side_effect=[
            ai_base.APIConnectionError(
                request=httpx.Request("POST", "https://api.openai.com/v1/responses")
            ),
            successful_response,
        ]
    )

    def responses_create(**_kwargs):
        effect = create.side_effect.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect

    client = SimpleNamespace(
        responses=SimpleNamespace(create=responses_create),
    )
    monkeypatch.setattr(ai_base, "get_openai_client", lambda: client)
    monkeypatch.setattr(ai_base, "sleep", lambda _seconds: None)

    with app.app_context():
        step_id = analytics.start_step_analytics(draft_id, "answer", 1)
        with analytics.step_analytics_scope(step_id):
            assert ai_base.chat_completion_j("prompt") == "{not valid application json"

        db.session.expire_all()
        row = db.session.get(StepAnalytics, step_id)
        assert row.llmCallCount == 2
        assert row.llmSuccessCount == 1
        assert row.llmFailureCount == 1
        assert row.totalTokens == 88
        usages = StepLlmModelUsage.query.filter_by(stepAnalyticsId=step_id).all()
        assert {(usage.model, usage.llmCallCount) for usage in usages} == {
            ("gpt-6.1-sol", 1),
            ("gpt-5.5-2026-09-01", 1),
        }


def test_responses_helpers_forward_explicit_reasoning_effort(
    monkeypatch, guided_analytics_app
):
    app, _draft_id = guided_analytics_app
    requests = []
    response = SimpleNamespace(
        model="gpt-5.6-sol-2026-09-01",
        usage=SimpleNamespace(total_tokens=21),
        output_text='{"results": []}',
        output=[],
        sources=[],
    )

    def responses_create(**kwargs):
        requests.append(kwargs)
        return response

    client = SimpleNamespace(responses=SimpleNamespace(create=responses_create))
    monkeypatch.setattr(ai_base, "get_openai_client", lambda: client)
    monkeypatch.setattr(ai_base, "client", client)

    with app.app_context():
        assert ai_base.chat_completion(
            "prompt",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
        ) == '{"results": []}'
        assert ai_base.chat_completion_j(
            "prompt",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
        ) == '{"results": []}'
        assert ai_base.web_search_completion(
            "prompt",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
        ) == '{"results": []}'

    assert len(requests) == 3
    assert all(
        request["model"] == "gpt-5.6-sol"
        and request["reasoning"] == {"effort": "medium"}
        for request in requests
    )


@pytest.mark.parametrize(
    "model,reasoning_effort,expected_reasoning",
    [
        ("gpt-6.1-sol", None, {"effort": "medium"}),
        ("gpt-6.1-sol", "high", {"effort": "high"}),
        ("gpt-6.1-sol", "xhigh", {"effort": "xhigh"}),
        ("gpt-5.5", None, None),
    ],
)
def test_responses_helpers_pin_sol_medium_and_preserve_overrides(
    monkeypatch, guided_analytics_app, model, reasoning_effort, expected_reasoning
):
    app, _draft_id = guided_analytics_app
    requests = []
    response = SimpleNamespace(
        model=model,
        usage=SimpleNamespace(total_tokens=21),
        output_text='{"results": []}',
        output=[],
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            create=lambda **kwargs: requests.append(kwargs) or response
        )
    )
    monkeypatch.setattr(ai_base, "get_openai_client", lambda: client)
    completion_kwargs = {"model": model}
    if reasoning_effort is not None:
        completion_kwargs["reasoning_effort"] = reasoning_effort

    with app.app_context():
        assert ai_base.chat_completion("prompt", **completion_kwargs) == '{"results": []}'
        assert ai_base.chat_completion_j("prompt", **completion_kwargs) == '{"results": []}'
        assert ai_base.multichat_completion_j(
            [], "prompt", **completion_kwargs
        ) == '{"results": []}'

    assert len(requests) == 3
    assert all(request["model"] == model for request in requests)
    for request in requests:
        if expected_reasoning is None:
            assert "reasoning" not in request
        else:
            assert request["reasoning"] == expected_reasoning


def test_heading_section_detection_uses_sol(monkeypatch):
    captured = {}

    def fake_chat_completion_parse(prompt, response_model, **kwargs):
        captured.update(prompt=prompt, response_model=response_model, **kwargs)
        return response_model.model_validate({
            "section headings": "FACTS (2)",
            "first heading of next section": "ARGUMENT (8)",
        })

    monkeypatch.setattr(ai_base, "chat_completion_parse", fake_chat_completion_parse)

    assert ai_base.start_end_from_headings(
        "find the facts section",
        ["FACTS", "ARGUMENT"],
        [2, 8],
    ) == [2, 8]
    assert captured == {
        "prompt": "find the facts section",
        "response_model": ai_base.HeadingSectionResult,
        "model": "gpt-6.1-sol",
        "reasoning_effort": None,
    }


def test_list_requests_provider_path_records_usage(monkeypatch, guided_analytics_app):
    app, draft_id = guided_analytics_app
    requests = []
    response = SimpleNamespace(
        model="gpt-6.1-sol",
        usage=SimpleNamespace(total_tokens=44),
        output_parsed=ai_base.RFPRequestList(requests=[]),
    )

    def responses_parse(**kwargs):
        requests.append(kwargs)
        return response

    client = SimpleNamespace(
        responses=SimpleNamespace(parse=responses_parse),
    )
    monkeypatch.setattr(ai_base, "get_openai_client", lambda: client)

    with app.app_context():
        step_id = analytics.start_step_analytics(draft_id, "RFP_RESPONSE", 0)
        with analytics.step_analytics_scope(step_id), task_analytics.task_llm_usage_scope(
            draft_id, task_analytics.LLM_USAGE_CHAT
        ):
            assert ai_base.chat_completion_j_list_requests("requests") == {
                "requests": []
            }

        db.session.expire_all()
        row = db.session.get(StepAnalytics, step_id)
        assert row.llmCallCount == 1
        assert row.llmSuccessCount == 1
        assert row.totalTokens == 44
        scoped_row = TaskAnalytics.query.filter_by(draftId=draft_id).one()
        assert scoped_row.llmCallCount == 1
        assert scoped_row.totalTokens == 44
    assert len(requests) == 1
    request = requests[0]
    assert set(request) == {"model", "reasoning", "text_format", "input"}
    assert request["model"] == "gpt-6.1-sol"
    assert request["reasoning"] == {"effort": "medium"}
    assert request["text_format"] is ai_base.RFPRequestList
    assert request["input"] == [
        {
            "role": "system",
            "content": "You are a JSON‐only extractor. When given raw text of Request for Production items, identify each individual request and return a JSON array of objects with `request_id` and `request` fields.",
        },
        {"role": "user", "content": "requests"},
    ]
    assert not {
        "function_call",
        "functions",
        "tool_choice",
        "tools",
    }.intersection(request)


def test_list_requests_preserves_extracted_text(monkeypatch):
    response = SimpleNamespace(
        model="gpt-6.1-sol",
        usage=SimpleNamespace(total_tokens=31),
        output_parsed=ai_base.RFPRequestList(
            requests=[
                ai_base.RFPRequestItem(
                    request_id="First Request",
                    request="Produce every version of the signed agreement.",
                )
            ]
        ),
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(parse=lambda **_kwargs: response),
    )
    monkeypatch.setattr(ai_base, "get_openai_client", lambda: client)

    assert ai_base.chat_completion_j_list_requests("raw requests") == {
        "requests": [
            {
                "request_id": "First Request",
                "request": "Produce every version of the signed agreement.",
            }
        ]
    }


def test_list_requests_accepts_luna_medium_override(monkeypatch):
    requests = []
    response = SimpleNamespace(
        model="gpt-6-luna",
        usage=SimpleNamespace(total_tokens=31),
        output_parsed=ai_base.RFPRequestList(
            requests=[
                ai_base.RFPRequestItem(
                    request_id="First Request",
                    request="Produce every version of the signed agreement.",
                )
            ]
        ),
    )

    def responses_parse(**kwargs):
        requests.append(kwargs)
        return response

    monkeypatch.setattr(
        ai_base,
        "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(parse=responses_parse)),
    )

    assert ai_base.chat_completion_j_list_requests(
        "raw requests", model="gpt-6-luna", reasoning_effort="medium"
    ) == {
        "requests": [
            {
                "request_id": "First Request",
                "request": "Produce every version of the signed agreement.",
            }
        ]
    }
    assert len(requests) == 1
    assert requests[0]["model"] == "gpt-6-luna"
    assert requests[0]["reasoning"] == {"effort": "medium"}
    assert requests[0]["text_format"] is ai_base.RFPRequestList
    assert requests[0]["input"][1] == {"role": "user", "content": "raw requests"}


def test_rfp_response_state1_uses_luna_medium_for_request_extraction(
    monkeypatch, guided_analytics_app
):
    app, draft_id = guided_analytics_app
    memory = {
        "body_text": json.dumps(
            ["REQUEST NO. 1: Produce the contract.", "REQUEST NO. 2: Produce emails."]
        ),
        "inputs/requests_start_end_i": "0,2",
        "inputs/side": "defendant",
        "inputs/plaintiff": "Alice",
        "inputs/defendant": "Bob",
        "inputs/venue": "United States District Court for the Northern District of California",
    }
    requests = []
    response = SimpleNamespace(
        model="gpt-6-luna",
        usage=SimpleNamespace(total_tokens=44),
        output_parsed=ai_base.RFPRequestList(
            requests=[
                ai_base.RFPRequestItem(
                    request_id="REQUEST NO. 1", request="Produce the contract."
                ),
                ai_base.RFPRequestItem(
                    request_id="REQUEST NO. 2", request="Produce emails."
                ),
            ]
        ),
    )

    def responses_parse(**kwargs):
        requests.append(kwargs)
        if kwargs["text_format"] is rfp_response_drafter.RFPObjectionBatch:
            return SimpleNamespace(
                model="gpt-6.1-sol",
                usage=SimpleNamespace(total_tokens=33),
                output_parsed=rfp_response_drafter.RFPObjectionBatch(
                    objections=[rfp_response_drafter.RFPObjection(
                        index_id=0, reason="Overbroad.", objection="Objection to scope."
                    )]
                ),
            )
        return response

    monkeypatch.setattr(
        ai_base,
        "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(parse=responses_parse)),
    )
    monkeypatch.setattr(
        rfp_response_drafter, "DBMemory", lambda **_kwargs: memory
    )
    with app.app_context():
        result = rfp_response_drafter.state1.run(
            {
                "draftId": draft_id,
                "introBody": "Intro",
                "generalBody": "General",
                "objectionBody": "Objection",
            }
        )

    assert len(requests) == 2
    assert requests[0]["model"] == "gpt-6-luna"
    assert requests[0]["reasoning"] == {"effort": "medium"}
    assert requests[0]["text_format"] is ai_base.RFPRequestList
    assert requests[1]["model"] == "gpt-6.1-sol"
    assert requests[1]["reasoning"] == {"effort": "high"}
    assert requests[1]["text_format"] is rfp_response_drafter.RFPObjectionBatch
    assert "FRCP 26(b)(1)" in requests[1]["input"][1]["content"]
    assert [item["request_id"].strip() for item in result["rfp_response"]] == [
        "REQUEST NO. 1",
        "REQUEST NO. 2",
    ]
    assert [item["request"].strip() for item in result["rfp_response"]] == [
        "Produce the contract.",
        "Produce emails.",
    ]
    assert result["rfp_response"][0]["objection"] == "Objection to scope."
    assert result["rfp_response"][0]["reason"] == "Overbroad."


def test_rfp_response_state2_writing_uses_sol_high_typed_route(
    monkeypatch, guided_analytics_app
):
    app, draft_id = guided_analytics_app
    memory = {
        "inputs/side": "defendant",
        "inputs/plaintiff": "Alice",
        "inputs/defendant": "Bob",
        "drafted_sections/requests": json.dumps(
            [
                {
                    "request_id": "REQUEST NO. 1",
                    "request": "Produce the contract.",
                    "objection": "Objection.",
                }
            ]
        ),
    }
    provider_requests = []

    def responses_parse(**kwargs):
        provider_requests.append(kwargs)
        return SimpleNamespace(
            model="gpt-6.1-sol",
            usage=SimpleNamespace(total_tokens=20),
            output_parsed=rfp_response_drafter.RFPResponseBatch(
                responses=[rfp_response_drafter.RFPResponse(
                    index_id=0, response="Subject to objection, the contract is produced."
                )]
            ),
        )

    monkeypatch.setattr(rfp_response_drafter, "DBMemory", lambda **_kwargs: memory)
    monkeypatch.setattr(
        ai_base, "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(parse=responses_parse)),
    )
    monkeypatch.setattr(
        rfp_response_drafter, "draft_full_rfp_response", lambda *_args: None
    )

    with app.app_context():
        rfp_response_drafter.state2.run({"draftId": draft_id, "req-0": "Full Production"})

    assert len(provider_requests) == 1
    assert provider_requests[0]["model"] == "gpt-6.1-sol"
    assert provider_requests[0]["reasoning"] == {"effort": "high"}
    assert provider_requests[0]["text_format"] is rfp_response_drafter.RFPResponseBatch
    assert "Produce the contract." in provider_requests[0]["input"][1]["content"]
    assert "Subject to objection, the contract is produced." in memory[
        "drafted_sections/rfp_response_text"
    ]


def test_list_requests_exhausts_four_unparsed_responses(
    monkeypatch, guided_analytics_app
):
    app, draft_id = guided_analytics_app
    provider_calls = []
    sleep_calls = []
    responses = [
        SimpleNamespace(
            model="gpt-6.1-sol",
            usage=SimpleNamespace(total_tokens=token_count),
            output_parsed=None,
        )
        for token_count in (10, 11, 12, 13)
    ]

    def responses_parse(**kwargs):
        provider_calls.append(kwargs)
        return responses.pop(0)

    client = SimpleNamespace(
        responses=SimpleNamespace(parse=responses_parse),
    )
    monkeypatch.setattr(ai_base, "get_openai_client", lambda: client)
    monkeypatch.setattr(
        ai_base, "_chat_completion_backoff_seconds", lambda attempt: 2 ** (attempt - 1)
    )
    monkeypatch.setattr(ai_base, "sleep", sleep_calls.append)

    with app.app_context():
        step_id = analytics.start_step_analytics(draft_id, "RFP_RESPONSE", 0)
        with analytics.step_analytics_scope(step_id), task_analytics.task_llm_usage_scope(
            draft_id, task_analytics.LLM_USAGE_CHAT
        ):
            assert ai_base.chat_completion_j_list_requests("raw requests") is None

        db.session.expire_all()
        row = db.session.get(StepAnalytics, step_id)
        assert row.llmCallCount == 4
        assert row.llmSuccessCount == 4
        assert row.llmFailureCount == 0
        assert row.totalTokens == 46
        scoped_row = TaskAnalytics.query.filter_by(draftId=draft_id).one()
        assert scoped_row.llmCallCount == 4
        assert scoped_row.totalTokens == 46

    assert len(provider_calls) == 4
    assert sleep_calls == [1, 2, 4]


def test_list_requests_does_not_retry_bad_request(
    monkeypatch, guided_analytics_app
):
    app, draft_id = guided_analytics_app
    provider_calls = []
    sleep_calls = []
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    error_response = httpx.Response(400, request=request)
    error = ai_base.APIStatusError(
        "invalid request",
        response=error_response,
        body={"error": {"type": "invalid_request_error"}},
    )

    def responses_parse(**kwargs):
        provider_calls.append(kwargs)
        raise error

    client = SimpleNamespace(
        responses=SimpleNamespace(parse=responses_parse),
    )
    monkeypatch.setattr(ai_base, "get_openai_client", lambda: client)
    monkeypatch.setattr(ai_base, "sleep", sleep_calls.append)

    with app.app_context():
        step_id = analytics.start_step_analytics(draft_id, "RFP_RESPONSE", 0)
        with analytics.step_analytics_scope(step_id):
            assert ai_base.chat_completion_j_list_requests("requests") is None

        db.session.expire_all()
        row = db.session.get(StepAnalytics, step_id)
        assert row.llmCallCount == 1
        assert row.llmSuccessCount == 0
        assert row.llmFailureCount == 1
        assert row.totalTokens == 0

    assert len(provider_calls) == 1
    assert sleep_calls == []


@pytest.mark.parametrize("error_kind", ["connection", "server"])
def test_list_requests_retries_transient_errors(
    monkeypatch, guided_analytics_app, error_kind
):
    app, draft_id = guided_analytics_app
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    if error_kind == "connection":
        transient_error = ai_base.APIConnectionError(request=request)
    else:
        transient_error = ai_base.APIStatusError(
            "server error",
            response=httpx.Response(500, request=request),
            body={"error": {"type": "server_error"}},
        )

    successful_response = SimpleNamespace(
        model="gpt-6.1-sol",
        usage=SimpleNamespace(total_tokens=28),
        output_parsed=ai_base.RFPRequestList(
            requests=[
                ai_base.RFPRequestItem(
                    request_id="7",
                    request="Produce the final contract.",
                )
            ]
        ),
    )
    effects = [transient_error, successful_response]
    provider_calls = []
    sleep_calls = []

    def responses_parse(**kwargs):
        provider_calls.append(kwargs)
        effect = effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect

    client = SimpleNamespace(
        responses=SimpleNamespace(parse=responses_parse),
    )
    monkeypatch.setattr(ai_base, "get_openai_client", lambda: client)
    monkeypatch.setattr(
        ai_base, "_chat_completion_backoff_seconds", lambda attempt: 2 ** (attempt - 1)
    )
    monkeypatch.setattr(ai_base, "sleep", sleep_calls.append)

    with app.app_context():
        step_id = analytics.start_step_analytics(draft_id, "RFP_RESPONSE", 0)
        with analytics.step_analytics_scope(step_id):
            assert ai_base.chat_completion_j_list_requests("requests") == {
                "requests": [
                    {
                        "request_id": "7",
                        "request": "Produce the final contract.",
                    }
                ]
            }

        db.session.expire_all()
        row = db.session.get(StepAnalytics, step_id)
        assert row.llmCallCount == 2
        assert row.llmSuccessCount == 1
        assert row.llmFailureCount == 1
        assert row.totalTokens == 28

    assert len(provider_calls) == 2
    assert sleep_calls == [1]


def test_step_analytics_api_exposes_status_totals_and_models(
    monkeypatch, guided_analytics_app
):
    app, draft_id = guided_analytics_app
    with app.app_context():
        step_id = analytics.start_step_analytics(draft_id, "complaint", 0)
        with analytics.step_analytics_scope(step_id):
            analytics.record_guided_llm_response(_response("gpt-5.5", 51))
        analytics.finish_step_analytics(step_id, analytics.STEP_STATUS_SUCCEEDED)

        with app.test_request_context("/analytics?view=steps&page=1&per_page=50"):
            response = inspect.unwrap(views.analytics_api)()
        payload = response.get_json()

    assert payload["total"] == 1
    row = payload["results"][0]
    assert row["status"] == "succeeded"
    assert row["completedAt"] is not None
    assert row["llmCallCount"] == 1
    assert row["llmSuccessCount"] == 1
    assert row["llmFailureCount"] == 0
    assert row["totalTokens"] == 51
    assert row["modelUsage"] == [
        {
            "model": "gpt-5.5",
            "llmCallCount": 1,
            "llmSuccessCount": 1,
            "llmFailureCount": 0,
            "totalTokens": 51,
        }
    ]
