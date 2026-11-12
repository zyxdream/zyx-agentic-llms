"""Configuration for isolated end-to-end test runs.

E2E mode is deliberately opt-in. It must use a queue that cannot be confused
with the normal Celery queues, so a test worker cannot consume application
jobs accidentally.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


E2E_ENABLED_ENV = "LEXINTEL_E2E"
DEFAULT_E2E_REDIS_URL = "redis://localhost:6379/15"
DEFAULT_E2E_CELERY_QUEUE = "lexintel-e2e"
E2E_CELERY_QUEUE_PREFIX = "lexintel-e2e"
FORBIDDEN_E2E_QUEUES = frozenset({"celery", "default"})


def validate_e2e_queue(queue: str) -> str:
    """Return a safe E2E queue name or raise a clear configuration error."""
    normalized = (queue or "").strip()
    if not normalized:
        raise ValueError("E2E_CELERY_QUEUE must not be empty")
    if normalized in FORBIDDEN_E2E_QUEUES:
        raise ValueError(
            f"E2E_CELERY_QUEUE={normalized!r} is not isolated; use a dedicated queue"
        )
    if not (
        normalized == E2E_CELERY_QUEUE_PREFIX
        or normalized.startswith(f"{E2E_CELERY_QUEUE_PREFIX}-")
    ):
        raise ValueError(
            "E2E_CELERY_QUEUE must start with "
            f"{E2E_CELERY_QUEUE_PREFIX!r} to prevent queue sharing"
        )
    return normalized


def build_e2e_config(env: Mapping[str, str] | None = None) -> dict[str, object]:
    """Build Flask/Celery overrides for a real, isolated E2E run.

    The explicit opt-in prevents a normal application process from silently
    switching to test infrastructure because an E2E variable was partially
    configured.
    """
    values = os.environ if env is None else env
    if values.get(E2E_ENABLED_ENV) != "1":
        raise RuntimeError(
            "E2E mode is disabled; set LEXINTEL_E2E=1 to build E2E configuration"
        )

    broker_url = values.get("E2E_REDIS_URL", DEFAULT_E2E_REDIS_URL).strip()
    if not broker_url:
        raise ValueError("E2E_REDIS_URL must not be empty")

    result_backend = values.get("E2E_CELERY_RESULT_BACKEND", broker_url).strip()
    if not result_backend:
        raise ValueError("E2E_CELERY_RESULT_BACKEND must not be empty")

    queue = validate_e2e_queue(
        values.get("E2E_CELERY_QUEUE", DEFAULT_E2E_CELERY_QUEUE)
    )
    database_url = values.get("E2E_DATABASE_URL", "sqlite:///e2e.db").strip()
    if not database_url:
        raise ValueError("E2E_DATABASE_URL must not be empty")

    celery_config = {
        "broker_url": broker_url,
        "result_backend": result_backend,
        "task_ignore_result": False,
        "task_default_queue": queue,
        "task_default_exchange": queue,
        "task_default_routing_key": queue,
        "include": ["backend.standalone_hg", "backend.caseviewer_tasks"],
    }

    return {
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": database_url,
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "CELERY": celery_config,
        "E2E_CELERY_QUEUE": queue,
        "E2E_REDIS_URL": broker_url,
    }
