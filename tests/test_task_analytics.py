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
from backend.models import Drafts, Project, WorkspaceUsage, TextEntry, User
from backend.util import task_analytics
from backend.util import workspace_store






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

        row = WorkspaceUsage.query.filter_by(draftId=draft_id).one()
        assert row.userId == user_id
        assert row.promptsSent == 2
        assert row.answersReturned == 0
        assert row.llmCallCount == 3
        assert row.totalTokens == 200
        assert row.downloadAttempts == 0


def test_task_analytics_survives_task_deletion(task_analytics_app):
    app, user_id, draft_id = task_analytics_app
    with app.app_context():
        assert task_analytics.ensure_task_analytics_row(draft_id, user_id=user_id)
        draft = db.session.get(Drafts, draft_id)
        db.session.delete(draft)
        db.session.commit()

        row = WorkspaceUsage.query.filter_by(draftId=draft_id).one()
        assert row.userId == user_id




















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






def test_chat_answer_counter_only_records_success(monkeypatch):
    from backend.workspace import state as everything

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
