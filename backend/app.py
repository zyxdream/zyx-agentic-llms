"""Flask factory for the local Workspace API and Celery workers."""
from collections.abc import Mapping
from datetime import timedelta
from flask import Flask
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from flask_migrate import Migrate
from celery import Celery, Task
from backend.config import ROOT, app_settings
from backend.database import db
migrate = Migrate()

def create_app(config_overrides: Mapping[str, object] | None = None):
    app = Flask(__name__, instance_path=str(ROOT / 'instance'))
    app.config.update(app_settings())
    if config_overrides: app.config.update(config_overrides)
    if not app.config.get('SECRET_KEY'):
        if app.config.get('TESTING'): app.config['SECRET_KEY'] = app.config['JWT_SECRET_KEY'] = 'isolated-test-secret'
        else: raise ValueError('Set ZYX_FLASK_SECRET_KEY in the local .env')
    app.config.setdefault('JWT_ACCESS_TOKEN_EXPIRES', timedelta(weeks=2))
    CORS(app, resources={r'/api/*': {'origins': app.config['CORS_ALLOWED_ORIGINS']}},supports_credentials=True)
    JWTManager(app)
    db.init_app(app)
    from backend import models
    migrate.init_app(app, db)
    from backend.auth import init_auth
    init_auth(app)
    celery_init_app(app)
    from backend.workspace.routes import bp
    app.register_blueprint(bp)
    from backend.workspace_runs import bp as runs
    app.register_blueprint(runs)
    from backend.document_preparation_routes import register
    register(app)
    @app.get('/api/health')
    def health(): return {'status':'ok','application':'zyx-agentic-llms','environment':'dev-monica'}
    return app

def celery_init_app(app):
    class FlaskTask(Task):
        def __call__(self, *args, **kwargs):
            with app.app_context(): return self.run(*args, **kwargs)
    celery_app = Celery('zyx-agentic-llms', task_cls=FlaskTask)
    celery_app.config_from_object(app.config['CELERY'])
    queue = celery_app.conf.task_default_queue
    preparation_queue = app.config.get('DOCUMENT_PREPARATION_QUEUE', queue + '.documents')
    celery_app.conf.beat_schedule = {
        'reconcile-document-preparation': {'task':'backend.document_preparation_tasks.reconcile_document_preparations','schedule':30.0,'options':{'queue':preparation_queue}},
        'reconcile-workspace-runs': {'task':'backend.workspace.execution.reconcile_runs','schedule':30.0,'options':{'queue':queue}},
    }
    celery_app.flask_app = app
    celery_app.set_default()
    app.extensions['celery'] = celery_app
    return celery_app
