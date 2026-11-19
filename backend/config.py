"""Project-owned local configuration; no settings are read from another project."""
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / '.private'
load_dotenv(ROOT / '.env', override=os.getenv('ZYX_E2E') != '1')
os.environ['AWS_SHARED_CREDENTIALS_FILE'] = str(PRIVATE / 'aws/credentials')
os.environ['AWS_CONFIG_FILE'] = str(PRIVATE / 'aws/config')
os.environ['AWS_PROFILE'] = 'workspace'
os.environ['AWS_EC2_METADATA_DISABLED'] = 'true'
for _legacy_celery_setting in ('CELERY_BROKER_URL','CELERY_RESULT_BACKEND','CELERY_CONFIG_MODULE'):
    os.environ.pop(_legacy_celery_setting,None)
os.environ['TIKTOKEN_CACHE_DIR'] = str(ROOT / 'instance/cache/tiktoken')
PUBLIC = json.loads((ROOT / 'config' / 'dev-monica.json').read_text())
ENVIRONMENT = os.getenv('ENVIRONMENT', 'dev-monica')
if ENVIRONMENT != 'dev-monica':
    raise ValueError('Only ENVIRONMENT=dev-monica is supported')
E2E_MODE = os.getenv('ZYX_E2E') == '1'

def app_settings():
    prefix = 'zyx-agentic-llms.e2e' if E2E_MODE else 'zyx-agentic-llms.dev'
    redis_url = os.getenv('E2E_REDIS_URL' if E2E_MODE else 'ZYX_REDIS_URL',
        'redis://127.0.0.1:6390/15' if E2E_MODE else 'redis://127.0.0.1:6390/0')
    if not E2E_MODE:
        address=urlparse(redis_url)
        if address.hostname not in {'localhost','127.0.0.1'} or address.port != PUBLIC['redisPort']:
            raise ValueError('Workspace must use its own local Redis port from config/dev-monica.json')
    queue = os.getenv('E2E_CELERY_QUEUE' if E2E_MODE else 'ZYX_CELERY_QUEUE', prefix)
    if not queue.startswith(prefix):
        raise ValueError('Celery queue must use the application namespace: ' + prefix)
    database_url = os.getenv('E2E_DATABASE_URL' if E2E_MODE else 'ZYX_DATABASE_URL',
        'sqlite:///' + str(ROOT / 'instance' / ('zyx-agentic-llms-e2e.db' if E2E_MODE else 'zyx-agentic-llms.db')))
    if not E2E_MODE:
        url=make_url(database_url)
        if url.drivername != 'sqlite' or not url.database or not Path(url.database).is_absolute() or not Path(url.database).resolve().is_relative_to((ROOT/'instance').resolve()):
            raise ValueError('Local Workspace database must be a SQLite file inside this project instance directory')
    secret = os.getenv('ZYX_FLASK_SECRET_KEY')
    from backend.ai import model_settings
    model_settings.load_model_settings()
    app_url=PUBLIC['appUrl']
    frontend_port=PUBLIC['frontendPort']
    settings = {
        'SECRET_KEY': secret, 'JWT_SECRET_KEY': secret,
        'SQLALCHEMY_DATABASE_URI': database_url, 'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'WORKSPACE_LEGAL_ENABLED': os.getenv('WORKSPACE_LEGAL_ENABLED','1') != '0',
        'WORKSPACE_STORAGE_DIR':str(ROOT/'instance/workspace'),
        'DOCUMENT_PREPARATION_SOURCE_DIR':str(ROOT/'instance/workspace/sources'),
        'AGENT_PYTHON_STORAGE_DIR':str(ROOT/'instance/workspace/python'),
        'DOCUMENT_EXPORT_STORAGE_DIR':str(ROOT/'instance/workspace/exports'),
        'WORKSPACE_DOCUMENT_EXPORT_DIR':str(ROOT/'instance/workspace/exports'),
        'PUBLIC_APP_URL':app_url,'MAX_CONTENT_LENGTH':None,
        'CORS_ALLOWED_ORIGINS':[app_url,f'http://127.0.0.1:{frontend_port}'],
        'SESSION_COOKIE_NAME':'zyx_workspace_session', 'SESSION_COOKIE_HTTPONLY':True,
        'SESSION_COOKIE_SAMESITE':'Lax','SESSION_COOKIE_PATH':'/api',
        'SESSION_COOKIE_SECURE':False,
        'CELERY':{
            'broker_url':redis_url,'result_backend':redis_url,
            'task_default_queue':queue,'task_default_exchange':queue,'task_default_routing_key':queue,
            'task_ignore_result':True,'result_expires':25200,
            'broker_transport_options':{'global_keyprefix':prefix+':'},
            'result_backend_transport_options':{'global_keyprefix':prefix+':'},
            'include':['backend.agent_tasks','backend.workspace.execution','backend.document_preparation_tasks','backend.workspace.state'],
        },
    }
    if E2E_MODE:settings.update(TESTING=True,E2E_CELERY_QUEUE=queue,E2E_REDIS_URL=redis_url)
    return settings
