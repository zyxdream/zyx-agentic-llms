from copy import deepcopy
from datetime import datetime
import importlib
import inspect
import json
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import Flask
import pytest
import sqlalchemy as sa

from backend.database import db
from backend.models import Drafts, Project, TaskAnalytics, TextEntry, User
from backend.util import task_analytics
from backend.util import workspace_store


def test_task_analytics_migration_backfills_case_assistant_tasks(monkeypatch):
    migration = importlib.import_module(
        "migrations.versions.a1b2c3d4e5f6_add_case_assistant_task_analytics"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    projects = sa.Table(
        "projects",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("userId", sa.Integer, nullable=False),
    )
    drafts = sa.Table(
        "drafts",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("projectId", sa.Integer, nullable=False),
        sa.Column("docCategory", sa.Integer, nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(projects.insert(), {"id": 5, "userId": 17})
        connection.execute(
            drafts.insert(),
            [
                {"id": 42, "projectId": 5, "docCategory": 69},
                {"id": 43, "projectId": 5, "docCategory": 1},
            ],
        )
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()

        rows = connection.execute(
            sa.text(
                "SELECT userId, draftId, promptsSent, downloadTokens "
                "FROM taskAnalytics ORDER BY draftId"
            )
        ).mappings().all()

    assert rows == [
        {
            "userId": 17,
            "draftId": 42,
            "promptsSent": 0,
            "downloadTokens": 0,
        }
    ]


def test_draft_title_migration_backfills_saved_workspace_state(monkeypatch):
    migration = importlib.import_module(
        "migrations.versions.b2c3d4e5f6a7_add_draft_title_to_task_analytics"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    task_analytics_table = sa.Table(
        "taskAnalytics",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("draftId", sa.Integer, nullable=False),
        sa.Column("updatedAt", sa.DateTime, nullable=False),
    )
    text_entries = sa.Table(
        "textEntries",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("draftId", sa.Integer, nullable=False),
        sa.Column("textName", sa.String(250)),
        sa.Column("content", sa.Text),
    )
    metadata.create_all(engine)
    original_updated_at = datetime(2026, 1, 2, 3, 4, 5)

    with engine.begin() as connection:
        connection.execute(
            task_analytics_table.insert(),
            [
                {"id": draft_id, "draftId": draft_id, "updatedAt": original_updated_at}
                for draft_id in range(1, 7)
            ],
        )
        connection.execute(
            text_entries.insert(),
            [
                {
                    "id": 10,
                    "draftId": 1,
                    "textName": "state",
                    "content": json.dumps(
                        {"draft": {"title": "  Motion to Dismiss  ", "sections": []}}
                    ),
                },
                {
                    "id": 20,
                    "draftId": 2,
                    "textName": "state",
                    "content": json.dumps(
                        {
                            "draft": {
                                "title": "",
                                "sections": [
                                    {"section_name": "Argument", "section_content": "Text"}
                                ],
                            }
                        }
                    ),
                },
                {
                    "id": 30,
                    "draftId": 3,
                    "textName": "state",
                    "content": json.dumps({"draft": {"title": "", "sections": []}}),
                },
                {"id": 40, "draftId": 4, "textName": "state", "content": "not-json"},
                {
                    "id": 50,
                    "draftId": 6,
                    "textName": "state",
                    "content": json.dumps(
                        {"draft": {"title": "Canonical title", "sections": []}}
                    ),
                },
                {
                    "id": 51,
                    "draftId": 6,
                    "textName": "state",
                    "content": json.dumps(
                        {"draft": {"title": "Later duplicate", "sections": []}}
                    ),
                },
            ],
        )
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()

        rows = connection.execute(
            sa.text(
                'SELECT draftId, draftTitle, updatedAt FROM taskAnalytics ORDER BY draftId'
            )
        ).mappings().all()
        migration.downgrade()
        remaining_columns = {
            column["name"] for column in sa.inspect(connection).get_columns("taskAnalytics")
        }

    assert [row["draftTitle"] for row in rows] == [
        "Motion to Dismiss",
        "Untitled document",
        None,
        None,
        None,
        "Canonical title",
    ]
    assert all(row["updatedAt"] == "2026-01-02 03:04:05.000000" for row in rows)
    assert "draftTitle" not in remaining_columns


@pytest.fixture
def task_analytics_app():
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite://",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        user = User(email="analytics@example.com")
        project = Project(name="Matter", userId=None)
        db.session.add(user)
        db.session.flush()
        project.userId = user.id
        db.session.add(project)
        db.session.flush()
        draft = Drafts(
            display_name="Case Assistant Task",
            docCategory=69,
            projectId=project.id,
            location=None,
        )
        db.session.add(draft)
        db.session.commit()
        workspace_store.initialize(draft.id)
        db.session.commit()
        yield app, user.id, draft.id
        db.session.remove()
        db.drop_all()


def test_task_analytics_lazy_creation_and_atomic_increments(task_analytics_app):
    app, user_id, draft_id = task_analytics_app
    with app.app_context():
        assert task_analytics.increment_task_analytics(
            draft_id,
            {"promptsSent": 1, "llmCallCount": 2, "totalTokens": 125},
        )
        assert task_analytics.increment_task_analytics(
            draft_id,
            {"promptsSent": 1, "llmCallCount": 1, "totalTokens": 75},
        )

        row = TaskAnalytics.query.filter_by(draftId=draft_id).one()
        assert row.userId == user_id
        assert row.promptsSent == 2
        assert row.answersReturned == 0
        assert row.llmCallCount == 3
        assert row.totalTokens == 200
        assert row.hallucinationGuardAttempts == 0
        assert row.downloadAttempts == 0


def test_task_analytics_survives_task_deletion(task_analytics_app):
    app, user_id, draft_id = task_analytics_app
    with app.app_context():
        assert task_analytics.ensure_task_analytics_row(draft_id, user_id=user_id)
        draft = db.session.get(Drafts, draft_id)
        db.session.delete(draft)
        db.session.commit()

        row = TaskAnalytics.query.filter_by(draftId=draft_id).one()
        assert row.userId == user_id


def test_case_assistant_creation_creates_task_analytics_row(
    monkeypatch,
    task_analytics_app,
):
    from backend import views

    app, user_id, _draft_id = task_analytics_app
    with app.app_context():
        project = Project.query.filter_by(userId=user_id).one()
        monkeypatch.setattr(views, "current_user", SimpleNamespace(id=user_id))
        with app.test_request_context(
            json={
                "proj-id": project.id,
                "draft-name": "New Case Assistant Task",
                "draft-type": 69,
            }
        ):
            body, status = inspect.unwrap(views.create_draft)()

        assert status == 200
        created = Drafts.query.filter_by(public_id=body["draftId"]).one()
        analytics = TaskAnalytics.query.filter_by(draftId=created.id).one()
        assert analytics.userId == user_id
        assert analytics.draftTitle is None
        assert analytics.promptsSent == 0


def test_task_analytics_admin_view_serializes_aggregate_rows(
    task_analytics_app,
):
    from backend import views

    app, _user_id, draft_id = task_analytics_app
    with app.app_context():
        task_analytics.increment_task_analytics(
            draft_id,
            {
                "promptsSent": 2,
                "answersReturned": 1,
                "downloadLlmCallCount": 1,
                "downloadTokens": 300,
            },
        )
        analytics = TaskAnalytics.query.filter_by(draftId=draft_id).one()
        analytics.draftTitle = "Summary Judgment Memorandum"
        db.session.commit()
        with app.test_request_context(
            "/api/analytics?view=tasks&page=1&per_page=50",
            method="POST",
        ):
            response = inspect.unwrap(views.analytics_api)()

        payload = response.get_json()
        assert payload["view"] == "tasks"
        assert payload["total"] == 1
        assert payload["results"][0]["draftId"] == draft_id
        assert payload["results"][0]["draftTitle"] == "Summary Judgment Memorandum"
        assert payload["results"][0]["promptsSent"] == 2
        assert payload["results"][0]["answersReturned"] == 1
        assert payload["results"][0]["downloadLlmCallCount"] == 1
        assert payload["results"][0]["downloadTokens"] == 300


def test_workspace_state_save_keeps_task_analytics_draft_title_in_sync(
    task_analytics_app,
):
    from backend import everything
    from backend.util.db import DBMemory

    app, _user_id, draft_id = task_analytics_app
    with app.app_context():
        memory = DBMemory(db=db, doc_id=draft_id)
        state = everything.load_state_from_memory(memory)
        state["draft"] = {
            "title": "  Defense Memorandum  ",
            "sections": [{"section_name": "Facts", "section_content": "Content"}],
        }
        everything.save_draft_to_state(state, state["draft"])
        everything.save_state_to_memory(state, memory)
        db.session.expire_all()
        row = TaskAnalytics.query.filter_by(draftId=draft_id).one()
        assert row.draftTitle == "Defense Memorandum"

        state["draft"]["title"] = ""
        everything.save_draft_to_state(state, state["draft"])
        everything.save_state_to_memory(state, memory)
        db.session.expire_all()
        row = TaskAnalytics.query.filter_by(draftId=draft_id).one()
        assert row.draftTitle == "Untitled document"

        state["draft"] = {"title": "", "sections": []}
        everything.save_draft_to_state(state, state["draft"])
        everything.save_state_to_memory(state, memory)
        db.session.expire_all()
        row = TaskAnalytics.query.filter_by(draftId=draft_id).one()
        assert row.draftTitle is None


def test_manual_right_panel_title_edit_updates_task_analytics(
    monkeypatch,
    task_analytics_app,
):
    from backend import views
    from backend.util.db import DBMemory

    app, user_id, draft_id = task_analytics_app
    with app.app_context():
        draft_record = db.session.get(Drafts, draft_id)

        def allow_owned_draft(**_kwargs):
            allow_owned_draft._last_draft = draft_record
            return None

        monkeypatch.setattr(views, "ownership_check", allow_owned_draft)
        monkeypatch.setattr(views, "current_user", SimpleNamespace(id=user_id))

        with app.test_request_context(
            json={
                "draftId": draft_id,
                "expected_draft_version": workspace_store.load(draft_id)['draft_version'],
                "draft": {
                    "title": "Manually Renamed Brief",
                    "sections": [
                        {"section_name": "Introduction", "section_content": "Text"}
                    ],
                },
            }
        ):
            body, status = inspect.unwrap(views.update_section_text)()

        assert status == 200
        assert body["draft"]["title"] == "Manually Renamed Brief"
        db.session.expire_all()
        row = TaskAnalytics.query.filter_by(draftId=draft_id).one()
        assert row.draftTitle == "Manually Renamed Brief"
        persisted = json.loads(DBMemory(db=db, doc_id=draft_id)["state"])
        assert persisted["draft"]["title"] == "Manually Renamed Brief"


def test_unchanged_workspace_title_does_not_touch_analytics_timestamp(
    task_analytics_app,
):
    from backend import everything
    from backend.util.db import DBMemory

    app, _user_id, draft_id = task_analytics_app
    with app.app_context():
        memory = DBMemory(db=db, doc_id=draft_id)
        state = everything.load_state_from_memory(memory)
        state["draft"] = {"title": "Existing Title", "sections": []}
        everything.save_draft_to_state(state, state["draft"])
        everything.save_state_to_memory(state, memory)

        fixed_timestamp = datetime(2020, 1, 2, 3, 4, 5)
        db.session.execute(
            sa.update(TaskAnalytics)
            .where(TaskAnalytics.draftId == draft_id)
            .values(updatedAt=fixed_timestamp)
        )
        db.session.commit()

        everything.save_state_to_memory(state, memory)
        db.session.expire_all()
        row = TaskAnalytics.query.filter_by(draftId=draft_id).one()
        assert row.updatedAt == fixed_timestamp


def test_workspace_title_sync_ignores_non_case_assistant_drafts(
    task_analytics_app,
):
    from backend import everything
    from backend.util.db import DBMemory

    app, user_id, _draft_id = task_analytics_app
    with app.app_context():
        project = Project.query.filter_by(userId=user_id).one()
        other_draft = Drafts(
            display_name="Other Drafter",
            docCategory=1,
            projectId=project.id,
            location=None,
        )
        db.session.add(other_draft)
        db.session.commit()

        memory = DBMemory(db=db, doc_id=other_draft.id)
        state = everything.load_state_from_memory(memory)
        state["draft"] = {"title": "Should not record", "sections": []}
        everything.save_draft_to_state(state, state["draft"])
        everything.save_state_to_memory(state, memory)

        assert TaskAnalytics.query.filter_by(draftId=other_draft.id).count() == 0


def test_workspace_state_save_survives_draft_title_analytics_failure(
    monkeypatch,
    task_analytics_app,
):
    from backend import everything
    from backend.util.db import DBMemory

    app, _user_id, draft_id = task_analytics_app
    with app.app_context():
        monkeypatch.setattr(
            task_analytics,
            "_session",
            lambda: (_ for _ in ()).throw(RuntimeError("analytics unavailable")),
        )
        memory = DBMemory(db=db, doc_id=draft_id)
        state = everything.load_state_from_memory(memory)
        state["draft"] = {"title": "Still persists", "sections": []}

        everything.save_draft_to_state(state, state["draft"])
        everything.save_state_to_memory(state, memory)

        assert workspace_store.load(draft_id)["draft"]["title"] == "Still persists"


def test_user_deletion_removes_task_analytics(monkeypatch, task_analytics_app):
    from backend import views

    app, user_id, draft_id = task_analytics_app
    with app.app_context():
        user = db.session.get(User, user_id)
        user.is_disabled = True
        db.session.commit()
        assert task_analytics.ensure_task_analytics_row(draft_id, user_id=user_id)
        monkeypatch.setattr(views, "delete_cognito_user", lambda username: None)

        with app.test_request_context(method="POST"):
            response, status = inspect.unwrap(views.delete_user)(user_id)

        assert status == 200
        assert response.get_json()["message"] == "User deleted successfully"
        assert TaskAnalytics.query.filter_by(userId=user_id).count() == 0


@pytest.mark.parametrize(
    ("category", "expected"),
    (
        (
            task_analytics.LLM_USAGE_CHAT,
            {"llmCallCount": 1, "totalTokens": 41},
        ),
        (
            task_analytics.LLM_USAGE_HALLUCINATION_GUARD,
            {"hallucinationGuardLlmCallCount": 1, "hallucinationGuardTokens": 41},
        ),
        (
            task_analytics.LLM_USAGE_DOWNLOAD,
            {"downloadLlmCallCount": 1, "downloadTokens": 41},
        ),
    ),
)
def test_scoped_llm_response_routes_usage_to_one_category(
    monkeypatch,
    category,
    expected,
):
    recorded = []
    monkeypatch.setattr(
        task_analytics,
        "increment_task_analytics",
        lambda draft_id, increments: recorded.append((draft_id, increments)) or True,
    )
    response = SimpleNamespace(usage=SimpleNamespace(total_tokens=41))

    with task_analytics.task_llm_usage_scope(99, category):
        assert task_analytics.record_scoped_llm_response(
            response,
            model="gpt-test",
        )

    assert recorded == [(99, expected)]


def test_provider_response_without_scope_is_not_recorded(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        task_analytics,
        "increment_task_analytics",
        lambda *args, **kwargs: recorded.append((args, kwargs)) or True,
    )

    assert not task_analytics.record_scoped_llm_response(
        SimpleNamespace(usage=SimpleNamespace(total_tokens=100))
    )
    assert recorded == []


def test_llm_wrapper_records_provider_usage_before_output_validation(monkeypatch):
    from backend.ai import base

    recorded = []
    response = SimpleNamespace(
        output_text="not valid json",
        usage=SimpleNamespace(total_tokens=63),
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kwargs: response)
    )
    monkeypatch.setattr(base, "get_openai_client", lambda: client)
    monkeypatch.setattr(
        task_analytics,
        "increment_task_analytics",
        lambda draft_id, increments: recorded.append((draft_id, increments)) or True,
    )

    with task_analytics.task_llm_usage_scope(
        42,
        task_analytics.LLM_USAGE_DOWNLOAD,
    ):
        output = base.chat_completion_j("format this", model="gpt-5.5")

    assert output == "not valid json"
    assert recorded == [
        (42, {"downloadLlmCallCount": 1, "downloadTokens": 63})
    ]


def test_chat_usage_scope_reaches_threaded_file_llm_calls(monkeypatch):
    from backend import everything

    recorded = []
    monkeypatch.setattr(
        task_analytics,
        "increment_task_analytics",
        lambda draft_id, increments: recorded.append((draft_id, increments)) or True,
    )

    def fake_chat_completion(*args, **kwargs):
        task_analytics.record_scoped_llm_response(
            SimpleNamespace(usage=SimpleNamespace(total_tokens=12)),
            model="gpt-test",
        )
        return '{"query_relevant_summary":"summary","source_anchors":["page 1"]}'

    monkeypatch.setattr(everything, "chat_completion_j", fake_chat_completion)
    entries = [
        {
            "file_id": "one",
            "file_name": "one.pdf",
            "file_scope": "matter",
            "chunks": [{"chunk_index": 0, "content": "one"}],
        },
        {
            "file_id": "two",
            "file_name": "two.pdf",
            "file_scope": "matter",
            "chunks": [{"chunk_index": 0, "content": "two"}],
        },
    ]

    with task_analytics.task_llm_usage_scope(
        77,
        task_analytics.LLM_USAGE_CHAT,
    ):
        results = everything._summarize_full_content_entries_threaded(
            "summarize",
            entries,
        )

    assert len(results) == 2
    assert recorded == [
        (77, {"llmCallCount": 1, "totalTokens": 12}),
        (77, {"llmCallCount": 1, "totalTokens": 12}),
    ]


def test_threaded_llm_usage_can_persist_with_copied_app_context(
    monkeypatch,
    task_analytics_app,
):
    from backend import everything

    app, _user_id, draft_id = task_analytics_app

    def fake_chat_completion(*args, **kwargs):
        task_analytics.record_scoped_llm_response(
            SimpleNamespace(usage=SimpleNamespace(total_tokens=27)),
            model="gpt-test",
        )
        return '{"query_relevant_summary":"summary","source_anchors":["page 1"]}'

    monkeypatch.setattr(everything, "chat_completion_j", fake_chat_completion)
    entries = [
        {
            "file_id": "one",
            "file_name": "one.pdf",
            "file_scope": "matter",
            "chunks": [{"chunk_index": 0, "content": "one"}],
        }
    ]

    with app.app_context():
        with task_analytics.task_llm_usage_scope(
            draft_id,
            task_analytics.LLM_USAGE_CHAT,
        ):
            everything._summarize_full_content_entries_threaded(
                "summarize",
                entries,
            )

        row = TaskAnalytics.query.filter_by(draftId=draft_id).one()
        assert row.llmCallCount == 1
        assert row.totalTokens == 27


def test_chat_answer_counter_only_records_success(monkeypatch):
    from backend import everything

    recorded = []
    monkeypatch.setattr(
        everything,
        "increment_task_analytics",
        lambda draft_id, increments: recorded.append((draft_id, increments)) or True,
    )

    assert everything._record_successful_chat_answer(
        7,
        {"response": "Completed", "error": False},
    )
    assert not everything._record_successful_chat_answer(
        7,
        {"response": "Try again", "error": True},
    )
    assert not everything._record_successful_chat_answer(
        7,
        {"response": "", "cancelled": True},
    )
    assert not everything._record_successful_chat_answer(
        7,
        {"response": "Choose an option", "awaiting_confirmation": True},
    )
    assert recorded == [(7, {"answersReturned": 1})]


def _fake_ownership(draft):
    def ownership_check(*args, **kwargs):
        return None

    ownership_check._last_draft = draft
    return ownership_check


def test_case_assistant_download_records_outcome_and_llm_usage(monkeypatch):
    from backend import views

    draft = SimpleNamespace(id=42, public_id="public-task", display_name="Task")
    lifecycle = []
    usage = []
    monkeypatch.setattr(views, "current_user", SimpleNamespace(id=17))
    monkeypatch.setattr(views, "ownership_check", _fake_ownership(draft))
    monkeypatch.setattr(views, "analytics_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        views,
        "increment_task_analytics",
        lambda draft_id, increments, **kwargs: lifecycle.append((draft_id, increments)) or True,
    )
    monkeypatch.setattr(
        task_analytics,
        "increment_task_analytics",
        lambda draft_id, increments, **kwargs: usage.append((draft_id, increments)) or True,
    )
    monkeypatch.setattr(views, "DBMemory", lambda **kwargs: {})

    def fake_word_export(*args, **kwargs):
        task_analytics.record_scoped_llm_response(
            SimpleNamespace(usage=SimpleNamespace(total_tokens=88)),
            model="gpt-5.5",
        )

    monkeypatch.setattr(views, "draft_full_everything_word", fake_word_export)
    monkeypatch.setattr(views, "send_file", lambda *args, **kwargs: "download")

    app = Flask(__name__)
    with app.test_request_context(json={"sectionOrder": []}):
        result = views.download_word_everything.__wrapped__("public-task")

    assert result == "download"
    assert lifecycle == [
        (42, {"downloadAttempts": 1}),
        (42, {"downloadSuccesses": 1}),
    ]
    assert usage == [
        (42, {"downloadLlmCallCount": 1, "downloadTokens": 88})
    ]


def test_case_assistant_download_keeps_usage_when_generation_fails(monkeypatch):
    from backend import views

    draft = SimpleNamespace(id=42, public_id="public-task", display_name="Task")
    lifecycle = []
    usage = []
    monkeypatch.setattr(views, "current_user", SimpleNamespace(id=17))
    monkeypatch.setattr(views, "ownership_check", _fake_ownership(draft))
    monkeypatch.setattr(views, "analytics_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        views,
        "increment_task_analytics",
        lambda draft_id, increments, **kwargs: lifecycle.append((draft_id, increments)) or True,
    )
    monkeypatch.setattr(
        task_analytics,
        "increment_task_analytics",
        lambda draft_id, increments, **kwargs: usage.append((draft_id, increments)) or True,
    )
    monkeypatch.setattr(views, "DBMemory", lambda **kwargs: {})

    def failing_word_export(*args, **kwargs):
        task_analytics.record_scoped_llm_response(
            SimpleNamespace(usage=SimpleNamespace(total_tokens=55)),
            model="gpt-5.5",
        )
        raise RuntimeError("docx generation failed")

    monkeypatch.setattr(views, "draft_full_everything_word", failing_word_export)

    app = Flask(__name__)
    with app.test_request_context(json={}):
        with pytest.raises(RuntimeError, match="docx generation failed"):
            views.download_word_everything.__wrapped__("public-task")

    assert lifecycle == [
        (42, {"downloadAttempts": 1}),
        (42, {"downloadFailures": 1}),
    ]
    assert usage == [
        (42, {"downloadLlmCallCount": 1, "downloadTokens": 55})
    ]


def test_case_assistant_prompt_counted_after_scheduling(monkeypatch):
    from backend import views
    from backend import case_assistant_tasks
    from backend.util import case_assistant_workflow
    from backend.util import document_preparation
    monkeypatch.setattr(document_preparation, 'build_snapshot', lambda *a: {'version': 2, 'files': []})
    monkeypatch.setattr(document_preparation, 'mark_snapshot_committed', lambda *a: None)
    monkeypatch.setattr(views.db.session, 'commit', lambda: None)

    draft = SimpleNamespace(id=42, public_id="public-task")
    recorded = []
    queued = SimpleNamespace(id="wf_case-assistant-turn", dispatch_pending=True,
                             payload_json=None, chat_thread_id=42, draft_document_id=501)
    monkeypatch.setattr(workspace_store, 'document_for_chat',
        lambda chat_id, document_id: SimpleNamespace(public_id='public-document'))
    queued_args = []
    metadata = []
    monkeypatch.setattr(views, "current_user", SimpleNamespace(id=17))
    monkeypatch.setattr(views, "ownership_check", _fake_ownership(draft))
    monkeypatch.setattr(case_assistant_workflow, 'submit', lambda user_id, data:
        (queued_args.append(data) or queued))
    monkeypatch.setattr(case_assistant_tasks, 'dispatch_planning', lambda row: metadata.append(row.id))
    monkeypatch.setattr(
        views,
        "increment_task_analytics",
        lambda draft_id, increments, **kwargs: recorded.append(
            (draft_id, increments, kwargs)
        ) or True,
    )
    monkeypatch.setattr(
        views,
        "_cancel_redis",
        SimpleNamespace(set=lambda *args, **kwargs: metadata.append((args, kwargs))),
    )

    app = Flask(__name__)
    with app.test_request_context(
        json={"draftId": "public-task", "query": "Draft a motion"}
    ):
        response, status = views.everything_api.__wrapped__()

    assert status == 200
    assert response == {"result_id": "wf_case-assistant-turn", "workflow_run_id": "wf_case-assistant-turn",
                        "chat_thread_id": "public-task", "draft_document_id": "public-document"}
    assert queued_args[0]['query'] == 'Draft a motion'
    assert metadata == ['wf_case-assistant-turn']
    assert recorded == [
        (42, {"promptsSent": 1}, {"user_id": 17})
    ]


def test_hallucination_guard_request_records_attempt_and_enqueue_failure(monkeypatch):
    from backend import views

    draft = SimpleNamespace(id=42, public_id="public-task")
    recorded = []
    monkeypatch.setattr(views, "current_user", SimpleNamespace(id=17))
    monkeypatch.setattr(views, "ownership_check", _fake_ownership(draft))
    monkeypatch.setattr(views.ChatThread, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(views, "analytics_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        views,
        "increment_task_analytics",
        lambda draft_id, increments, **kwargs: recorded.append(
            (draft_id, increments)
        ) or True,
    )
    monkeypatch.setattr(
        views.hallucination_guard_task,
        "apply_async",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("queue down")),
    )

    app = Flask(__name__)
    with app.test_request_context(json={"draftId": "public-task"}):
        with pytest.raises(RuntimeError, match="queue down"):
            views.hallucination_guard_api.__wrapped__()

    assert recorded == [
        (42, {"hallucinationGuardAttempts": 1}),
        (42, {"hallucinationGuardFailures": 1}),
    ]


def test_hallucination_guard_task_records_success_separately_from_usage(monkeypatch):
    from backend import everything

    state = {
        "draft": {
            "title": "Brief",
            "sections": [
                {
                    "section_id": "1",
                    "section_name": "Argument",
                    "section_content": "Text",
                }
            ],
        },
        "chat_history": [],
    }
    outcomes = []
    usage = []
    monkeypatch.setattr(everything, "DBMemory", lambda **kwargs: {})
    monkeypatch.setattr(everything, "load_state_from_memory", lambda mem: state)
    monkeypatch.setattr(everything, "save_state_to_memory", lambda s, mem, **kwargs: deepcopy(s))
    monkeypatch.setattr(
        everything,
        "ChatThread",
        SimpleNamespace(
            query=SimpleNamespace(
                get=lambda draft_id: SimpleNamespace(public_id="public-task")
            )
        ),
    )
    monkeypatch.setattr(
        everything,
        "increment_task_analytics",
        lambda draft_id, increments: outcomes.append((draft_id, increments)) or True,
    )
    monkeypatch.setattr(
        task_analytics,
        "increment_task_analytics",
        lambda draft_id, increments: usage.append((draft_id, increments)) or True,
    )

    def fake_guard(*args, **kwargs):
        task_analytics.record_scoped_llm_response(
            SimpleNamespace(usage=SimpleNamespace(total_tokens=144)),
            model="gpt-5.5",
        )
        return {"response": "<p>Complete</p>"}

    monkeypatch.setattr(everything._hg, "run_hallucination_guard_on_text", fake_guard)

    result = everything.hallucination_guard_task.run({"doc_id": 42})

    assert result["response"] == "<p>Complete</p>"
    assert outcomes == [(42, {"hallucinationGuardSuccesses": 1})]
    assert usage == [
        (
            42,
            {
                "hallucinationGuardLlmCallCount": 1,
                "hallucinationGuardTokens": 144,
            },
        )
    ]
