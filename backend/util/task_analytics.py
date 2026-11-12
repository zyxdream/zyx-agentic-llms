"""Fail-open, operation-scoped analytics for Case Assistant tasks."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
from typing import Iterator, Mapping

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.database import db
from backend.models import ChatThread, Project, TaskAnalytics


logger = logging.getLogger(__name__)

LLM_USAGE_CHAT = "chat"
LLM_USAGE_HALLUCINATION_GUARD = "hallucination_guard"
LLM_USAGE_DOWNLOAD = "download"
LLM_USAGE_CATEGORIES = {
    LLM_USAGE_CHAT,
    LLM_USAGE_HALLUCINATION_GUARD,
    LLM_USAGE_DOWNLOAD,
}

_LLM_COUNTER_FIELDS = {
    LLM_USAGE_CHAT: ("llmCallCount", "totalTokens"),
    LLM_USAGE_HALLUCINATION_GUARD: (
        "hallucinationGuardLlmCallCount",
        "hallucinationGuardTokens",
    ),
    LLM_USAGE_DOWNLOAD: ("downloadLlmCallCount", "downloadTokens"),
}

_COUNTER_FIELDS = {
    "promptsSent",
    "answersReturned",
    "llmCallCount",
    "totalTokens",
    "hallucinationGuardAttempts",
    "hallucinationGuardSuccesses",
    "hallucinationGuardFailures",
    "hallucinationGuardLlmCallCount",
    "hallucinationGuardTokens",
    "downloadAttempts",
    "downloadSuccesses",
    "downloadFailures",
    "downloadLlmCallCount",
    "downloadTokens",
}


@dataclass(frozen=True)
class LlmUsageContext:
    draft_id: int
    category: str


_current_llm_usage: ContextVar[LlmUsageContext | None] = ContextVar(
    "case_assistant_llm_usage",
    default=None,
)


@contextmanager
def task_llm_usage_scope(draft_id: int, category: str) -> Iterator[None]:
    """Attribute successful LLM responses in this execution context to one task."""
    if category not in LLM_USAGE_CATEGORIES:
        raise ValueError(f"Unknown Case Assistant LLM usage category: {category}")
    token = _current_llm_usage.set(
        LlmUsageContext(draft_id=int(draft_id), category=category)
    )
    try:
        yield
    finally:
        _current_llm_usage.reset(token)


def _session():
    """Use an independent session so analytics never commit product state."""
    return sessionmaker(bind=db.engine, expire_on_commit=False)()


def _resolve_user_id(session, draft_id: int) -> int | None:
    return session.execute(
        select(Project.userId)
        .join(ChatThread, ChatThread.projectId == Project.id)
        .where(ChatThread.id == int(draft_id))
    ).scalar_one_or_none()


def _resolve_case_assistant_user_id(session, draft_id: int) -> int | None:
    """Return the task owner only when the draft is a Case Assistant task."""
    row = session.execute(
        select(ChatThread.docCategory, Project.userId)
        .join(Project, ChatThread.projectId == Project.id)
        .where(ChatThread.id == int(draft_id))
    ).one_or_none()
    if row is None or row.docCategory != 69:
        return None
    return row.userId


def ensure_task_analytics_row(draft_id: int, user_id: int | None = None) -> bool:
    """Ensure one aggregate row exists, without blocking product work on failure."""
    session = None
    try:
        session = _session()
        existing = session.execute(
            select(TaskAnalytics.id).where(TaskAnalytics.chat_thread_id == int(draft_id))
        ).scalar_one_or_none()
        if existing is not None:
            return True

        resolved_user_id = user_id or _resolve_user_id(session, draft_id)
        if resolved_user_id is None:
            raise ValueError(f"Cannot resolve user for Case Assistant task {draft_id}")
        session.add(
            TaskAnalytics(userId=int(resolved_user_id), chat_thread_id=int(draft_id))
        )
        session.commit()
        return True
    except IntegrityError:
        if session is not None:
            session.rollback()
            # A concurrent recorder may have inserted the unique draftId first.
            try:
                existing = session.execute(
                    select(TaskAnalytics.id).where(
                        TaskAnalytics.chat_thread_id == int(draft_id)
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return True
            except Exception:
                logger.exception(
                    "Failed to verify concurrent Case Assistant analytics insert: draft_id=%s",
                    draft_id,
                )
                return False
        logger.exception(
            "Failed to insert Case Assistant task analytics row: draft_id=%s",
            draft_id,
        )
        return False
    except Exception:
        if session is not None:
            session.rollback()
        logger.exception(
            "Failed to ensure Case Assistant task analytics row: draft_id=%s",
            draft_id,
        )
        return False
    finally:
        if session is not None:
            session.close()


def increment_task_analytics(
    draft_id: int,
    increments: Mapping[str, int],
    *,
    user_id: int | None = None,
) -> bool:
    """Atomically add counter deltas, lazily inserting the aggregate row."""
    clean: dict[str, int] = {}
    for field, amount in increments.items():
        if field not in _COUNTER_FIELDS:
            raise ValueError(f"Unknown TaskAnalytics counter: {field}")
        numeric_amount = int(amount)
        if numeric_amount < 0:
            raise ValueError(f"TaskAnalytics increments must be non-negative: {field}")
        if numeric_amount:
            clean[field] = numeric_amount
    if not clean:
        return ensure_task_analytics_row(draft_id, user_id=user_id)

    def apply_update(session) -> int:
        values = {
            field: getattr(TaskAnalytics, field) + amount
            for field, amount in clean.items()
        }
        values["updatedAt"] = func.now()
        result = session.execute(
            update(TaskAnalytics)
            .where(TaskAnalytics.chat_thread_id == int(draft_id))
            .values(**values)
        )
        return result.rowcount

    session = None
    try:
        session = _session()
        if apply_update(session) == 0:
            resolved_user_id = user_id or _resolve_user_id(session, draft_id)
            if resolved_user_id is None:
                raise ValueError(f"Cannot resolve user for Case Assistant task {draft_id}")
            session.add(
                TaskAnalytics(
                    userId=int(resolved_user_id),
                    chat_thread_id=int(draft_id),
                    **clean,
                )
            )
        session.commit()
        return True
    except IntegrityError:
        # Another worker won the lazy-insert race; retry as an atomic UPDATE.
        if session is not None:
            session.rollback()
        try:
            if apply_update(session) == 0:
                raise RuntimeError(
                    f"TaskAnalytics row disappeared during update for draft {draft_id}"
                )
            session.commit()
            return True
        except Exception:
            session.rollback()
            logger.exception(
                "Failed to retry Case Assistant task analytics update: draft_id=%s increments=%s",
                draft_id,
                clean,
            )
            return False
    except Exception:
        if session is not None:
            session.rollback()
        logger.exception(
            "Failed to update Case Assistant task analytics: draft_id=%s increments=%s",
            draft_id,
            clean,
        )
        return False
    finally:
        if session is not None:
            session.close()


def sync_task_analytics_draft_title(
    draft_id: int,
    draft_title: str | None,
) -> bool:
    """Keep the aggregate row aligned with the persisted workspace draft title.

    This deliberately uses an independent session and absorbs all persistence
    errors so analytics cannot prevent the primary Case Assistant state save.
    """
    session = None
    try:
        normalized_title = None if draft_title is None else str(draft_title)
        session = _session()
        user_id = _resolve_case_assistant_user_id(session, draft_id)
        if user_id is None:
            return False

        row = session.execute(
            select(TaskAnalytics).where(TaskAnalytics.chat_thread_id == int(draft_id))
        ).scalar_one_or_none()
        if row is None:
            session.add(
                TaskAnalytics(
                    userId=int(user_id),
                    chat_thread_id=int(draft_id),
                    draftTitle=normalized_title,
                )
            )
            session.commit()
            return True

        if row.draftTitle == normalized_title:
            return True

        session.execute(
            update(TaskAnalytics)
            .where(TaskAnalytics.chat_thread_id == int(draft_id))
            .values(draftTitle=normalized_title, updatedAt=func.now())
        )
        session.commit()
        return True
    except IntegrityError:
        # Another worker may have lazily created the aggregate row first.
        if session is not None:
            session.rollback()
        try:
            result = session.execute(
                update(TaskAnalytics)
                .where(TaskAnalytics.chat_thread_id == int(draft_id))
                .values(draftTitle=normalized_title, updatedAt=func.now())
            )
            if result.rowcount == 0:
                raise RuntimeError(
                    f"TaskAnalytics row disappeared during title sync for draft {draft_id}"
                )
            session.commit()
            return True
        except Exception:
            if session is not None:
                session.rollback()
            logger.exception(
                "Failed to retry Case Assistant draft-title sync: draft_id=%s",
                draft_id,
            )
            return False
    except Exception:
        if session is not None:
            session.rollback()
        logger.exception(
            "Failed to sync Case Assistant analytics draft title: draft_id=%s",
            draft_id,
        )
        return False
    finally:
        if session is not None:
            session.close()


def _response_total_tokens(response) -> int:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0
    value = (
        usage.get("total_tokens", 0)
        if isinstance(usage, dict)
        else getattr(usage, "total_tokens", 0)
    )
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def record_scoped_llm_response(response, *, model: str | None = None) -> bool:
    """Record one successful provider response in the active operation scope."""
    context = _current_llm_usage.get()
    if context is None:
        return False
    call_field, token_field = _LLM_COUNTER_FIELDS[context.category]
    total_tokens = _response_total_tokens(response)
    recorded = increment_task_analytics(
        context.draft_id,
        {call_field: 1, token_field: total_tokens},
    )
    if recorded:
        logger.info(
            "Recorded Case Assistant LLM usage: draft_id=%s category=%s model=%s total_tokens=%s",
            context.draft_id,
            context.category,
            model,
            total_tokens,
        )
    return recorded
