"""New UI requests use a saved server policy; historical runs keep theirs."""
import json
import pytest
from backend.ai import model_policy
from backend.config import app_settings
from backend.database import db
from backend.models import WorkspaceRun
from tests.test_workspace_api import client, new_chat


@pytest.mark.parametrize('mode,effort',[('fast','medium'),('deep','high')])
def test_mode_less_requests_save_the_backend_default(client,monkeypatch,mode,effort):
    client.application.config['WORKSPACE_DEFAULT_MODE']=mode
    monkeypatch.setattr(model_policy,'WORKSPACE_FAST_MODEL','configured-fast-model')
    monkeypatch.setenv('WORKSPACE_DEEP_MODEL','configured-deep-model')
    chat=new_chat(client)
    response=client.post(f"/api/workspace/conversations/{chat['id']}/runs",json={'query':'Hello Workspace','run_id':'wf_server_policy'})
    assert response.status_code==202,response.json
    saved=json.loads(db.session.get(WorkspaceRun,'wf_server_policy').data_json)
    assert saved['assistant_mode']==mode
    resolved=model_policy.resolve_assistant_mode(saved)
    assert model_policy._assistant_model(resolved)==f'configured-{mode}-model'
    assert model_policy._output_effort(resolved)==effort


def test_current_default_matches_the_former_ui_default(monkeypatch):
    monkeypatch.delenv('WORKSPACE_DEFAULT_MODE',raising=False)
    assert app_settings()['WORKSPACE_DEFAULT_MODE']=='fast'


def test_lost_response_resubmission_reuses_saved_policy_after_default_changes(client):
    chat=new_chat(client);url=f"/api/workspace/conversations/{chat['id']}/runs"
    body={'query':'Keep the same request','run_id':'wf_response_lost'}
    client.application.config['WORKSPACE_DEFAULT_MODE']='fast'
    assert client.post(url,json=body).status_code==202
    row=db.session.get(WorkspaceRun,body['run_id']);before=row.data_json;task=row.expected_task_id
    client.application.config['WORKSPACE_DEFAULT_MODE']='deep'
    repeated=client.post(url,json=body)
    assert repeated.status_code==202,repeated.json
    db.session.expire_all();row=db.session.get(WorkspaceRun,body['run_id'])
    assert row.data_json==before and row.expected_task_id==task
    assert WorkspaceRun.query.count()==1
    assert client.post(f"/api/workspace/runs/{body['run_id']}/cancel").status_code==200
    assert client.post(f"/api/workspace/runs/{body['run_id']}/retry").status_code==200
    db.session.expire_all()
    assert db.session.get(WorkspaceRun,body['run_id']).data_json==before


@pytest.mark.parametrize('selectors,expected',[
    ({'assistant_mode':'deep'},'deep'),
    ({'assistant_mode':'fast'},'fast'),
    ({'model':'gpt-6.1-sol'},'fast'),
    ({'selected_model':'gpt-6.1-sol'},'fast'),
    ({},'deep'),
])
def test_legacy_runs_retain_exact_policy_on_plain_resubmission_and_retry(client,selectors,expected):
    chat=new_chat(client);url=f"/api/workspace/conversations/{chat['id']}/runs"
    body={'query':'A saved conversation','run_id':'wf_legacy_policy'}
    assert client.post(url,json={**body,**selectors}).status_code==202
    row=db.session.get(WorkspaceRun,body['run_id'])
    data=json.loads(row.data_json)
    # Reproduce the pre-change row shape, including a historical mode-less task.
    for key in ('assistant_mode','model','selected_model'):data.pop(key,None)
    data.update(selectors);row.data_json=json.dumps(data);db.session.commit()
    before=row.data_json
    client.application.config['WORKSPACE_DEFAULT_MODE']='fast' if expected=='deep' else 'deep'
    assert model_policy.resolve_assistant_mode(data)==expected
    assert client.post(url,json=body).status_code==202
    assert client.post('/api/workspace/runs/wf_legacy_policy/cancel').status_code==200
    assert client.post('/api/workspace/runs/wf_legacy_policy/retry').status_code==200
    db.session.expire_all();saved=db.session.get(WorkspaceRun,body['run_id'])
    assert saved.data_json==before and model_policy.resolve_assistant_mode(json.loads(saved.data_json))==expected


def test_explicit_legacy_policy_change_is_still_rejected_for_an_existing_id(client):
    chat=new_chat(client);url=f"/api/workspace/conversations/{chat['id']}/runs"
    body={'query':'Do not change the policy','run_id':'wf_policy_conflict'}
    assert client.post(url,json={**body,'selected_model':'gpt-6.1-sol'}).status_code==202
    conflict=client.post(url,json={**body,'selected_model':'another-model'})
    assert conflict.status_code==400
    assert json.loads(db.session.get(WorkspaceRun,body['run_id']).data_json)['selected_model']=='gpt-6.1-sol'


def test_invalid_backend_default_fails_as_a_configuration_error(monkeypatch):
    monkeypatch.setenv('WORKSPACE_DEFAULT_MODE','invalid')
    with pytest.raises(ValueError,match='model configuration') as caught:app_settings()
    assert 'Choose' not in str(caught.value)


@pytest.mark.parametrize('value',['invalid',None,[]])
def test_invalid_legacy_modes_never_ask_users_to_choose_a_model(value):
    with pytest.raises(ValueError,match='model configuration') as caught:model_policy.resolve_assistant_mode({'assistant_mode':value})
    assert 'Choose' not in str(caught.value)
