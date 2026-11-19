"""Configuration must not silently reconnect to the original application's resources."""
import json
from pathlib import Path
import pytest
from backend import config


def test_legacy_environment_variables_are_not_used(monkeypatch):
    monkeypatch.setattr(config,'E2E_MODE',False)
    for name,value in {'REDIS_URL':'redis://localhost:6379/0','DATABASE_URL':'sqlite:////tmp/other-app.db',
        'WORKSPACE_DATABASE_URL':'sqlite:////tmp/old.db','CELERY_QUEUE':'celery','COGNITO_USERPOOL_ID':'old-pool',
        'COGNITO_APP_CLIENT_ID':'old-client','FLASK_SECRET_KEY':'legacy-secret','JWT_SECRET_KEY':'legacy-secret'}.items():monkeypatch.setenv(name,value)
    settings=config.app_settings()
    assert settings['CELERY']['broker_url']=='redis://127.0.0.1:6390/0'
    assert settings['SECRET_KEY']!='legacy-secret'
    assert Path(settings['SQLALCHEMY_DATABASE_URI'].split('sqlite:///')[1]).is_relative_to(config.ROOT/'instance')
    assert not any(name.startswith('COGNITO_') for name in settings)
    assert 'http://localhost:3000' not in settings['CORS_ALLOWED_ORIGINS']
    assert settings['SESSION_COOKIE_NAME']=='zyx_workspace_session'


def test_public_config_has_no_shared_identity_provider():
    public=config.PUBLIC
    assert public['frontendPort']==3010 and public['backendPort']==5010 and public['redisPort']==6390
    assert public['authentication']=='local' and public['apiUrl']=='/api/'
    assert not {'userPoolId','userPoolClientId','identityPoolId'} & set(public)


def test_normal_configuration_rejects_other_application_redis(monkeypatch):
    monkeypatch.setattr(config,'E2E_MODE',False);monkeypatch.setenv('ZYX_REDIS_URL','redis://127.0.0.1:6379/0')
    with pytest.raises(ValueError,match='own local Redis'):config.app_settings()


def test_normal_database_must_be_inside_the_project(monkeypatch):
    monkeypatch.setattr(config,'E2E_MODE',False);monkeypatch.setenv('ZYX_DATABASE_URL','sqlite:////tmp/shared-project.db')
    with pytest.raises(ValueError,match='inside this project'):config.app_settings()


def test_launcher_discards_inherited_project_configuration(monkeypatch,tmp_path):
    import importlib.util
    spec=importlib.util.spec_from_file_location('workspace_dev',config.ROOT/'scripts/dev.py')
    launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher,'ROOT',tmp_path)
    (tmp_path/'.env').write_text('ZYX_FLASK_SECRET_KEY=project-test-secret\nOPENAI_API_KEY=project-test-key\nMODEL_PROVIDER=openai\n')
    for name,value in {'CONDA_PREFIX':'/tmp/other-runtime','PATH':'/tmp/other-runtime/bin:/usr/bin',
        'PYTHONPATH':'/tmp/another-project','CELERY_BROKER_URL':'redis://localhost:6379/0',
        'WORKSPACE_STORAGE_DIR':'/tmp/shared-files','AWS_ACCESS_KEY_ID':'inherited-test-key',
        'FLASK_DEBUG':'1','ZYX_E2E':'1','MODEL_PROVIDER':'inherited-provider'}.items():monkeypatch.setenv(name,value)
    env=launcher.environment()
    assert env['PYTHONPATH']==str(tmp_path)
    assert env['PATH']==str(tmp_path/'.venv/bin')+':/usr/bin'
    assert env['OPENAI_API_KEY']=='project-test-key' and env['MODEL_PROVIDER']=='openai'
    assert env['ZYX_FLASK_SECRET_KEY']=='project-test-secret' and env['ZYX_E2E']=='0'
    assert env['AWS_SHARED_CREDENTIALS_FILE']==str(tmp_path/'.private/aws/credentials')
    assert not {'CONDA_PREFIX','CELERY_BROKER_URL','WORKSPACE_STORAGE_DIR','AWS_ACCESS_KEY_ID','FLASK_DEBUG'} & env.keys()
