"""Role-based model settings: one table, no fast/deep mode."""
import json

import pytest

from backend.ai import model_settings
from backend.database import db
from backend.models import WorkspaceRun
from tests.test_workspace_api import client, new_chat


def test_default_settings_assign_logic_roles_to_sol():
    settings = model_settings.load_model_settings()
    assert settings['model_settings_version'] == 2
    assert settings['roles']['preparation'] == {
        'provider': 'openai', 'model': 'gpt-6-luna', 'effort': 'medium'}
    for role in ('controller', 'analysis', 'writer', 'verifier'):
        assert settings['roles'][role]['model'] == 'gpt-6.1-sol'
        assert settings['roles'][role]['effort'] == 'medium'
    for role in ('research', 'search', 'utility', 'court_format'):
        assert settings['roles'][role]['model'] == 'gpt-6-luna'


def test_role_environment_overrides(monkeypatch):
    monkeypatch.setenv('WORKSPACE_MODEL_CONTROLLER', 'openai:configured-controller')
    monkeypatch.setenv('WORKSPACE_EFFORT_WRITER', 'high')
    settings = model_settings.load_model_settings()
    assert settings['roles']['controller'] == {
        'provider': 'openai', 'model': 'configured-controller', 'effort': 'medium'}
    assert settings['roles']['writer']['effort'] == 'high'
    assert model_settings.default_model_setting('controller')['model'] == 'configured-controller'
    monkeypatch.setenv('WORKSPACE_MODEL_PREPARATION', 'openai:configured-preparation')
    assert model_settings.default_model_setting('preparation')['model'] == 'configured-preparation'


def test_invalid_settings_fail_loudly(monkeypatch):
    monkeypatch.setenv('WORKSPACE_MODEL_CONTROLLER', 'unknown:model')
    with pytest.raises(ValueError, match='Unsupported model provider'):
        model_settings.load_model_settings()
    monkeypatch.delenv('WORKSPACE_MODEL_CONTROLLER')
    monkeypatch.setenv('WORKSPACE_EFFORT_WRITER', 'extreme')
    with pytest.raises(ValueError, match='Unsupported reasoning effort'):
        model_settings.load_model_settings()
    monkeypatch.delenv('WORKSPACE_EFFORT_WRITER')
    monkeypatch.setenv('WORKSPACE_MODEL_SETTINGS_FILE', '/nonexistent/models.json')
    with pytest.raises(ValueError, match='not found'):
        model_settings.load_model_settings()


def test_new_run_saves_one_model_settings_snapshot(client):
    chat = new_chat(client)
    response = client.post(f"/api/workspace/conversations/{chat['id']}/runs",
                           json={'query': 'A role settings request', 'run_id': 'wf_role_settings'})
    assert response.status_code == 202, response.json
    data = json.loads(db.session.get(WorkspaceRun, 'wf_role_settings').data_json)
    assert 'assistant_mode' not in data
    assert data['model_settings']['roles']['controller']['model'] == 'gpt-6.1-sol'
    assert data['model_settings']['roles']['writer']['model'] == 'gpt-6.1-sol'
    assert data['model_settings']['roles']['preparation']['model'] == 'gpt-6-luna'


def test_resubmission_keeps_saved_settings_after_config_change(client, monkeypatch):
    chat = new_chat(client)
    url = f"/api/workspace/conversations/{chat['id']}/runs"
    body = {'query': 'Keep the same settings', 'run_id': 'wf_settings_stable'}
    assert client.post(url, json=body).status_code == 202
    before = json.loads(db.session.get(WorkspaceRun, 'wf_settings_stable').data_json)
    monkeypatch.setenv('WORKSPACE_MODEL_CONTROLLER', 'openai:changed-controller')
    assert client.post(url, json=body).status_code == 202
    db.session.expire_all()
    after = json.loads(db.session.get(WorkspaceRun, 'wf_settings_stable').data_json)
    assert after['model_settings'] == before['model_settings']


def test_different_settings_on_existing_run_are_rejected(client):
    chat = new_chat(client)
    url = f"/api/workspace/conversations/{chat['id']}/runs"
    body = {'query': 'A fixed settings request', 'run_id': 'wf_settings_conflict'}
    assert client.post(url, json=body).status_code == 202
    changed = dict(body)
    changed['model_settings'] = {
        'model_settings_version': 2,
        'roles': {role: {'provider': 'openai', 'model': 'different', 'effort': 'medium'}
                  for role in model_settings.MODEL_ROLES},
    }
    assert client.post(url, json=changed).status_code == 400


def test_historical_row_without_snapshot_uses_current_table():
    settings = model_settings.run_model_settings({'assistant_mode': 'fast', 'model': 'gpt-6.1-sol'})
    assert settings['roles']['controller']['model'] == 'gpt-6.1-sol'
    assert settings['roles']['writer']['model'] == 'gpt-6.1-sol'
    assert settings['roles']['preparation']['model'] == 'gpt-6-luna'
    assert 'legacy_mode' not in settings


def test_attach_strips_obsolete_selectors_and_freezes_the_table():
    data = model_settings.attach_model_settings({'query': 'Old client', 'assistant_mode': 'deep'})
    assert 'assistant_mode' not in data
    assert data['model_settings']['roles']['controller']['model'] == 'gpt-6.1-sol'
