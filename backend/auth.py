"""Project-local accounts and signed sessions. No external user pool is consulted."""
from datetime import timedelta
from functools import wraps
import secrets
from flask import Blueprint,current_app,g,request,session
from werkzeug.local import LocalProxy
from werkzeug.security import generate_password_hash,check_password_hash
from sqlalchemy.exc import IntegrityError
from backend.database import db
from backend.models import User,LocalAuthBootstrap

bp=Blueprint('local_auth',__name__,url_prefix='/api/auth')
current_user=LocalProxy(lambda:getattr(g,'workspace_user',None))

def _token():
    if not session.get('csrf'):session['csrf']=secrets.token_urlsafe(32)
    return session['csrf']

def _csrf_valid():
    origin=request.headers.get('Origin')
    allowed=set(current_app.config['CORS_ALLOWED_ORIGINS'])
    allowed.add('http://localhost:5010');allowed.add('http://127.0.0.1:5010')
    if origin and origin not in allowed:return False
    expected=session.get('csrf');provided=request.headers.get('X-Workspace-CSRF')
    return bool(expected and provided and secrets.compare_digest(expected,provided))

def _user():
    user_id=session.get('user_id')
    if type(user_id) is not int:return None
    value=db.session.get(User,user_id)
    if value is None or value.is_disabled or not value.password_hash:
        session.clear();return None
    return value

def auth_required(fn):
    @wraps(fn)
    def call(*args,**kwargs):
        value=_user()
        if value is None:return {'msg':'Sign in to Workspace.'},401
        if request.method not in {'GET','HEAD','OPTIONS'} and not _csrf_valid():
            return {'msg':'Invalid session request. Refresh Workspace and try again.'},403
        g.workspace_user=value
        return fn(*args,**kwargs)
    return call

def _public(value):return {'id':value.id,'email':value.email}
def _status(value=None):
    return {'user':_public(value) if value else None,'csrf_token':_token(),
        'setup_required':db.session.get(LocalAuthBootstrap,1) is None}

def _credentials():
    data=request.get_json(silent=True) or {}
    email=data.get('email');password=data.get('password')
    if not isinstance(email,str) or not isinstance(password,str):raise ValueError('Enter an email and password.')
    email=email.strip().lower()
    if '@' not in email or not 3<=len(email)<=120:raise ValueError('Enter a valid email address.')
    if not 12<=len(password)<=128:raise ValueError('Use a password of 12 to 128 characters.')
    return email,password

def _login(value):
    session.clear();session['user_id']=value.id;session.permanent=True
    current_app.permanent_session_lifetime=timedelta(days=7)
    return _status(value)

@bp.get('/session')
def session_status():return _status(_user())

@bp.post('/setup')
def setup():
    if not _csrf_valid():return {'msg':'Refresh the setup page and try again.'},403
    if db.session.get(LocalAuthBootstrap,1):return {'msg':'Workspace already has a local account. Sign in.'},409
    try:email,password=_credentials()
    except ValueError as exc:return {'msg':str(exc)},400
    value=User(email=email,password_hash=generate_password_hash(password),contact_info='',is_disabled=False)
    db.session.add(value)
    try:
        db.session.flush()
        db.session.add(LocalAuthBootstrap(id=1,user_id=value.id));db.session.commit()
    except IntegrityError:
        db.session.rollback();return {'msg':'Setup was already completed or this email exists. Sign in.'},409
    return _login(value),201

@bp.post('/login')
def login():
    if not _csrf_valid():return {'msg':'Refresh the sign-in page and try again.'},403
    try:email,password=_credentials()
    except ValueError as exc:return {'msg':str(exc)},400
    value=User.query.filter_by(email=email).first()
    # Equal-cost hash verification for an unknown email avoids an account timing shortcut.
    digest=value.password_hash if value and value.password_hash else current_app.extensions['workspace_dummy_password']
    valid=check_password_hash(digest,password)
    if not valid or value is None or value.is_disabled:return {'msg':'Email or password is incorrect.'},401
    return _login(value)

@bp.post('/logout')
@auth_required
def logout():
    session.clear()
    return _status()

def init_auth(app):
    # Process-local validation dummy, never an account or accepted credential.
    app.extensions['workspace_dummy_password']=generate_password_hash(secrets.token_urlsafe(32))
    app.register_blueprint(bp)
