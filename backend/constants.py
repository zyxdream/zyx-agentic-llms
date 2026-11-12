"""Local infrastructure constants; no company or hosted-environment profiles."""
import os
import sys
from backend.config import ROOT, PUBLIC, ENVIRONMENT, E2E_MODE, app_settings
PDF_OCR_BACKEND = 'cloud'
STATIC_FOLDER = str(ROOT / 'instance' / 'workspace' / 'exports')
UPLOAD_FOLDER = str(ROOT / 'instance' / 'workspace' / 'uploads')
SOURCE_CODE_ROOT = str(ROOT)
PYTHON_EXECUTABLE = sys.executable
REDIS_URL = app_settings()['CELERY']['broker_url']
CELERY_QUEUE = app_settings()['CELERY']['task_default_queue']
REDIS_KEY_PREFIX = ('zyx-agentic-llms.e2e:' if E2E_MODE else 'zyx-agentic-llms.dev:')
API_URL = PUBLIC['apiUrl']
DBURI = app_settings()['SQLALCHEMY_DATABASE_URI']
