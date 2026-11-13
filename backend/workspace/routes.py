"""Authenticated project, conversation, document and run resources."""
import json
import uuid
from pathlib import Path
from flask import Blueprint, request, send_file, current_app
from sqlalchemy.exc import IntegrityError
from backend.auth import auth_required, current_user
from backend.database import db
from backend.models import Project, ChatThread, DraftDocument, WorkspaceRun, AgentArtifact, DocumentPreparation, UploadedFiles, UploadGroup
from backend.util import workspace_store, workspace_service as runs, document_preparation as preparation
from backend.util.account_access import require_enabled_account
from backend.workspace_runs import guarded
from backend.workspace.project_access import lock_project
bp = Blueprint('workspace', __name__, url_prefix='/api/workspace')

def owned_project(project_id):
    value = Project.query.filter_by(id=project_id, userId=current_user.id).first()
    if value is None: raise runs.WorkflowNotFound('Project not found')
    require_enabled_account(current_user.id)
    return value

def owned_chat(conversation_id):
    try: chat_id = ChatThread.resolve_id(conversation_id)
    except (ValueError, TypeError): raise runs.WorkflowNotFound('Conversation not found')
    value = db.session.get(ChatThread, chat_id)
    if value is None: raise runs.WorkflowNotFound('Conversation not found')
    owned_project(value.projectId)
    return value

def owned_authored(document_id):
    value = DraftDocument.query.filter_by(public_id=document_id).first()
    if value is None: raise runs.WorkflowNotFound('Document not found')
    return owned_chat(value.chat_thread_id), value

def project_view(p): return {'id':p.id,'name':p.name}
def conversation_view(c): return {'id':c.public_id,'project_id':c.projectId,'name':c.display_name}
def name_input(default=None):
    value = (request.get_json(silent=True) or {}).get('name', default)
    if not isinstance(value,str) or not value.strip() or len(value.strip()) > 250: raise ValueError('Enter a name of 1 to 250 characters')
    return value.strip()

def commit_project_name():
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return {'code':'project_name_conflict','msg':'You already have a project with this name.'},409

@bp.route('/projects', methods=['GET','POST'])
@auth_required
@guarded
def projects():
    if request.method=='GET': return {'projects':[project_view(p) for p in Project.query.filter_by(userId=current_user.id).order_by(Project.id).all()]}
    require_enabled_account(current_user.id)
    value = Project(name=name_input(), userId=current_user.id)
    db.session.add(value)
    conflict=commit_project_name()
    if conflict:return conflict
    return project_view(value),201

@bp.route('/projects/<int:project_id>',methods=['GET','PATCH','DELETE'])
@auth_required
@guarded
def project(project_id):
    value=owned_project(project_id)
    if request.method=='DELETE':
        value=lock_project(project_id,current_user.id)
        active_runs=WorkspaceRun.query.join(ChatThread,WorkspaceRun.chat_thread_id==ChatThread.id).filter(
            ChatThread.projectId==project_id,WorkspaceRun.active_chat_thread_id.isnot(None)).count()
        active_preparations=DocumentPreparation.query.join(UploadGroup,DocumentPreparation.group_id==UploadGroup.id).filter(
            UploadGroup.project_id==project_id,DocumentPreparation.status.in_(preparation.ACTIVE)).count()
        if active_runs or active_preparations:
            db.session.rollback()
            return {'code':'project_busy','msg':'Finish or cancel active requests and document preparation before deleting this project.',
                'active_runs':active_runs,'active_preparations':active_preparations},409
        db.session.delete(value);db.session.commit();return {'deleted':True}
    if request.method=='PATCH':
        value.name=name_input()
        conflict=commit_project_name()
        if conflict:return conflict
    return project_view(value)

@bp.route('/projects/<int:project_id>/conversations', methods=['GET','POST'])
@auth_required
@guarded
def conversations(project_id):
    owned_project(project_id)
    if request.method=='GET':return {'conversations':[conversation_view(c) for c in ChatThread.query.filter_by(projectId=project_id).order_by(ChatThread.date.desc(),ChatThread.id.desc()).all()]}
    lock_project(project_id,current_user.id)
    value=ChatThread(display_name=name_input('New conversation'),projectId=project_id)
    db.session.add(value);db.session.flush();workspace_store.initialize(value.id);db.session.commit()
    return conversation_view(value),201

@bp.route('/conversations/<conversation_id>',methods=['GET','PATCH','DELETE'])
@auth_required
@guarded
def conversation(conversation_id):
    value=owned_chat(conversation_id)
    if request.method in {'PATCH','DELETE'}:
        lock_project(value.projectId,current_user.id)
        value=owned_chat(conversation_id)
    if request.method=='DELETE':
        active=WorkspaceRun.query.filter_by(active_chat_thread_id=value.id).first()
        if active:runs.cancel(active)
        db.session.delete(value);db.session.commit();return {'deleted':True}
    if request.method=='PATCH':value.display_name=name_input();db.session.commit()
    state=workspace_store.load(value.id)
    document=workspace_store.document_for_chat(value.id)
    stored_turns=workspace_store.turns(value.id)
    history=[{'id':t.id,'run_id':t.run_id,'query':t.user_text,'response':t.assistant_text,'metadata':json.loads(t.metadata_json or '{}'),'created_at':t.created_at.isoformat()} for t in stored_turns]
    known={t.run_id for t in stored_turns if t.run_id}
    saved_runs=WorkspaceRun.query.filter_by(chat_thread_id=value.id).order_by(WorkspaceRun.created_at,WorkspaceRun.id).all()
    overlays=[]
    for run in saved_runs:
        if run.id in known:continue
        original=json.loads(run.data_json)
        result=json.loads(run.result_json) if run.result_json else {}
        overlays.append({'id':run.id,'run_id':run.id,'query':original.get('display_user_message') or original['query'],
            'response':result.get('response') or run.error_message or '', 'status':run.status,
            'created_at':run.created_at.isoformat(), 'metadata':{'prompt_attachments':original.get('promptAttachments',[])}})
    history=sorted(history+overlays,key=lambda t:(t['created_at'],t['id']))
    active=next((r for r in saved_runs if r.active_chat_thread_id==value.id),None)
    result={**conversation_view(value),'document_id':document.public_id,'document':state['draft'],
        'document_version':document.draft_version,'history':history,'active_run':runs.status(active) if active else None}
    db.session.commit()
    return result

@bp.post('/conversations/<conversation_id>/runs')
@auth_required
@guarded
def submit(conversation_id):
    chat=owned_chat(conversation_id)
    data=dict(request.get_json(silent=True) or {})
    data['conversation_id']=data['chat_thread_id']=chat.public_id
    data['user_id']=current_user.id
    prior=db.session.get(WorkspaceRun,data.get('run_id')) if data.get('run_id') else None
    data['document_snapshot']=preparation.build_snapshot(current_user.id,chat.public_id,data)
    db.session.commit()
    row=runs.submit(current_user.id,data)
    if row.dispatch_pending:
        from backend.agent_tasks import dispatch
        dispatch(row)
    preparation.mark_snapshot_committed(data['document_snapshot'])
    from backend.util.task_analytics import increment_task_analytics
    if prior is None:increment_task_analytics(chat.id,{'promptsSent':1},user_id=current_user.id)
    return runs.status(row),202

@bp.get('/conversations/<conversation_id>/runs/active')
@auth_required
@guarded
def active_run(conversation_id):
    chat=owned_chat(conversation_id)
    row=WorkspaceRun.query.filter_by(active_chat_thread_id=chat.id,user_id=current_user.id).first()
    return {'run':runs.status(row) if row else None}

@bp.route('/documents/<document_id>',methods=['GET','PATCH'])
@auth_required
@guarded
def document(document_id):
    chat,value=owned_authored(document_id)
    if request.method=='PATCH':
        data=request.get_json(silent=True) or {}
        if type(data.get('expected_version')) is not int or not isinstance(data.get('draft'),dict):raise ValueError('Supply a document and its expected version')
        try:
            workspace_store.update_document(chat.id,value.public_id,data['draft'],expected_version=data['expected_version'])
            db.session.commit()
        except workspace_store.WorkspaceConflict as exc:
            db.session.rollback()
            return {'code':'document_conflict','document_version':exc.current_version,'msg':'The document changed elsewhere. Your edits have been kept.'},409
    return {'document_id':value.public_id,'conversation_id':chat.public_id,'document':json.loads(value.draft_json),'document_version':value.draft_version}

@bp.post('/documents/<document_id>/revisions/<revision_id>/download')
@auth_required
@guarded
def download_revision(document_id,revision_id):
    from backend.util.document_downloads import download_revision as export, DocumentDownloadError
    chat,value=owned_authored(document_id);data=request.get_json(silent=True) or {}
    if data.get('preset')=='court' and not current_app.config['WORKSPACE_LEGAL_ENABLED']:raise ValueError('Court formatting is disabled')
    try:path,metadata,_=export(user_id=current_user.id,draft_id=chat.id,revision_id=revision_id,format=data.get('format','docx'),preset=data.get('preset','general'),current_draft=json.loads(value.draft_json))
    except DocumentDownloadError as exc:return {'msg':str(exc)},exc.status
    return send_file(path,as_attachment=True,download_name=path.name,conditional=True)

@bp.post('/documents/<document_id>/download')
@auth_required
@guarded
def download_current(document_id):
    from backend.util.document_exports import export_document, DocumentExportError
    chat,value=owned_authored(document_id);data=request.get_json(silent=True) or {};draft=json.loads(value.draft_json)
    if not draft.get('markdown') and not draft.get('sections'):raise ValueError('The document is empty')
    format=data.get('format','docx');preset=data.get('preset','general')
    if preset=='court' and not current_app.config['WORKSPACE_LEGAL_ENABLED']:raise ValueError('Court formatting is disabled')
    root=Path(current_app.config['WORKSPACE_DOCUMENT_EXPORT_DIR'])/'manual'/value.public_id/str(value.draft_version)
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    extension={'markdown':'md','html':'html','docx':'docx','pdf':'pdf'}.get(format)
    if extension is None:raise ValueError('Unsupported download format')
    path=root/(uuid.uuid4().hex+'.'+extension)
    db.session.commit()
    try:export_document(draft,format,path,preset=preset)
    except (DocumentExportError,ValueError) as exc:return {'msg':str(exc)},400
    return send_file(path,as_attachment=True,download_name='Document.'+extension)

@bp.get('/sources/<source_id>/content')
@auth_required
@guarded
def source_content(source_id):
    row=db.session.get(DocumentPreparation,source_id)
    if row is None:raise runs.WorkflowNotFound('Source not found')
    preparation.owned_group(row.group_id,current_user.id)
    if row.status=='cancelled':raise runs.WorkflowNotFound('Source not found')
    value=db.session.get(UploadedFiles,row.uploaded_file_id)
    text=(value.content or '') if value else ''
    start=int(request.args.get('start',0));limit=int(request.args.get('limit',20000))
    if start<0 or not 1<=limit<=50000:raise ValueError('Invalid source range')
    end=min(len(text),start+limit)
    return {'id':source_id,'filename':row.filename,'text':text[start:end], 'status':row.status,
            'start':start,'end':end,'total_characters':len(text)}
