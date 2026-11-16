#!/usr/bin/env python3
"""Export a benchmark's own projects, with durable results and attributable metrics."""
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter
import argparse,csv,hashlib,json,re,sqlite3,sys
from zipfile import ZipFile
from xml.etree import ElementTree as ET

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from scripts.live_acceptance_report import collect,seconds,sanitized

def write(path,value):path.write_text(json.dumps(sanitized(value),indent=2,ensure_ascii=False)+'\n')

def main(run):
 state=json.loads((run/'state.json').read_text());projects=[c['project_id'] for c in state['cases'].values() if c.get('project_id')]
 if not projects:raise ValueError('No benchmark projects created.')
 collect(ROOT/'instance/zyx-agentic-llms.db',projects,[],run/'results')
 evidence=json.loads((run/'results/evidence.json').read_text());by_id={r['id']:r for r in evidence['runs']}
 c=sqlite3.connect((ROOT/'instance/zyx-agentic-llms.db').as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
 marks=','.join('?' for _ in projects)
 versions=[dict(r) for r in c.execute(f'''SELECT v.*,d.chat_thread_id FROM published_draft_versions v
 JOIN draft_documents d ON d.id=v.document_id JOIN chat_threads t ON t.id=d.chat_thread_id
 WHERE t.projectId IN ({marks}) ORDER BY v.created_at,v.draft_version''',projects)]
 for v in versions:v['draft']=json.loads(v.pop('draft_json'))
 decisions=[]
 for rid in by_id:
  for r in c.execute("SELECT sequence,result_json FROM agent_steps WHERE run_id=? AND kind='model' ORDER BY sequence",(rid,)):
   result=json.loads(r['result_json'] or '{}')
   # Retain tool choices, never private reasoning/encrypted provider checkpoints.
   choices=[{k:v for k,v in x.items() if k in ('type','name','call_id','arguments')} for x in result.get('output',[]) if x.get('type')=='function_call']
   decisions.append({'run_id':rid,'sequence':r['sequence'],'tool_calls':choices})
 c.close();write(run/'confirmed-versions.json',versions);write(run/'model-decisions.json',decisions)
 usage=[]
 chat_ids={r['chat_thread_id'] for r in evidence['runs']}
 for line in (ROOT/'instance/dev/agent.log').read_text(errors='replace').splitlines():
  m=re.search(r'Recorded Workspace LLM usage: draft_id=(\d+) category=(\w+) model=([^ ]+) total_tokens=(\d+)',line)
  if m and int(m[1]) in chat_ids:usage.append({'log_timestamp':line[1:24],'chat_thread_id':int(m[1]),'category':m[2],'model':m[3],'total_tokens':int(m[4])})
 write(run/'actual-model-usage.json',usage)
 metrics=[];downloads=[];responses=run/'responses';responses.mkdir(exist_ok=True)
 for t in state['turns']:
  rid=t.get('run_id')
  if rid not in by_id:continue
  r=by_id[rid];tools=[s for s in r['steps'] if s['kind']=='tool'];model_steps=[s for s in r['steps'] if s['kind']=='model']
  counts=r['tool_counts'];before=t.get('usage_before',{});after=t.get('usage_after',{})
  delta=lambda key:after.get(key,0)-before.get(key,0) if 'usage_after' in t else None
  response=r['result'].get('response') or r['result'].get('error_message') or ''
  (responses/(t['id']+'.md')).write_text(response+'\n')
  reads=[];queries=[]
  for step in tools:
   if step['name'] in ('search_legal_sources','web_search'):queries.append({'sequence':step['sequence'],'operation':step['name'],'arguments':step['arguments']})
   if step['name']=='read_authority':reads.append(step['arguments'])
  normalized_queries=[json.dumps(q['arguments'],sort_keys=True) for q in queries]
  out=t.get('output',{})
  item={'turn':t['id'],'workflow':t['workflow'],'run_id':rid,'status':r['status'],'agent_wall_seconds':r['wall_seconds'],
    'decision_steps':len(model_steps),'decision_step_seconds':round(sum(s['elapsed_seconds'] or 0 for s in model_steps),3),
    'tool_steps':len(tools),'tool_step_seconds':round(sum(s['elapsed_seconds'] or 0 for s in tools),3),
    'tool_errors':r['tool_error_count'],'explicit_run_retries':r['attempts'],
    'search_calls':sum(counts.get(k,0) for k in ('search_legal_sources','web_search')),'source_read_calls':counts.get('read_authority',0),
    'exact_duplicate_search_arguments':sum(n-1 for n in Counter(normalized_queries).values()),
    'llm_calls':delta('llmCallCount'),'total_tokens':delta('totalTokens'),
    'candidate_count':r['artifact_kind_counts'].get('draft',0),
    'snapshot_files':r['snapshot_file_count'],'response_words':len(response.split()),
    'output_words':out.get('words'),'draft_version_before':t.get('draft_version_before'),'draft_version_after':out.get('draft_version'),
    'download_ms':out.get('download_ms'),'docx_bytes':out.get('docx_bytes')}
  metrics.append(item)
  write(run/'results'/(t['id']+'-research-activity.json'),{'queries':queries,'reads':reads})
  docx=run/'outputs'/(t['id']+'.docx');markdown=run/'outputs'/(t['id']+'.md')
  if docx.exists():
   with ZipFile(docx) as z:
    corrupt=z.testzip();xml=ET.fromstring(z.read('word/document.xml'))
    ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    text='\n'.join(''.join(x.text or '' for x in p.iter(ns+'t')) for p in xml.iter(ns+'p'))
   (run/'outputs'/(t['id']+'-docx-text.txt')).write_text(text+'\n')
   matching=[v for v in versions if v['chat_thread_id']==r['chat_thread_id'] and v['draft_version']==out.get('draft_version')]
   canonical=matching[0]['draft'].get('markdown') if matching else None
   body=markdown.read_text() if markdown.exists() else None
   result={'turn':t['id'],'zip_ok':corrupt is None,'has_document_text':bool(text.strip()),'docx_sha256_matches_capture':hashlib.sha256(docx.read_bytes()).hexdigest()==out.get('docx_sha256'),'markdown_matches_confirmed_version':body==canonical if canonical is not None else None,'confirmed_version_id':matching[0]['id'] if matching else None}
   downloads.append(result)
 write(run/'metrics.json',metrics);write(run/'download-verification.json',downloads)
 if metrics:
  with (run/'metrics.csv').open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=list(metrics[0]));w.writeheader();w.writerows(metrics)
 summary={'status':state['status'],'collected_at':datetime.now(timezone.utc).isoformat(),'projects':projects,'turns_recorded':len(metrics),'completed_turns':sum(r['status']=='completed' for r in metrics),'agent_wall_seconds':round(sum(r['agent_wall_seconds'] or 0 for r in metrics),3),'llm_calls':sum(r['llm_calls'] or 0 for r in metrics),'total_tokens':sum(r['total_tokens'] or 0 for r in metrics),'tool_errors':sum(r['tool_errors'] for r in metrics),'downloads_recorded':len(downloads),'recorded_models':dict(Counter(u['model'] for u in usage)),'semantic_review':'see quality-review.json when written'}
 write(run/'benchmark-summary.json',summary)
 print(json.dumps(summary))

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('run',type=Path);args=p.parse_args();main(args.run.resolve())
