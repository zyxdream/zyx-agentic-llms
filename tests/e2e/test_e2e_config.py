import pytest

from backend.e2e_config import (
    DEFAULT_E2E_CELERY_QUEUE,
    DEFAULT_E2E_REDIS_URL,
    build_e2e_config,
    validate_e2e_queue,
)
from backend.e2e_worker import worker_main


def test_build_e2e_config_defaults_to_isolated_local_resources():
    config = build_e2e_config({"LEXINTEL_E2E": "1"})

    assert config["E2E_CELERY_QUEUE"] == DEFAULT_E2E_CELERY_QUEUE
    assert config["E2E_REDIS_URL"] == DEFAULT_E2E_REDIS_URL
    assert config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///e2e.db"
    assert config["CELERY"]["task_default_queue"] == DEFAULT_E2E_CELERY_QUEUE
    assert config["CELERY"]["task_ignore_result"] is False


def test_build_e2e_config_accepts_hosted_redis_and_mysql():
    config = build_e2e_config(
        {
            "LEXINTEL_E2E": "1",
            "E2E_REDIS_URL": "rediss://e2e-redis.example/4",
            "E2E_CELERY_RESULT_BACKEND": "rediss://e2e-results.example/4",
            "E2E_DATABASE_URL": "mysql+pymysql://e2e-user:e2e-pass@db.example/e2e",
            "E2E_CELERY_QUEUE": "lexintel-e2e-run-123",
        }
    )

    assert config["E2E_REDIS_URL"] == "rediss://e2e-redis.example/4"
    assert config["SQLALCHEMY_DATABASE_URI"].startswith("mysql+")
    assert config["CELERY"]["broker_url"] == "rediss://e2e-redis.example/4"
    assert config["CELERY"]["result_backend"] == "rediss://e2e-results.example/4"
    assert config["CELERY"]["task_default_queue"] == "lexintel-e2e-run-123"


def test_build_e2e_config_requires_explicit_opt_in():
    with pytest.raises(RuntimeError, match="LEXINTEL_E2E=1"):
        build_e2e_config({})


@pytest.mark.parametrize(
    "queue", ["", "celery", "default", " celery ", "integration-tests"]
)
def test_validate_e2e_queue_rejects_non_isolated_names(queue):
    with pytest.raises(ValueError):
        validate_e2e_queue(queue)


def test_validate_e2e_queue_strips_a_custom_queue_name():
    assert validate_e2e_queue("  lexintel-e2e-run-123  ") == "lexintel-e2e-run-123"


@pytest.mark.parametrize("argument", ["-Q", "--queues", "--queues=celery"])
def test_e2e_worker_rejects_queue_overrides(monkeypatch, argument):
    monkeypatch.setenv("LEXINTEL_E2E", "1")

    with pytest.raises(ValueError, match="queue is managed"):
        worker_main([argument])
