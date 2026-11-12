"""Step-scoped analytics fail open; worker account authorization fails closed."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Iterator

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.database import db as application_db
from backend.models import ChatThread, StepAnalytics, StepLlmModelUsage


logger = logging.getLogger(__name__)

STEP_STATUS_UNKNOWN = "unknown"
STEP_STATUS_RUNNING = "running"
STEP_STATUS_SUCCEEDED = "succeeded"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_CANCELLED = "cancelled"
STEP_STATUSES = {
    STEP_STATUS_UNKNOWN,
    STEP_STATUS_RUNNING,
    STEP_STATUS_SUCCEEDED,
    STEP_STATUS_FAILED,
    STEP_STATUS_CANCELLED,
}

_step_analytics_id: ContextVar[int | None] = ContextVar(
    "step_analytics_id", default=None
)
_usage_capture: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "guided_step_usage_capture", default=None
)


def _independent_session():
    """Create a transaction isolated from the product request/task session."""
    return sessionmaker(bind=application_db.engine, expire_on_commit=False)()


def _resolve_draft_id(draft_id: int | str) -> int:
    return ChatThread.resolve_id(draft_id)


def _current_celery_task_id() -> str | None:
    try:
        from celery import current_task

        task_id = getattr(getattr(current_task, "request", None), "id", None)
        return str(task_id) if task_id else None
    except Exception:
        return None


def start_step_analytics(
    draft_id: int | str,
    doc_type: str,
    step: int,
    *,
    celery_task_id: str | None = None,
    status: str = STEP_STATUS_RUNNING,
) -> int | None:
    """Create one execution-attempt row without joining the product transaction."""
    session = None
    try:
        resolved_draft_id = _resolve_draft_id(draft_id)
        draft = ChatThread.query.filter_by(id=resolved_draft_id).first()
        if draft is None or draft.parent is None:
            raise ValueError(f"Draft or parent project not found: {draft_id}")
        session = _independent_session()
        row = StepAnalytics(
            userId=draft.parent.userId,
            chat_thread_id=resolved_draft_id,
            docType=doc_type,
            step=step,
            status=status,
            celeryTaskId=celery_task_id or _current_celery_task_id(),
        )
        session.add(row)
        session.commit()
        return row.id
    except Exception:
        logger.exception(
            "Failed to start step analytics: draft_id=%s doc_type=%s step=%s",
            draft_id,
            doc_type,
            step,
        )
        if session is not None:
            session.rollback()
        return None
    finally:
        if session is not None:
            session.close()


def finish_step_analytics(step_analytics_id: int | None, status: str) -> bool:
    """Finalize a main-step attempt. Analytics failures never escape."""
    if step_analytics_id is None:
        return False
    if status not in STEP_STATUSES - {STEP_STATUS_UNKNOWN, STEP_STATUS_RUNNING}:
        raise ValueError(f"Invalid terminal step status: {status}")
    session = None
    try:
        session = _independent_session()
        session.execute(
            update(StepAnalytics)
            .where(
                StepAnalytics.id == step_analytics_id,
                StepAnalytics.status == STEP_STATUS_RUNNING,
            )
            .values(status=status, completedAt=datetime.now(timezone.utc))
        )
        session.commit()
        return True
    except Exception:
        logger.exception(
            "Failed to finish step analytics: id=%s status=%s",
            step_analytics_id,
            status,
        )
        if session is not None:
            session.rollback()
        return False
    finally:
        if session is not None:
            session.close()


def finish_running_steps_for_celery_task(
    celery_task_id: str | None, status: str
) -> bool:
    """Reconcile failure/revocation signals for rows still marked running."""
    if not celery_task_id:
        return False
    session = None
    try:
        session = _independent_session()
        session.execute(
            update(StepAnalytics)
            .where(
                StepAnalytics.celeryTaskId == str(celery_task_id),
                StepAnalytics.status == STEP_STATUS_RUNNING,
            )
            .values(status=status, completedAt=datetime.now(timezone.utc))
        )
        session.commit()
        return True
    except Exception:
        logger.exception(
            "Failed to reconcile Celery step analytics: task_id=%s status=%s",
            celery_task_id,
            status,
        )
        if session is not None:
            session.rollback()
        return False
    finally:
        if session is not None:
            session.close()


@contextmanager
def step_analytics_scope(step_analytics_id: int | None) -> Iterator[None]:
    token = _step_analytics_id.set(step_analytics_id)
    try:
        yield
    finally:
        _step_analytics_id.reset(token)


def _result_failed(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("successful") is False or result.get("error"):
        return True
    return str(result.get("message", "")).lower() == "error"


def guided_step_operation(
    doc_type: str,
    step: int,
    draft_id_getter: Callable[..., int | str],
):
    """Track one main operation attempt and scope all nested provider calls."""

    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            draft_id = draft_id_getter(*args, **kwargs)
            analytics_id = start_step_analytics(draft_id, doc_type, step)
            try:
                # Legacy guided workers share the same fresh owner check at
                # entry and at nested provider/output checkpoints.
                from backend.util.account_access import account_execution, chat_account_id
                with account_execution(chat_account_id(draft_id)), step_analytics_scope(analytics_id):
                    result = function(*args, **kwargs)
            except BaseException:
                finish_step_analytics(analytics_id, STEP_STATUS_FAILED)
                raise

            finish_step_analytics(
                analytics_id,
                STEP_STATUS_FAILED if _result_failed(result) else STEP_STATUS_SUCCEEDED,
            )
            return result

        return wrapped

    return decorator


@contextmanager
def guided_step_run(
    draft_id: int | str, doc_type: str, step: int
) -> Iterator[int | None]:
    """Track a synchronous main operation, such as a download."""
    analytics_id = start_step_analytics(draft_id, doc_type, step)
    try:
        with step_analytics_scope(analytics_id):
            yield analytics_id
    except BaseException:
        finish_step_analytics(analytics_id, STEP_STATUS_FAILED)
        raise
    else:
        finish_step_analytics(analytics_id, STEP_STATUS_SUCCEEDED)


def latest_or_create_step_analytics(
    draft_id: int | str, doc_type: str, step: int
) -> int | None:
    """Find the latest outer-step attempt or create a legacy unknown row."""
    session = None
    try:
        resolved_draft_id = _resolve_draft_id(draft_id)
        session = _independent_session()
        existing_id = (
            session.query(StepAnalytics.id)
            .filter_by(chat_thread_id=resolved_draft_id, docType=doc_type, step=step)
            .order_by(StepAnalytics.id.desc())
            .limit(1)
            .scalar()
        )
        if existing_id is not None:
            return int(existing_id)

        draft = ChatThread.query.filter_by(id=resolved_draft_id).first()
        if draft is None or draft.parent is None:
            raise ValueError(f"Draft or parent project not found: {draft_id}")
        row = StepAnalytics(
            userId=draft.parent.userId,
            chat_thread_id=resolved_draft_id,
            docType=doc_type,
            step=step,
            status=STEP_STATUS_UNKNOWN,
        )
        session.add(row)
        session.commit()
        return row.id
    except Exception:
        logger.exception(
            "Failed to attach auxiliary usage: draft_id=%s doc_type=%s step=%s",
            draft_id,
            doc_type,
            step,
        )
        if session is not None:
            session.rollback()
        return None
    finally:
        if session is not None:
            session.close()


def attach_step_usage(
    doc_type: str,
    step: int,
    draft_id_getter: Callable[..., int | str],
):
    """Attach auxiliary LLM work to its latest outer/main step."""

    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            draft_id = draft_id_getter(*args, **kwargs)
            try:
                analytics_id = latest_or_create_step_analytics(draft_id, doc_type, step)
            except Exception:
                logger.exception(
                    "Failed to prepare auxiliary step scope: %s step %s",
                    doc_type,
                    step,
                )
                analytics_id = None
            from backend.util.account_access import account_execution, chat_account_id
            with account_execution(chat_account_id(draft_id)), step_analytics_scope(analytics_id):
                return function(*args, **kwargs)

        return wrapped

    return decorator


def _response_total_tokens(response: Any) -> int:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    total_tokens = getattr(usage, "total_tokens", None)
    if total_tokens is None and isinstance(usage, dict):
        total_tokens = usage.get("total_tokens")
    try:
        return max(0, int(total_tokens or 0))
    except (TypeError, ValueError):
        return 0


def _response_model(response: Any, requested_model: str | None) -> str:
    model = getattr(response, "model", None)
    if model is None and isinstance(response, dict):
        model = response.get("model")
    return str(model or requested_model or "unknown")[:200]


def record_guided_llm_event(
    *, model: str | None, successful: bool, total_tokens: int = 0
) -> bool:
    """Atomically update parent and per-model counters for one provider attempt."""
    normalized_model = str(model or "unknown")[:200]
    tokens = max(0, int(total_tokens or 0)) if successful else 0
    captured = _usage_capture.get()
    if captured is not None:
        captured.append(
            {
                "model": normalized_model,
                "successful": bool(successful),
                "totalTokens": tokens,
            }
        )

    analytics_id = _step_analytics_id.get()
    if analytics_id is None:
        return captured is not None

    parent_values = {
        "llmCallCount": StepAnalytics.llmCallCount + 1,
        "llmSuccessCount": StepAnalytics.llmSuccessCount + (1 if successful else 0),
        "llmFailureCount": StepAnalytics.llmFailureCount + (0 if successful else 1),
        "totalTokens": StepAnalytics.totalTokens + tokens,
    }
    model_values = {
        "llmCallCount": StepLlmModelUsage.llmCallCount + 1,
        "llmSuccessCount": StepLlmModelUsage.llmSuccessCount + (1 if successful else 0),
        "llmFailureCount": StepLlmModelUsage.llmFailureCount + (0 if successful else 1),
        "totalTokens": StepLlmModelUsage.totalTokens + tokens,
    }

    for allow_insert in (True, False):
        session = None
        try:
            session = _independent_session()
            session.execute(
                update(StepAnalytics)
                .where(StepAnalytics.id == analytics_id)
                .values(**parent_values)
            )
            updated = session.execute(
                update(StepLlmModelUsage)
                .where(
                    StepLlmModelUsage.stepAnalyticsId == analytics_id,
                    StepLlmModelUsage.model == normalized_model,
                )
                .values(**model_values)
            )
            if updated.rowcount == 0 and allow_insert:
                session.add(
                    StepLlmModelUsage(
                        stepAnalyticsId=analytics_id,
                        model=normalized_model,
                        llmCallCount=1,
                        llmSuccessCount=1 if successful else 0,
                        llmFailureCount=0 if successful else 1,
                        totalTokens=tokens,
                    )
                )
            elif updated.rowcount == 0:
                raise RuntimeError("Per-model usage row disappeared during retry")
            session.commit()
            return True
        except IntegrityError:
            if session is not None:
                session.rollback()
            if not allow_insert:
                logger.exception(
                    "Failed to resolve concurrent model usage insert: step_id=%s model=%s",
                    analytics_id,
                    normalized_model,
                )
                return False
        except Exception:
            logger.exception(
                "Failed to record step-scoped LLM usage: step_id=%s model=%s",
                analytics_id,
                normalized_model,
            )
            if session is not None:
                session.rollback()
            return False
        finally:
            if session is not None:
                session.close()
    return False


def record_guided_llm_response(response: Any, *, model: str | None = None) -> bool:
    return record_guided_llm_event(
        model=_response_model(response, model),
        successful=True,
        total_tokens=_response_total_tokens(response),
    )


def record_guided_llm_failure(*, model: str | None = None) -> bool:
    return record_guided_llm_event(model=model, successful=False, total_tokens=0)


@contextmanager
def capture_guided_llm_usage() -> Iterator[list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    token = _usage_capture.set(events)
    try:
        yield events
    finally:
        _usage_capture.reset(token)


def replay_guided_llm_usage(events: list[dict[str, Any]] | None) -> None:
    for event in events or []:
        record_guided_llm_event(
            model=event.get("model"),
            successful=bool(event.get("successful")),
            total_tokens=event.get("totalTokens", 0),
        )


def submit_with_context(executor, function, /, *args, **kwargs):
    """Submit work with a fresh copy of the current operation context."""
    context = copy_context()
    return executor.submit(context.run, function, *args, **kwargs)


def analytics_record(db: SQLAlchemy, draft_id: int, doc_type: str, step: int) -> None:
    """Legacy event recorder retained for non-Guided-Drafter analytics."""
    draft = ChatThread.query.filter_by(id=draft_id).first()
    new_entry = StepAnalytics(
        userId=draft.parent.userId,
        chat_thread_id=draft_id,
        docType=doc_type,
        step=step,
    )
    db.session.add(new_entry)
    db.session.commit()


def analytics_record_fail_open(
    db: SQLAlchemy, draft_id: int, doc_type: str, step: int
) -> bool:
    """Record a legacy event without allowing analytics failures to block work."""
    try:
        analytics_record(db, draft_id, doc_type, step)
    except Exception:
        logger.exception(
            "Failed to record step analytics: draft_id=%s doc_type=%s step=%s",
            draft_id,
            doc_type,
            step,
        )
        try:
            db.session.rollback()
        except Exception:
            logger.exception(
                "Failed to roll back step analytics transaction: draft_id=%s",
                draft_id,
            )
        return False
    return True


def analytics_record_user_event(
    db: SQLAlchemy,
    user_id: int,
    doc_type: str,
    step: int,
    draft_id: int = 0,
) -> None:
    """Record analytics for non-draft (or caller-specified) user events."""
    new_entry = StepAnalytics(
        userId=user_id, chat_thread_id=draft_id, docType=doc_type, step=step
    )
    db.session.add(new_entry)
    db.session.commit()


def analytics_record_research(db: SQLAlchemy, user_id: int) -> None:
    analytics_record_user_event(
        db, user_id=user_id, doc_type="research", step=0, draft_id=0
    )


try:
    from celery.signals import task_failure, task_revoked
    from flask import has_app_context

    def _run_celery_reconciliation(sender, task_id, status):
        if has_app_context():
            finish_running_steps_for_celery_task(task_id, status)
            return
        flask_app = getattr(getattr(sender, "app", None), "flask_app", None)
        if flask_app is None:
            logger.warning(
                "Cannot reconcile Celery step analytics without a Flask app: task_id=%s",
                task_id,
            )
            return
        with flask_app.app_context():
            finish_running_steps_for_celery_task(task_id, status)

    @task_failure.connect
    def _mark_failed_celery_step(sender=None, task_id=None, **_kwargs):
        _run_celery_reconciliation(sender, task_id, STEP_STATUS_FAILED)

    @task_revoked.connect
    def _mark_cancelled_celery_step(sender=None, request=None, **_kwargs):
        _run_celery_reconciliation(
            sender, getattr(request, "id", None), STEP_STATUS_CANCELLED
        )
except Exception:  # pragma: no cover - Celery is optional in narrow unit tests.
    logger.debug("Celery step analytics signals were not registered", exc_info=True)
