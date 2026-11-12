from flask import Flask, request, jsonify
# from flask_login import LoginManager
from flask_jwt_extended import JWTManager
from flask_migrate import Migrate
from flask_cors import CORS
from werkzeug.utils import secure_filename
from celery import Celery, Task
import os
from dotenv import load_dotenv
from datetime import timedelta
from flask_cognito import CognitoAuth, CognitoAuthError
from backend.database import db
from backend import views
from backend.practice import bp_practice
from backend.models import User
from backend.constants import CELERY_QUEUE, DBURI, COGNITO_APP, COGNITO_USERPOOL, E2E_MODE, REDIS_URL
from backend.e2e_config import build_e2e_config
from collections.abc import Mapping

UPLOAD_FOLDER = 'backend/uploads'
# Keep one unbound migration extension so every Flask app created by the
# factory can register the same Flask-Migrate/Alembic integration.
migrate = Migrate()

def create_app(config_overrides: Mapping[str, object] | None = None) -> Flask:
    """Flask App factory."""
    app = Flask(__name__)
    app.config.from_mapping(
        CELERY=dict(
            broker_url=REDIS_URL,
            result_backend=REDIS_URL,
            task_ignore_result=True,
            task_default_queue=CELERY_QUEUE,
            task_default_exchange=CELERY_QUEUE,
            task_default_routing_key=CELERY_QUEUE,
            include=['backend.standalone_hg', 'backend.caseviewer_tasks', 'backend.word_addin.tasks'],
        ),
    )
    app.config.from_prefixed_env()

    if E2E_MODE:
        app.config.update(build_e2e_config())
    if config_overrides:
        app.config.update(config_overrides)

    CORS(app, resources={r"/api/*": {"origins": ["https://testapi.lextext.ai", "https://app.lexintel.ai", "http://localhost:3000"]}})

    load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))
    app.config['JWT_SECRET_KEY'] = os.environ.get("JWT_SECRET_KEY")
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(weeks=2)
    jwt = JWTManager(app)

    db_init(app)
    # Bind the shared SQLAlchemy extension to this specific Flask app.
    db.init_app(app)
    # Register Flask-Migrate commands and metadata for this app after the
    # database extension has been configured.
    migrate.init_app(app, db)
    celery_init_app(app)

    app.register_blueprint(views.bp)
    from backend.document_preparation_routes import register as register_document_preparation
    register_document_preparation(app)
    from backend.case_assistant_routes import bp as case_assistant_workflows
    app.register_blueprint(case_assistant_workflows)
    app.register_blueprint(bp_practice)
    from backend.word_addin.routes import register_word_addin
    register_word_addin(app)
    app.config['MAX_CONTENT_LENGTH'] = None  # No request size limit — per-file/page limits enforced in views
    app.secret_key = b'7f7c1717e8d3d3bb9c96753446bc097995f3617ea9a5edf894b68310e989f8a4'

    # AWS Cognito config
    app.config.update({
    'COGNITO_REGION': 'us-east-1',
    'COGNITO_USERPOOL_ID': COGNITO_USERPOOL,

    # optional
    'COGNITO_APP_CLIENT_ID': COGNITO_APP,  # client ID you wish to verify user is authenticated against
    'COGNITO_CHECK_TOKEN_EXPIRATION': False,  # disable token expiration checking for testing purposes
    'COGNITO_JWT_HEADER_NAME': 'Authorization',
    'COGNITO_JWT_HEADER_PREFIX': 'Bearer',
    })
    return app

def db_init(fapp: Flask) -> None:
    fapp.config.setdefault('SQLALCHEMY_DATABASE_URI', DBURI)
    fapp.config.setdefault('SQLALCHEMY_TRACK_MODIFICATIONS', False)

def celery_init_app(fapp: Flask) -> Celery:
    class FlaskTask(Task):
        def __call__(self, *args: object, **kwargs: object) -> object:
            # Celery runs outside an HTTP request. Supply Flask's application
            # context so tasks can use current_app and the app's SQL session.
            # This is different from service.execution(), which identifies the
            # submitted prompt/worker attempt inside that application context.
            with fapp.app_context():
                return self.run(*args, **kwargs)

    celery_app = Celery(fapp.name, task_cls=FlaskTask)
    celery_app.config_from_object(fapp.config["CELERY"])
    celery_app.conf.include = list(celery_app.conf.include or []) + ['backend.document_preparation_tasks', 'backend.case_assistant_tasks', 'backend.agent_tasks']
    # Redis chord coordination must outlive the maximum accepted prompt duration.
    # SQL retains prompt/evidence records independently; extending Redis result
    # expiry keeps the chord's short-lived completion bookkeeping available too.
    previous_expiry = celery_app.conf.result_expires or 86400
    previous_expiry = previous_expiry.total_seconds() if isinstance(previous_expiry, timedelta) else previous_expiry
    celery_app.conf.result_expires = max(int(previous_expiry),
        int(fapp.config.get('CASE_ASSISTANT_REQUEST_SECONDS', 21600)) + 3600)
    with fapp.app_context():
        from backend.util.document_preparation import queue_name
        preparation_queue = queue_name()
    celery_app.conf.beat_schedule = {
        **(celery_app.conf.beat_schedule or {}),
        'reconcile-document-preparation': {
            'task': 'backend.document_preparation_tasks.reconcile_document_preparations',
            'schedule': 30.0,
            'options': {'queue': preparation_queue},
        },
        'reconcile-case-assistant-workflows': {
            'task': 'backend.case_assistant_tasks.reconcile_case_assistant_workflows',
            'schedule': 30.0,
            # The main worker checks deadlines and resends unclaimed version-1
            # deliveries. Polling checks deadlines only; recovery needs Beat.
            'options': {'queue': celery_app.conf.task_default_queue},
        },
    }
    # Celery lifecycle signals run outside FlaskTask.__call__. Retain the Flask
    # app so fail-open analytics reconciliation can open an application context.
    celery_app.flask_app = fapp
    celery_app.set_default()
    fapp.extensions["celery"] = celery_app
    return celery_app

app = create_app()

# These extensions were previously initialized only on the module-level app:
# db.init_app(app)  # this is needed
# migrate = Migrate(app, db)
# They are intentionally initialized inside create_app() now so every app
# produced by the factory, including the Celery app, is configured exactly once.

cogauth = CognitoAuth(app)

@cogauth.identity_handler
def lookup_cognito_user(payload):
    """Look up user in our database from Cognito JWT payload.
    Auto-creates the local User row if it doesn't exist (dev convenience)."""
    user = User.query.filter(User.cognito_username == payload['username']).one_or_none()
    if user is None:
        email = payload.get('email') or payload.get('cognito:username', payload['username'])
        try:
            user = User(
                email=email,
                cognito_username=payload['username'],
                contact_info='',
                is_disabled=False,
            )
            db.session.add(user)
            db.session.commit()
            print(f"Auto-created local user: {email} (cognito: {payload['username']}, id: {user.id})")
        except Exception:
            db.session.rollback()
            user = User.query.filter(User.cognito_username == payload['username']).one()
    from backend.util.account_access import AccountUnavailable, require_enabled_account
    try:
        require_enabled_account(user.id)
    except AccountUnavailable as exc:
        raise CognitoAuthError('Account unavailable', str(exc), status_code=403) from exc
    return user

# Table creation previously ran whenever backend.app was imported:
# with app.app_context():
#     db.create_all()
# It is intentionally disabled because create_all() bypasses Alembic migration
# history, skips migration backfills, and can make a later db upgrade attempt to
# create tables that already exist. Committed migrations now own schema changes.

# this is needed for local access
ALLOWED = ['http://localhost:3000']
@app.after_request
def after_request(response):
    # when using locally has headers: Origin, 
    # something else is adding a header
    # on server after Nginx
    if 'Origin' not in request.headers:
        return response
    
    r = request.headers['Origin']
    # print(request.headers)
    if r in ALLOWED:
        response.headers.add('Access-Control-Allow-Origin', r)
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
        response.headers.add('Access-Control-Allow-Methods', 'HEAD,GET,PUT,POST,DELETE,OPTIONS')

    return response
    

if __name__ == '__main__':
    # CORS(app=app, 
    #      origins=ALLOWED)
    CORS(appresources={r"/api/*": {"origins": ["https://d2izm9uo2u1qot.cloudfront.net", "https://app.lexintel.ai", 'http://localhost:3000']}})
    app.run(debug=True)
