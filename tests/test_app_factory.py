"""Regression coverage for Flask extension and schema initialization."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _sqlite_uri(database_path: Path) -> str:
    return f"sqlite:///{database_path}"


def _subprocess_environment(database_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "LEXINTEL_E2E": "1",
            "E2E_DATABASE_URL": _sqlite_uri(database_path),
            "E2E_REDIS_URL": "redis://localhost:6379/15",
            "E2E_CELERY_QUEUE": "lexintel-e2e-app-factory",
            "JWT_SECRET_KEY": "app-factory-test-secret",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _run_isolated(code: str, database_path: Path) -> None:
    subprocess.run(
        [sys.executable, "-c", code, str(database_path)],
        cwd=REPOSITORY_ROOT,
        env=_subprocess_environment(database_path),
        check=True,
        capture_output=True,
        text=True,
    )


def test_create_app_initializes_extensions_without_creating_tables(tmp_path):
    database_path = tmp_path / "factory.db"
    code = """
import sqlite3
import sys

from celery import Celery

from backend import app as app_module
from backend.database import db

app = app_module.create_app(
    {
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{sys.argv[1]}",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
    }
)

assert app.extensions["sqlalchemy"] is db
assert app.extensions["migrate"].migrate is app_module.migrate
assert app.extensions["migrate"].db is db
assert isinstance(app.extensions["celery"], Celery)

with sqlite3.connect(sys.argv[1]) as connection:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()

if tables:
    raise SystemExit(f"application factory created tables: {tables!r}")
"""

    _run_isolated(code, database_path)


def test_importing_app_does_not_create_tables(tmp_path):
    database_path = tmp_path / "import.db"
    code = """
import sqlite3
import sys

import backend.app  # noqa: F401

with sqlite3.connect(sys.argv[1]) as connection:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()

if tables:
    raise SystemExit(f"application import created tables: {tables!r}")
"""

    _run_isolated(code, database_path)


def test_make_celery_uses_factory_initialized_extensions(tmp_path):
    database_path = tmp_path / "celery.db"
    code = """
import sqlite3
import sys

from backend.database import db
from backend.make_celery import celery_app, flask_app

assert flask_app.extensions["sqlalchemy"] is db
assert flask_app.extensions["migrate"].db is db
assert celery_app is flask_app.extensions["celery"]

with sqlite3.connect(sys.argv[1]) as connection:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()

if tables:
    raise SystemExit(f"Celery application startup created tables: {tables!r}")
"""

    _run_isolated(code, database_path)
