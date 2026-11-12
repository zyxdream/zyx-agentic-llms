"""Real local-account/session boundary; no external user pool or test bypass."""
from pathlib import Path
import json
import pytest
from backend.app import create_app
from backend.database import db
from backend.models import User,LocalAuthBootstrap

PASSWORD='A project-only test passphrase'

@pytest.fixture
def app(tmp_path):
    value=create_app(dict(TESTING=True,SECRET_KEY='workspace-test-signing-key',
        SQLALCHEMY_DATABASE_URI='sqlite:///'+str(tmp_path/'local-auth.db')))
    with value.app_context():db.create_all()
    yield value
    with value.app_context():db.session.remove();db.engine.dispose()

def handshake(client):
    response=client.get('/api/auth/session');assert response.status_code==200
    return response.json,{'X-Workspace-CSRF':response.json['csrf_token']}

def account(client):
    status,headers=handshake(client);assert status['setup_required'] and status['user'] is None
    result=client.post('/api/auth/setup',json={'email':'owner@example.test','password':PASSWORD},headers=headers)
    assert result.status_code==201,result.json
    return result.json,{'X-Workspace-CSRF':result.json['csrf_token']}

def test_account_setup_signin_logout_and_password_storage(app):
    client=app.test_client();identity,headers=account(client)
    assert identity['user']['email']=='owner@example.test' and not identity['setup_required']
    assert client.get('/api/workspace/projects').status_code==200
    created=client.post('/api/workspace/projects',json={'name':'Private project'},headers=headers)
    assert created.status_code==201
    with app.app_context():
        user=User.query.one();assert user.password_hash and user.password_hash!=PASSWORD
        assert set(user.__table__.columns.keys()) == {'id','email','password_hash','contact_info','is_disabled'}
    assert client.post('/api/auth/logout',headers=headers).status_code==200
    assert client.get('/api/workspace/projects').status_code==401
    status,headers=handshake(client)
    assert not status['setup_required']
    bad=client.post('/api/auth/login',json={'email':'owner@example.test','password':'Incorrect test password'},headers=headers)
    assert bad.status_code==401
    good=client.post('/api/auth/login',json={'email':'owner@example.test','password':PASSWORD},headers=headers)
    assert good.status_code==200
    assert client.get('/api/workspace/projects').json['projects'][0]['name']=='Private project'

def test_setup_is_one_time_and_csrf_is_required(app):
    client=app.test_client()
    assert client.post('/api/auth/setup',json={'email':'x@y.test','password':PASSWORD}).status_code==403
    identity,headers=account(client)
    assert client.post('/api/workspace/projects',json={'name':'Missing token'}).status_code==403
    forbidden={**headers,'Origin':'http://localhost:3000'}
    assert client.post('/api/workspace/projects',json={'name':'Other application origin'},headers=forbidden).status_code==403
    assert client.post('/api/auth/setup',json={'email':'second@example.test','password':PASSWORD},headers=headers).status_code==409
    with app.app_context():assert User.query.count()==1 and LocalAuthBootstrap.query.count()==1

def test_cookies_are_project_named_and_httponly(app):
    client=app.test_client();identity,headers=account(client)
    response=client.get('/api/auth/session')
    cookie=client.get_cookie('zyx_workspace_session',path='/api')
    assert cookie is not None and cookie.http_only and cookie.same_site=='Lax'
    client.set_cookie('session','unrelated-project-cookie')
    assert client.get('/api/workspace/projects').status_code==200

def test_session_from_another_signing_key_cannot_authenticate(app,tmp_path):
    client=app.test_client();account(client)
    cookie=client.get_cookie('zyx_workspace_session',path='/api')
    other=create_app(dict(TESTING=True,SECRET_KEY='another-independent-project',SQLALCHEMY_DATABASE_URI='sqlite:///'+str(tmp_path/'other.db')))
    with other.app_context():db.create_all()
    second=other.test_client();second.set_cookie('zyx_workspace_session',cookie.value,path='/api')
    assert second.get('/api/workspace/projects').status_code==401

def test_disabled_account_invalidates_its_local_session(app):
    client=app.test_client();account(client)
    with app.app_context():User.query.one().is_disabled=True;db.session.commit()
    assert client.get('/api/workspace/projects').status_code==401

@pytest.mark.parametrize('password',['short','x'*129])
def test_setup_password_limits(app,password):
    client=app.test_client();_,headers=handshake(client)
    assert client.post('/api/auth/setup',json={'email':'owner@example.test','password':password},headers=headers).status_code==400
