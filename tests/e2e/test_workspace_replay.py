"""Real Redis/Celery/SQL replay; only identity and paid provider boundaries are mocked."""
import io
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid

import pytest
import redis
from celery.contrib.testing.worker import start_worker
from backend.app import create_app
from backend.config import app_settings
from backend.database import db
from backend.models import User, WorkspaceRun, AgentArtifact, AgentStep, ChatThread
from backend import agent_tasks, workspace_runs, document_preparation_routes
from backend.workspace import routes, state
from backend.ai import base
from backend.util import document_drafts, preparation_summary
from tests.test_document_preparation import prepared_provider

pytestmark=pytest.mark.skipif(os.getenv('ZYX_REPLAY_E2E')!='1',reason='Set ZYX_REPLAY_E2E=1 for an owned local Redis/Celery replay.')

@pytest.fixture(scope='module')
def owned_redis(tmp_path_factory):
    executable=shutil.which('redis-server')
    if not executable:pytest.fail('redis-server is required for the requested replay suite')
    root=tmp_path_factory.mktemp('replay-redis')
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    with (root/'redis.log').open('w') as log:
        process=subprocess.Popen([executable,'--bind','127.0.0.1','--port',str(port),'--save','','--appendonly','no','--dir',str(root)],stdout=log,stderr=subprocess.STDOUT)
        client=redis.Redis(host='127.0.0.1',port=port,socket_connect_timeout=1)
        try:
            deadline=time.monotonic()+5
            while True:
                try:
                    if client.ping():break
                except redis.ConnectionError:
                    if time.monotonic()>deadline:pytest.fail('The owned replay Redis did not start')
                    time.sleep(.02)
            yield f'redis://127.0.0.1:{port}/0'
        finally:
            client.close();process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)

class Replay:
    def __init__(self,app,client):
        self.app,self.client=app,client
        self.actions=[];self.calls=0;self.events=[];self.fail_once=False;self.timings={}
    def decisions(self,request):
        if self.fail_once:
            self.fail_once=False
            raise RuntimeError('Injected replay provider interruption')
        action=self.actions[self.calls];self.calls+=1
        if callable(action):action=action(request)
        name,args=action
        return {'status':'completed','output':[{'type':'function_call','call_id':f'call_{self.calls}','name':name,'arguments':json.dumps(args)}]}
    def create_chat(self):
        p=self.client.post('/api/workspace/projects',json={'name':'Replay Project'});assert p.status_code==201
        c=self.client.post(f"/api/workspace/projects/{p.json['id']}/conversations",json={'name':'Replay Conversation'});assert c.status_code==201
        self.chat=c.json['id'];self.url=f'/api/workspace/conversations/{self.chat}'
    def submit(self,query='Replay assignment',**extra):
        value=self.client.post(self.url+'/runs',json={'query':query,'assistant_mode':'fast',**extra})
        assert value.status_code==202,value.json
        self.run_id=value.json['run_id'];return value.json
    def progress(self):
        value=self.client.get('/api/workspace/runs/'+self.run_id);assert value.status_code==200,value.json;return value.json
    def wait(self,status,timeout=15):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            value=self.progress()
            if value['status']==status:return value
            if value['status'] in {'completed','failed','cancelled'} and value['status']!=status:pytest.fail(f"Expected {status}: {value}")
            time.sleep(.02)
        pytest.fail(f'Timed out waiting for {status}: {self.progress()}')
    def history(self):return self.client.get(self.url).json
    def gate(self,name,args):
        entered,released=threading.Event(),threading.Event();self.events.append(released)
        def blocked(request):
            entered.set()
            if not released.wait(10):raise RuntimeError('Replay gate was not released')
            return name,args
        return blocked,entered,released

@pytest.fixture
def replay(tmp_path,monkeypatch,owned_redis,request):
    queue='zyx-agentic-llms.e2e.replay.'+uuid.uuid4().hex
    celery_config={**app_settings()['CELERY'],'broker_url':owned_redis,'result_backend':owned_redis,
        'task_default_queue':queue,'task_default_exchange':queue,'task_default_routing_key':queue,
        'broker_transport_options':{'global_keyprefix':queue+':'},'result_backend_transport_options':{'global_keyprefix':queue+':'},
        'worker_prefetch_multiplier':1,'worker_hijack_root_logger':False,'broker_heartbeat':0}
    app=create_app(dict(TESTING=True,SQLALCHEMY_DATABASE_URI='sqlite:///'+str(tmp_path/'replay.db'),
        DOCUMENT_PREPARATION_SOURCE_DIR=str(tmp_path/'sources'),AGENT_PYTHON_STORAGE_DIR=str(tmp_path/'python'),
        DOCUMENT_EXPORT_STORAGE_DIR=str(tmp_path/'exports'),WORKSPACE_DOCUMENT_EXPORT_DIR=str(tmp_path/'exports'),
        CELERY=celery_config,E2E_CELERY_QUEUE=queue,E2E_REDIS_URL=owned_redis))
    for endpoint,fn in list(app.view_functions.items()):
        if endpoint.startswith(('workspace.','workspace_services.','document_preparation.')):app.view_functions[endpoint]=fn.__wrapped__
    for module in [routes,workspace_runs,document_preparation_routes]:monkeypatch.setattr(module,'current_user',SimpleNamespace(id=1))
    with app.app_context():
        db.create_all();db.session.add(User(id=1,email='replay@example.test'));db.session.commit()
    value=Replay(app,app.test_client())
    monkeypatch.setattr(base,'responses_completion',value.decisions)
    monkeypatch.setattr(base,'get_openai_client',lambda:pytest.fail('A replay attempted a paid provider call'))
    monkeypatch.setattr(base,'chat_completion_parse',lambda prompt,schema,**kw:schema(decision='not_forbidden',reason='Replay assignment permits editing.',supporting_wording=''))
    monkeypatch.setattr(preparation_summary,'_call',prepared_provider)
    monkeypatch.setattr(document_drafts,'resolve_court_settings',lambda *a,**kw:{'structural':{},'layout':{}})
    monkeypatch.setattr(state,'_enqueue_brief_update',lambda *a,**kw:None)
    celery=app.extensions['celery'];celery.loader.import_default_modules()
    # Celery configures process logging. Restore the caller's level so later tests
    # and applications do not silently lose warning diagnostics.
    root_level=logging.getLogger().level
    try:
        with start_worker(celery,pool='solo',concurrency=1,loglevel='WARNING',perform_ping_check=False,queues=[queue,queue+'.documents'],shutdown_timeout=12):
            try:value.create_chat();yield value
            finally:
                for event in value.events:event.set()
    finally:
        logging.getLogger().setLevel(root_level)
        with app.app_context():db.session.remove();db.engine.dispose()
        report=Path(__file__).resolve().parents[2]/'artifacts/workspace-replay/worker-metrics'
        report.mkdir(parents=True,exist_ok=True)
        (report/(hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]+'.json')).write_text(json.dumps({'test':request.node.nodeid,'controller_calls':value.calls,**value.timings},indent=2))

def outputs(request):
    return [json.loads(item['output']) for item in request['input'] if item.get('type')=='function_call_output']
def complete(response='Replay completed',candidate=None):return ('complete_task',{'response':response,'candidate_id':candidate})

def test_real_queue_conversation_completion_reload_and_idempotency(replay):
    replay.actions=[('list_documents',{}),complete()]
    replay.submit(run_id='wf_redis_replay')
    replay.wait('completed')
    assert replay.history()['history'][0]['response']=='Replay completed'
    assert len(replay.history()['history'])==1
    replay.submit(run_id='wf_redis_replay')
    assert replay.wait('completed')['successful']
    assert replay.calls==2 and len(replay.history()['history'])==1

def test_real_queue_clarification_reload_and_resume(replay):
    replay.actions=[('request_user_input',{'question':'Which audience?','options':['Internal']}),
        lambda req:complete('Audience: '+outputs(req)[-1]['answer'])]
    replay.submit();replay.wait('awaiting_user_confirmation')
    assert replay.history()['active_run']['value']['confirmation']['message']=='Which audience?'
    assert replay.client.post('/api/workspace/runs/'+replay.run_id+'/confirm',json={'decision':{'answer':'Internal team'}}).status_code==200
    replay.wait('completed');assert replay.history()['history'][0]['response']=='Audience: Internal team'

@pytest.mark.parametrize('phase',['inflight','question'])
def test_real_queue_cancel_reload_retry_and_late_result_fencing(replay,phase):
    if phase=='inflight':
        blocked,entered,released=replay.gate(*complete('Late completion must not publish'))
        replay.actions=[blocked,complete('Retried assignment completed')]
        replay.submit();assert entered.wait(5)
    else:
        replay.actions=[('request_user_input',{'question':'Choose scope','options':[]}),complete('Retried assignment completed')]
        replay.submit();replay.wait('awaiting_user_confirmation')
    start=time.perf_counter()
    response=replay.client.post('/api/workspace/runs/'+replay.run_id+'/cancel');assert response.json['cancelled']
    replay.timings['cancel_seconds']=time.perf_counter()-start
    assert replay.timings['cancel_seconds']<2, 'Cancellation waited for the mocked provider'
    if phase=='inflight':released.set()
    assert replay.history()['active_run'] is None
    assert replay.history()['history'][0]['status']=='cancelled'
    time.sleep(.1)
    assert replay.progress()['status']=='cancelled'
    with replay.app.app_context():
        from backend.models import ConversationTurn
        assert ConversationTurn.query.count()==0
    assert replay.client.post('/api/workspace/runs/'+replay.run_id+'/retry').status_code==200
    if phase=='question':
        replay.wait('awaiting_user_confirmation')
        replay.client.post('/api/workspace/runs/'+replay.run_id+'/confirm',json={'decision':{'answer':'All sources'}})
    replay.wait('completed')
    assert len(replay.history()['history'])==1

def test_real_queue_provider_failure_explicit_retry_reuses_completed_steps(replay):
    replay.actions=[('list_documents',{}),complete()]
    original=replay.decisions
    def failure_after_observation(req):
        if replay.calls==1 and not getattr(replay,'injected',False):replay.injected=True;raise RuntimeError('Injected provider failure')
        return original(req)
    from unittest.mock import patch
    with patch.object(base,'responses_completion',failure_after_observation):
        replay.submit();replay.wait('failed')
        with replay.app.app_context():
            assert AgentStep.query.filter_by(name='list_documents',state='ready').count()==1
        replay.client.post('/api/workspace/runs/'+replay.run_id+'/retry')
        replay.wait('completed')
        with replay.app.app_context():assert AgentStep.query.filter_by(name='list_documents',state='ready').count()==1
        assert replay.calls==2

def test_real_queue_upload_read_draft_verify_publish_and_immutable_download(replay,monkeypatch):
    raw=b'Order total is 240 dollars. Payment is due only after acceptance.'
    group=replay.client.post('/api/workspace/upload-groups',json={'scope':'chat','scopeId':replay.chat,'selectionId':'replay-upload',
        'documents':[{'client_upload_id':'source','filename':'order.txt','file_size':len(raw)}]});assert group.status_code==201
    doc=group.json['documents'][0];source_id=doc['document_id']
    uploaded=replay.client.post('/api/workspace/upload-groups/'+group.json['group_id']+'/files',data={'clientUploadIds':json.dumps([doc['clientUploadId']]),'files':(io.BytesIO(raw),'order.txt')},content_type='multipart/form-data');assert uploaded.status_code==202
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        catalog=replay.client.get('/api/workspace/document-preparation',query_string={'conversation_id':replay.chat}).json
        if catalog['documents'][0]['status']=='ready':break
        time.sleep(.02)
    assert catalog['documents'][0]['status']=='ready',catalog
    def writer(**kwargs):
        assert '240' in json.dumps(kwargs['materials'])
        return document_drafts.MarkdownDraftResult(title='Order report',markdown='# Order report\n\nTotal: $240. Payment requires acceptance.',update_court_format=False)
    monkeypatch.setattr(document_drafts,'write_document',writer)
    monkeypatch.setattr(base,'chat_completion',lambda *a,**kw:'The report preserves the payment condition.')
    replay.actions=[('read_sources',{'artifact_id':None,'ranges':[{'file_id':source_id,'start':0,'end':len(raw)}],'start':0,'end':None}),
        lambda req:('create_draft',{'instructions':'Preserve the payment condition','material_ids':[outputs(req)[-1]['artifact_id']]}),
        lambda req:('verify_draft',{'instructions':'Check the report','material_ids':[outputs(req)[0]['artifact_id']],'target':outputs(req)[-1]['artifact_id'],'check_citations':False}),
        lambda req:complete('Report ready',next(v['artifact_id'] for v in outputs(req) if v.get('kind')=='draft'))]
    replay.submit('Draft the order report',promptAttachments=[{'file_id':source_id}]);replay.wait('completed')
    restored=replay.history();assert '$240' in restored['document']['markdown']
    with replay.app.app_context():
        assert AgentArtifact.query.filter_by(kind='verification').count()==1
    revision=restored['document']['revision_id']
    download=replay.client.post('/api/workspace/documents/'+restored['document_id']+'/revisions/'+revision+'/download',json={'format':'docx'})
    assert download.status_code==200 and download.data.startswith(b'PK')
    download.close()

def test_real_queue_web_research_is_saved_without_a_legal_record(replay,monkeypatch):
    monkeypatch.setattr(base,'web_search_completion',lambda *a,**kw:('Replay synthesis',{'sources':[{'url':'https://example.org/replay','title':'Replay source'}]}))
    replay.actions=[('web_search',{'query':'Current document work practices'}),complete('Research saved')]
    replay.submit('Research general document practices');replay.wait('completed')
    with replay.app.app_context():
        assert AgentArtifact.query.filter_by(kind='web_search').count()==1
        assert AgentArtifact.query.filter_by(kind='authority_research').count()==0


def test_real_queue_optional_legal_research_snapshot_is_saved(replay,monkeypatch):
    from backend.domains.legal import authority_research,authority_sources
    monkeypatch.setattr(authority_research,'_discover_mentions',lambda *a,**kw:[])
    monkeypatch.setattr(authority_sources,'lookup_citations',lambda *a,**kw:([],None))
    replay.actions=[('lookup_authorities',{'targets':[{'file_id':None,'draft_id':None,
        'text':'Research the supplied legal question','start':None,'end':None}],
        'mode':'overview','purpose':'Identify applicable sources; disclose uncertainty','research_id':None}),
        complete('Legal research saved with unresolved coverage disclosed')]
    replay.submit('Review legal authority coverage');replay.wait('completed')
    with replay.app.app_context():
        step=AgentStep.query.filter_by(name='lookup_authorities',state='ready').one()
        result=json.loads(step.result_json)
        record=db.session.get(AgentArtifact,result['research_id'])
        assert record.kind=='authority_research'
        assert json.loads(record.content_json)['purpose']=='Identify applicable sources; disclose uncertainty'
