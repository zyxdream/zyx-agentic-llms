"""Role-based model policy: no user-facing fast/deep mode."""
import json

import pytest

from backend.ai import model_policy
from backend.database import db
from backend.models import WorkspaceRun
from tests.test_workspace_api import client, new_chat


def test_default_policy_assigns_logic_roles_to_sol():
    policy = model_policy.load_model_policy()
    assert policy['policy_version'] == 2
    assert policy['roles']['preparation'] == {
        'provider': 'openai', 'model': 'gpt-6-luna', 'effort': 'medium'}
    for role in ('controller', 'analysis', 'writer', 'verifier'):
        assert policy['roles'][role]['model'] == 'gpt-6.1-sol'
        assert policy['roles'][role]['effort'] == 'medium'
    for role in ('research', 'search', 'utility', 'court_format'):
        assert policy['roles'][role]['model'] == 'gpt-6-luna'


def test_role_environment_overrides(monkeypatch):
    monkeypatch.setenv('WORKSPACE_MODEL_CONTROLLER', 'openai:configured-controller')
    monkeypatch.setenv('WORKSPACE_EFFORT_WRITER', 'high')
    policy = model_policy.load_model_policy()
    assert policy['roles']['controller'] == {
        'provider': 'openai', 'model': 'configured-controller', 'effort': 'medium'}
    assert policy['roles']['writer']['effort'] == 'high'
    assert model_policy.default_role('controller')['model'] == 'configured-controller'
    monkeypatch.setenv('WORKSPACE_MODEL_PREPARATION', 'openai:configured-preparation')
    assert model_policy.default_role('preparation')['model'] == 'configured-preparation'


def test_invalid_policy_values_fail_loudly(monkeypatch):
    monkeypatch.setenv('WORKSPACE_MODEL_CONTROLLER', 'unknown:model')
    with pytest.raises(ValueError, match='Unsupported model provider'):
        model_policy.load_model_policy()
    monkeypatch.delenv('WORKSPACE_MODEL_CONTROLLER')
    monkeypatch.setenv('WORKSPACE_EFFORT_WRITER', 'extreme')
    with pytest.raises(ValueError, match='Unsupported reasoning effort'):
        model_policy.load_model_policy()
    monkeypatch.delenv('WORKSPACE_EFFORT_WRITER')
    monkeypatch.setenv('WORKSPACE_MODEL_POLICY_FILE', '/nonexistent/models.json')
    with pytest.raises(ValueError, match='not found'):
        model_policy.load_model_policy()


def test_new_run_saves_one_role_policy_snapshot(client):
    chat = new_chat(client)
    response = client.post(f"/api/workspace/conversations/{chat['id']}/runs",
                           json={'query': 'A role policy request', 'run_id': 'wf_role_policy'})
    assert response.status_code == 202, response.json
    data = json.loads(db.session.get(WorkspaceRun, 'wf_role_policy').data_json)
    assert 'assistant_mode' not in data
    assert data['model_policy']['roles']['controller']['model'] == 'gpt-6.1-sol'
    assert data['model_policy']['roles']['writer']['model'] == 'gpt-6.1-sol'
    assert data['model_policy']['roles']['preparation']['model'] == 'gpt-6-luna'


def test_resubmission_keeps_saved_policy_after_config_change(client, monkeypatch):
    chat = new_chat(client)
    url = f"/api/workspace/conversations/{chat['id']}/runs"
    body = {'query': 'Keep the same policy', 'run_id': 'wf_policy_stable'}
    assert client.post(url, json=body).status_code == 202
    before = json.loads(db.session.get(WorkspaceRun, 'wf_policy_stable').data_json)
    monkeypatch.setenv('WORKSPACE_MODEL_CONTROLLER', 'openai:changed-controller')
    assert client.post(url, json=body).status_code == 202
    db.session.expire_all()
    after = json.loads(db.session.get(WorkspaceRun, 'wf_policy_stable').data_json)
    assert after['model_policy'] == before['model_policy']


def test_different_policy_on_existing_run_is_rejected(client):
    chat = new_chat(client)
    url = f"/api/workspace/conversations/{chat['id']}/runs"
    body = {'query': 'A fixed policy request', 'run_id': 'wf_policy_conflict'}
    assert client.post(url, json=body).status_code == 202
    changed = dict(body)
    changed['model_policy'] = {
        'policy_version': 2,
        'roles': {role: {'provider': 'openai', 'model': 'different', 'effort': 'medium'}
                  for role in model_policy.ROLES},
    }
    assert client.post(url, json=changed).status_code == 400


def test_legacy_rows_keep_historical_policy():
    fast = model_policy.resolve_policy({'assistant_mode': 'fast'})
    assert fast['legacy_mode'] == 'fast'
    assert fast['roles']['writer'] == {
        'provider': 'openai', 'model': 'gpt-6-luna', 'effort': 'medium'}
    deep = model_policy.resolve_policy({'assistant_mode': 'deep'})
    assert deep['roles']['writer'] == {
        'provider': 'openai', 'model': 'gpt-6.1-sol', 'effort': 'high'}
    assert deep['roles']['preparation']['model'] == 'gpt-6-luna'
    assert deep['roles']['utility'] == {
        'provider': 'openai', 'model': 'gpt-6.1-sol', 'effort': 'medium'}
    assert model_policy.resolve_policy({})['legacy_mode'] == 'deep'
    assert model_policy.resolve_policy({'model': 'gpt-6.1-sol'})['legacy_mode'] == 'fast'


def test_explicit_legacy_selectors_are_not_replaced_by_a_new_policy():
    data = model_policy.apply_model_policy({'query': 'Old client', 'assistant_mode': 'deep'})
    assert 'model_policy' not in data
    assert data['assistant_mode'] == 'deep'
