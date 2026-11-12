"""Authenticated Workspace boundaries on a fresh, isolated SQLite database."""
from types import SimpleNamespace
import json
import pytest
from backend.app import create_app
from backend.database import db
from backend.models import User, WorkspaceRun
from backend.workspace import routes
from backend import workspace_runs, document_preparation_routes, agent_tasks
from backend.ai import agent_tools

@pytest.fixture
def client(tmp_path,monkeypatch):
 app=create_app(dict(TESTING=True,SQLALCHEMY_DATABASE_URI='sqlite:///'+str(tmp_path/'fresh.db'),
   DOCUMENT_PREPARATION_SOURCE_DIR=str(tmp_path/'sources'),DOCUMENT_EXPORT_STORAGE_DIR=str(tmp_path/'exports'),
   WORKSPACE_DOCUMENT_EXPORT_DIR=str(tmp_path/'exports')))
 for endpoint,fn in list(app.view_functions.items()):
  if endpoint.startswith(('workspace.','workspace_services.','document_preparation.')):app.view_functions[endpoint]=fn.__wrapped__
 for module in [routes,workspace_runs,document_preparation_routes]:monkeypatch.setattr(module,'current_user',SimpleNamespace(id=1))
 monkeypatch.setattr(agent_tasks.advance,'apply_async',lambda **kw:None)
 monkeypatch.setattr(__import__('celery').current_app.control,'revoke',lambda *a,**kw:None)
 with app.app_context():
  db.create_all();db.session.add_all([User(id=1,email='one@example.test'),User(id=2,email='two@example.test')]);db.session.commit()
  yield app.test_client()
  db.session.remove()

def new_chat(client):
 p=client.post('/api/workspace/projects',json={'name':'Research'});assert p.status_code==201
 c=client.post(f"/api/workspace/projects/{p.json['id']}/conversations",json={'name':'Review'})
 assert c.status_code==201
 return c.json

def test_fresh_identity_edit_conflict_and_download(client):
 chat=new_chat(client);url='/api/workspace/conversations/'+chat['id'];state=client.get(url)
 assert state.status_code==200
 value=state.json;assert value['document_id']!=chat['id'];assert value['history']==[]
 doc='/api/workspace/documents/'+value['document_id']
 draft={'title':'Notes','format_version':2,'source_format':'markdown','markdown':'# Notes\n\n| Item | Value |\n|---|---|\n| A | 2 |'}
 data={'draft':draft,'expected_version':value['document_version']}
 saved=client.patch(doc,json=data);assert saved.status_code==200
 assert saved.json['document_version']==value['document_version']+1
 assert client.patch(doc,json=data).status_code==409
 export=client.post(doc+'/download',json={'format':'markdown'});assert export.status_code==200
 assert b'| A | 2 |' in export.data
 assert client.get(url).json['document']['markdown']==draft['markdown']

def test_run_reload_cancel_retry_and_ownership(client,monkeypatch):
 chat=new_chat(client);url='/api/workspace/conversations/'+chat['id']
 sent=client.post(url+'/runs',json={'query':'Summarize the assignment','run_id':'wf_api','assistant_mode':'fast'})
 assert sent.status_code==202,sent.json
 assert client.post(url+'/runs',json={'query':'Summarize the assignment','run_id':'wf_api','assistant_mode':'fast'}).json['run_id']=='wf_api'
 restored=client.get(url).json;assert restored['active_run']['run_id']=='wf_api';assert len(restored['history'])==1
 assert client.post('/api/workspace/runs/wf_api/cancel').json['cancelled']
 assert client.get(url).json['history'][0]['status']=='cancelled'
 assert client.post('/api/workspace/runs/wf_api/retry').status_code==200
 monkeypatch.setattr(routes,'current_user',SimpleNamespace(id=2));monkeypatch.setattr(workspace_runs,'current_user',SimpleNamespace(id=2))
 assert client.get(url).status_code==404
 assert client.get('/api/workspace/runs/wf_api').status_code==404
 assert client.get('/api/workspace/documents/'+restored['document_id']).status_code==404

def test_optional_legal_and_removed_products(client):
 from flask import current_app
 current_app.config['WORKSPACE_LEGAL_ENABLED']=False
 names={t['name'] for t in agent_tools.definitions()}
 assert 'lookup_authorities' not in names and 'web_search' in names and 'create_draft' in names
 assert 'Legal research is optional' not in agent_tasks.system_instructions()
 for path in ['/api/everything','/api/smart-search','/api/hallucination-guard','/api/word','/CaseViewer/example']:
  assert client.get(path).status_code==404
 assert not {'case_assistant_work','smart_search_sessions','stepAnalytics'} & set(db.metadata.tables)
 assert 'execution_version' not in WorkspaceRun.__table__.columns

def test_auth_is_required_without_test_boundary_override():
 app=create_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':'sqlite:///:memory:'})
 assert app.test_client().get('/api/workspace/projects').status_code==401


def test_original_source_ranges_are_bounded_and_owned(client,monkeypatch):
    from backend.models import DocumentPreparation,UploadedFiles,ChatThread
    chat=new_chat(client)
    group=client.post('/api/workspace/upload-groups',json={'scope':'chat','scopeId':chat['id'],
        'selectionId':'selection-api','documents':[{'client_upload_id':'source-api','filename':'source.txt','file_size':6}]})
    assert group.status_code==201
    source_id=group.json['documents'][0]['document_id']
    row=db.session.get(DocumentPreparation,source_id)
    file=UploadedFiles(unique_id=source_id,original_filename='source.txt',file_size=6,content='abcdef',
        project_id=chat['project_id'],chat_thread_id=ChatThread.resolve_id(chat['id']))
    db.session.add(file);db.session.flush();row.uploaded_file_id=file.id;row.status='ready';db.session.commit()
    url='/api/workspace/sources/'+source_id+'/content'
    view=client.get(url+'?start=2&limit=3');assert view.status_code==200
    assert view.json['text']=='cde' and view.json['total_characters']==6
    assert client.get(url+'?limit=50001').status_code==400
    monkeypatch.setattr(routes,'current_user',SimpleNamespace(id=2))
    assert client.get(url).status_code==404


def test_removed_word_addin_routes_are_absent():
 from backend.app import create_app
 app=create_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':'sqlite:///:memory:'})
 assert not any('word' in rule.rule.lower() or 'addin' in rule.rule.lower() for rule in app.url_map.iter_rules())
 assert app.test_client().get('/api/word-addin/tasks').status_code==404
