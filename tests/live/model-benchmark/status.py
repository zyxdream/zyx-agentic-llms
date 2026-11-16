"""Read only the explicitly created benchmark projects; never read account/session rows."""
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter
import json,sqlite3,sys

ROOT=Path(__file__).resolve().parents[3]
def snapshot(run):
    state=json.loads((run/'state.json').read_text())
    c=sqlite3.connect((ROOT/'instance/zyx-agentic-llms.db').as_uri()+'?mode=ro',uri=True)
    c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
    out={'at':datetime.now(timezone.utc).isoformat(),'cases':{},'runs':{}}
    for key,case in state['cases'].items():
        project=case.get('project_id')
        if not project:continue
        chat=c.execute('SELECT id,public_id FROM chat_threads WHERE projectId=? AND public_id=?',(project,case.get('chat_id'))).fetchone()
        preps=[dict(r) for r in c.execute('''SELECT p.id,p.filename,p.status,p.scope_key,p.error_code,p.error_message,p.created_at,p.ready_at,p.attempts
            FROM document_preparations p JOIN document_upload_groups g ON g.id=p.group_id WHERE g.project_id=?''',(project,))]
        item={'project_id':project,'preparations':preps,'usage':{}}
        if chat:
            item['chat_db_id']=chat['id']
            u=c.execute('SELECT llmCallCount,totalTokens,downloadLlmCallCount,downloadTokens,downloadAttempts,downloadSuccesses FROM workspace_usage WHERE chat_thread_id=?',(chat['id'],)).fetchone()
            item['usage']=dict(u) if u else {}
            d=c.execute('SELECT draft_version FROM draft_documents WHERE chat_thread_id=?',(chat['id'],)).fetchone()
            item['draft_version']=d['draft_version'] if d else None
            for r in c.execute('SELECT id,status,phase,attempts,created_at,updated_at,error_message FROM workspace_runs WHERE chat_thread_id=? ORDER BY created_at',(chat['id'],)):
                value=dict(r)
                steps=[dict(s) for s in c.execute('SELECT sequence,name,kind,state,result_json FROM agent_steps WHERE run_id=? ORDER BY sequence',(r['id'],))]
                value['tool_counts']=dict(Counter(s['name'] for s in steps if s['kind']=='tool'))
                value['last_step']={k:v for k,v in steps[-1].items() if k!='result_json'} if steps else None
                value['tool_errors']=[]
                for step in steps:
                    result=json.loads(step['result_json'] or '{}')
                    if step['kind']=='tool' and isinstance(result,dict) and result.get('error'):
                        value['tool_errors'].append({'sequence':step['sequence'],'name':step['name'],'message':result.get('message') or result.get('error')})
                out['runs'][r['id']]=value
        out['cases'][key]=item
    c.close();return out

if __name__=='__main__':print(json.dumps(snapshot(Path(sys.argv[1]))))
