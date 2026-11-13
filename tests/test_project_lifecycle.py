"""Project deletion is serialized against admissions and never cancels work."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import event, text
from backend.database import db
from backend.models import Project, WorkspaceRun, DocumentPreparation, UploadGroup
from tests.test_workspace_api import client, new_chat


def manifest(client, project):
    return client.post('/api/workspace/upload-groups', json={
        'scope':'matter','scopeId':project,'selectionId':'selection-lifecycle',
        'documents':[{'client_upload_id':'file-one','filename':'notes.txt','file_size':5}]})


def test_duplicate_project_names_have_specific_conflicts(client):
    first=client.post('/api/workspace/projects',json={'name':'Research'}).json
    second=client.post('/api/workspace/projects',json={'name':'Notes'}).json
    for response in (client.post('/api/workspace/projects',json={'name':'Research'}),
                     client.patch(f"/api/workspace/projects/{second['id']}",json={'name':'Research'})):
        assert response.status_code==409 and response.json['code']=='project_name_conflict'
    assert client.get(f"/api/workspace/projects/{second['id']}").json['name']=='Notes'
    assert client.patch(f"/api/workspace/projects/{first['id']}",json={'name':'Research'}).status_code==200


@pytest.mark.parametrize('status',['running','awaiting_user_confirmation'])
def test_busy_project_keeps_agent_and_preparations_intact(client,status):
    chat=new_chat(client); project=chat['project_id']
    response=client.post(f"/api/workspace/conversations/{chat['id']}/runs",json={'query':'Review','run_id':'wf_busy'})
    assert response.status_code==202
    row=db.session.get(WorkspaceRun,'wf_busy');row.status=status;db.session.commit()
    group=manifest(client,project);assert group.status_code==201
    response=client.delete(f'/api/workspace/projects/{project}')
    assert response.status_code==409
    assert response.json['code']=='project_busy' and response.json['active_runs']==1 and response.json['active_preparations']==1
    db.session.expire_all()
    assert db.session.get(WorkspaceRun,'wf_busy').status==status
    assert db.session.get(Project,project) is not None
    assert client.post('/api/workspace/runs/wf_busy/cancel').status_code==200
    assert client.delete(f'/api/workspace/projects/{project}').json['active_preparations']==1
    source=group.json['documents'][0]['document_id']
    assert client.delete(f'/api/workspace/sources/{source}').status_code==200
    assert client.delete(f'/api/workspace/projects/{project}').status_code==200
    assert db.session.get(Project,project) is None
    assert db.session.query(WorkspaceRun).count()==0
    assert db.session.query(DocumentPreparation).count()==0


@pytest.mark.parametrize('admission',['run','retry','upload','preparation_retry'])
@pytest.mark.parametrize('delete_first',[True,False])
def test_project_delete_and_admission_are_serialized(client,admission,delete_first):
    chat=new_chat(client); project=chat['project_id']; app=client.application
    if admission=='retry':
        result=client.post(f"/api/workspace/conversations/{chat['id']}/runs",json={'query':'Review','run_id':'wf_race'})
        assert result.status_code==202
        assert client.post('/api/workspace/runs/wf_race/cancel').status_code==200
    source=None
    if admission=='preparation_retry':
        group=manifest(client,project);assert group.status_code==201
        source=group.json['documents'][0]['document_id']
        doc=db.session.get(DocumentPreparation,source);doc.status='failed';db.session.commit()
    locked=threading.Event(); release=threading.Event(); entered=threading.Event(); role=threading.local()
    engine=db.engine
    def after_write(conn,cursor,statement,parameters,context,many):
        if getattr(role,'winner',False) and not locked.is_set() and statement.startswith('UPDATE projects SET id='):
            locked.set()
            assert release.wait(5),'Release the project lock without hanging the test'
    event.listen(engine,'after_cursor_execute',after_write)
    def request(delete,winner):
        role.winner=winner
        with app.app_context():
            other=app.test_client()
            if not winner:entered.set()
            if delete:return other.delete(f'/api/workspace/projects/{project}')
            if admission=='run':return other.post(f"/api/workspace/conversations/{chat['id']}/runs",json={'query':'Review','run_id':'wf_race'})
            if admission=='retry':return other.post('/api/workspace/runs/wf_race/retry')
            if admission=='preparation_retry':return other.post(f'/api/workspace/sources/{source}/retry',json={})
            return manifest(other,project)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner=pool.submit(request,delete_first,True)
            assert locked.wait(5),'The first operation acquired a project write lock'
            loser=pool.submit(request,not delete_first,False)
            assert entered.wait(5)
            release.set()
            first,second=winner.result(timeout=10),loser.result(timeout=10)
    finally:
        release.set();event.remove(engine,'after_cursor_execute',after_write)
    db.session.expire_all()
    if delete_first:
        assert first.status_code==200,first.json
        assert second.status_code==404,second.json
        assert db.session.get(Project,project) is None
        assert db.session.query(WorkspaceRun).count()==0
        assert db.session.query(UploadGroup).count()==0
    else:
        assert first.status_code in {200,201,202},first.json
        assert second.status_code==409 and second.json['code']=='project_busy',second.json
        assert db.session.get(Project,project) is not None
    assert db.session.execute(text('PRAGMA foreign_key_check')).fetchall()==[]
